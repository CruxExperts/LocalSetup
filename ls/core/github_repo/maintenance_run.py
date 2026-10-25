from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .adapter import GitHubError
from .maintenance_config import CONFIG_RELATIVE_PATH, MaintenanceError, parse_config_bytes
from .model import RepositoryTarget
from .maintenance_service import (
    MaintenanceOperationError,
    _adapter,
    _read_target_bytes,
    _repo_metadata,
    _select_targets,
    _verify_one,
)

_OUTPUT_BYTE_LIMIT = 128 * 1024


def _api_page(adapter: Any, suffix: str, operation: str, *, collection_key: str | None = None) -> list[dict[str, Any]]:
    result = adapter._paginate_result(suffix, operation=operation, collection_key=collection_key)
    if not isinstance(result, dict) or result.get("complete") is not True:
        raise MaintenanceOperationError(f"{operation} pagination is incomplete")
    rows = result.get("items")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise MaintenanceOperationError(f"{operation} returned an invalid page")
    return rows


def _safe_issue_rows(rows: list[dict[str, Any]], *, kind: str, full_name: str) -> list[dict[str, Any]]:
    prefix = f"https://github.com/{full_name}/{'pull' if kind == 'pull_request' else 'issues'}/"
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        number = row.get("number")
        url = row.get("html_url")
        expected_url = f"{prefix}{number}" if type(number) is int and number > 0 else None
        if type(number) is not int or number <= 0 or not isinstance(url, str) or url != expected_url:
            raise MaintenanceOperationError("GitHub issue metadata failed identity validation")
        identity = (kind, number)
        if identity in seen:
            raise MaintenanceOperationError("GitHub returned duplicate issue metadata")
        seen.add(identity)
        result.append({"kind": kind, "number": number, "url": url})
    return sorted(result, key=lambda item: item["number"], reverse=True)


def _issue_snapshot(adapter: Any, full_name: str, limit: int) -> dict[str, Any]:
    issues = _api_page(adapter, "issues?state=open", "maintenance_open_issues")
    pulls = _api_page(adapter, "pulls?state=open", "maintenance_open_pull_requests")
    issue_rows = [row for row in issues if "pull_request" not in row]
    clean_issues = _safe_issue_rows(issue_rows, kind="issue", full_name=full_name)
    clean_pulls = _safe_issue_rows(pulls, kind="pull_request", full_name=full_name)
    return {
        "issues": {"count": len(clean_issues), "items": clean_issues[:limit], "truncated": len(clean_issues) > limit},
        "pull_requests": {"count": len(clean_pulls), "items": clean_pulls[:limit], "truncated": len(clean_pulls) > limit},
    }


def _action_snapshot(adapter: Any, full_name: str, limit: int) -> dict[str, Any]:
    rows = _api_page(adapter, "actions/runs", "maintenance_workflow_runs", collection_key="workflow_runs")
    failures: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("status")
        conclusion = row.get("conclusion")
        if status != "completed" or conclusion == "success":
            continue
        conclusion_name = conclusion if isinstance(conclusion, str) and conclusion else "unknown"
        counts[conclusion_name] = counts.get(conclusion_name, 0) + 1
        run_id = row.get("id")
        html_url = row.get("html_url")
        expected_url = f"https://github.com/{full_name}/actions/runs/{run_id}" if type(run_id) is int and run_id > 0 else None
        if type(run_id) is not int or run_id <= 0 or not isinstance(html_url, str) or html_url != expected_url:
            raise MaintenanceOperationError("GitHub workflow run metadata failed identity validation")
        failures.append({"run_id": run_id, "url": html_url, "conclusion": conclusion_name})
    failures.sort(key=lambda item: item["run_id"], reverse=True)
    return {"count": sum(counts.values()), "conclusion_counts": dict(sorted(counts.items())), "items": failures[:limit], "truncated": len(failures) > limit}


def _bounded_packet(packet: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) <= _OUTPUT_BYTE_LIMIT:
        return packet
    for key in ("issues", "pull_requests", "action_failures"):
        section = packet.get("findings", {}).get(key)
        if isinstance(section, dict):
            section["items"] = section.get("items", [])[:10]
            section["truncated"] = True
    packet["packet_truncated"] = True
    packet["handoff_ready"] = False
    packet.setdefault("readiness_reasons", []).append("packet_size_limit")
    return packet


def _run_one(
    repository: PublicRepository | None,
    *,
    target: RepositoryTarget,
    checkout: Path,
    adapter_factory: Callable[[RepositoryTarget], Any],
    validate: bool,
    limit: int,
) -> dict[str, Any]:
    if repository is not None and not checkout.exists():
        return {
            "schema": 1,
            "kind": "localsetup.github-maintenance-handoff",
            "handoff_ready": False,
            "target": {"hostname": target.hostname, "repository_id": repository.repository_id, "full_name": repository.full_name},
            "readiness_reasons": ["checkout_missing_or_ambiguous"],
        }
    verified = _verify_one(repository, target=target, checkout=checkout, adapter_factory=adapter_factory, validate=validate)
    adapter = adapter_factory(target)
    metadata = _repo_metadata(adapter, target)
    head = verified.get("head", {})
    default_branch = metadata.get("default_branch")
    findings: dict[str, Any] = {}
    collection_errors: list[str] = []
    try:
        findings.update(_issue_snapshot(adapter, metadata["full_name"], limit))
    except (MaintenanceOperationError, GitHubError):
        collection_errors.append("open_issues_or_pull_requests_incomplete")
    try:
        findings["action_failures"] = _action_snapshot(adapter, metadata["full_name"], limit)
    except (MaintenanceOperationError, GitHubError):
        collection_errors.append("workflow_run_pagination_incomplete")
    security = verified.get("security_advisories", {"status": "unknown", "counts": {}})
    if security["status"] != "complete":
        collection_errors.append("security_advisory_counts_unknown")
    reasons: list[str] = []
    if verified.get("config") != "current" or verified.get("config_tracked") is not True:
        reasons.append("config_not_current_and_tracked")
    if verified.get("workflow") != "current" or verified.get("workflow_tracked") is not True:
        reasons.append("workflow_not_current_and_tracked")
    if verified.get("origin") != "current":
        reasons.append("checkout_origin_mismatch")
    if not isinstance(head, dict) or head.get("status") != "current":
        reasons.append("local_head_stale_or_unknown")
    ci_evidence = verified.get("ci_evidence", {})
    if ci_evidence.get("ok") is not True:
        reasons.append("exact_head_ci_evidence_partial" if ci_evidence.get("status") == "partial" else "exact_head_ci_evidence_unknown")
    if verified.get("commit_signature") != "cryptographically_valid":
        reasons.append("local_head_signature_not_verified")
    if any(value.get("status") != "supported" for value in verified.get("tool_capabilities", {}).values()):
        reasons.append("configured_toolchain_unknown")
    if verified.get("handoff_ready") is not True:
        reasons.append("verification_not_ready")
    if type(verified.get("unexpected_tracked_maintenance_files")) is not int or verified.get("unexpected_tracked_maintenance_files") != 0:
        reasons.append("unexpected_tracked_maintenance_files")
    tracked_input_state = verified.get("tracked_input_state", {})
    if not isinstance(tracked_input_state, dict) or tracked_input_state.get("status") != "clean":
        reasons.append("tracked_inputs_dirty_or_unknown")
    if validate:
        local_validation = verified.get("local_validation")
        if not isinstance(local_validation, dict) or any(
            not isinstance(result, dict) or result.get("status") != "passed"
            for result in local_validation.values()
        ):
            reasons.append("local_validation_incomplete_or_failed")
    if collection_errors:
        reasons.extend(collection_errors)
    packet = {
        "schema": 1,
        "kind": "localsetup.github-maintenance-handoff",
        "handoff_ready": not reasons,
        "target": {
            "hostname": target.hostname,
            "repository_id": metadata["id"],
            "full_name": metadata["full_name"],
            "default_branch": default_branch if isinstance(default_branch, str) else None,
        },
        "anchor": {
            "local_head": head.get("local") if isinstance(head, dict) else None,
            "remote_default_head": head.get("remote_default") if isinstance(head, dict) else None,
            "config_digest": verified.get("config_digest"),
            "workflow_commit": _workflow_commit(checkout),
        },
        "checks": {
            "identity": "current",
            "origin": verified.get("origin"),
            "config": verified.get("config"),
            "workflow": verified.get("workflow"),
            "head": head.get("status") if isinstance(head, dict) else "unknown",
            "ci_evidence": verified.get("ci_evidence", {"status": "unknown", "ok": False}),
            "tracked_input_state": tracked_input_state,
            "commit_signature": verified.get("commit_signature"),
            "tool_capabilities": verified.get("tool_capabilities", {}),
            "pagination": {
                "issues_and_pull_requests": "complete" if "open_issues_or_pull_requests_incomplete" not in collection_errors else "unknown",
                "actions": "complete" if "workflow_run_pagination_incomplete" not in collection_errors else "unknown",
            },
        },
        "findings": findings,
        "security_advisories": security,
        "readiness_reasons": reasons,
        "native_agent_instruction": "Review the bounded public metadata and decide the next read-only maintenance task. Do not infer missing security coverage is clean. No external model was launched and no repository command was executed unless --validate was explicitly selected.",
    }
    if validate:
        packet["checks"]["local_validation"] = verified.get("local_validation", {})
        packet["checks"]["local_validation_sandbox"] = verified.get("local_validation_sandbox")
    return _bounded_packet(packet)


def _workflow_commit(checkout: Path) -> str | None:
    raw = _read_target_bytes(checkout, CONFIG_RELATIVE_PATH)
    if raw is None:
        return None
    try:
        config = parse_config_bytes(raw)
    except MaintenanceError:
        return None
    return config["workflow_commit"]


def run(args: Any, home: Path, *, adapter_factory: Callable[[RepositoryTarget], Any] = _adapter) -> dict[str, Any]:
    limit = getattr(args, "limit", 10)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise MaintenanceOperationError("--limit must be an integer from 1 through 100")
    results: list[dict[str, Any]] = []
    selected = _select_targets(args, adapter_factory=adapter_factory)
    discovered_count = len(selected)
    if getattr(args, "all_public", False):
        work = selected[:limit]
        pending = selected[limit:]
    else:
        work = selected
        pending = []
    for repository, target, checkout in work:
        if repository is not None and checkout is None:
            results.append({
                "schema": 1,
                "kind": "localsetup.github-maintenance-handoff",
                "handoff_ready": False,
                "target": {"hostname": target.hostname, "repository_id": repository.repository_id, "full_name": repository.full_name},
                "readiness_reasons": ["checkout_missing_or_ambiguous"],
            })
            continue
        try:
            assert checkout is not None
            results.append(_run_one(repository, target=target, checkout=checkout, adapter_factory=adapter_factory, validate=bool(getattr(args, "validate", False)), limit=limit))
        except (MaintenanceOperationError, GitHubError) as exc:
            results.append({
                "schema": 1,
                "kind": "localsetup.github-maintenance-handoff",
                "handoff_ready": False,
                "target": {"hostname": target.hostname, "repository_id": repository.repository_id if repository else None, "full_name": repository.full_name if repository else target.full_name},
                "readiness_reasons": ["collection_failed"],
                "error": str(exc) if isinstance(exc, MaintenanceOperationError) else "GitHub metadata collection failed",
            })
    ready = bool(results) and not pending and all(item.get("handoff_ready") is True for item in results)
    return {
        "status": "ready" if ready else "incomplete",
        "scope": "all_public" if getattr(args, "all_public", False) else "repository",
        "owner": getattr(args, "owner", None),
        "repository_count": discovered_count,
        "processed_repository_count": len(results),
        "discovered_public_repository_count": discovered_count if getattr(args, "all_public", False) else None,
        "pending_repository_count": len(pending),
        "pending_repository_ids": [repository.repository_id for repository, _, _ in pending if repository is not None],
        "pending_status": "pending_limit" if pending else "none",
        "handoff_ready": ready,
        "repositories": results,
    }
