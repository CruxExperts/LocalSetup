from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .github_api import GitHubAPI


BLOCK_RE = re.compile(r"```qc.issue-handoff.v1\n(.*?)\n```", re.DOTALL)
KNOWN_CATEGORY_LABELS = {"workflow_security", "release", "docs", "pr_review", "inventory"}
_HERDR_PROVENANCE_CHECK = "herdr_upstream_provenance"
_STABLE_RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+$")
_SOURCE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_title(title: str) -> str:
    return " ".join(title.lower().split())


def fingerprint_for(finding: dict[str, Any]) -> str:
    material = "|".join(
        [
            str(finding.get("category", "")),
            str(finding.get("check_type", "")),
            normalize_title(str(finding.get("title", ""))),
            str((finding.get("affected_paths") or [""])[0]),
            str(finding.get("region", "")),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def extract_handoff(body: str) -> dict[str, Any] | None:
    match = BLOCK_RE.search(body or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def issue_body(finding: dict[str, Any], *, compact: bool = False) -> str:
    stored_finding = {"evidence_identity": finding.get("evidence_identity")} if compact else finding
    handoff = {"schema_version": "qc.issue-handoff.v1", "fingerprint": fingerprint_for(finding), "finding": stored_finding}
    block = f"```qc.issue-handoff.v1\n{json.dumps(handoff, indent=2, sort_keys=True)}\n```"
    if compact:
        block = f"<!--\n{block}\n-->"
    return f"{finding['body']}\n\n{block}\n"


def labels_for(finding: dict[str, Any], base_labels: list[str]) -> list[str]:
    labels = set(base_labels + [f"qc/severity/{finding['severity']}"])
    if finding["category"] in KNOWN_CATEGORY_LABELS:
        labels.add(f"qc/category/{finding['category']}")
    return sorted(labels)


def issue_policy(finding: dict[str, Any], *, mode: str = "legacy") -> tuple[bool, str]:
    check_type = str(finding.get("check_type", ""))
    severity = str(finding.get("severity", ""))
    if check_type == "ai_packet_adjudication":
        return False, "ai_artifact_only"
    if mode == "legacy":
        return True, "legacy_issue_behavior"
    if severity in {"high", "critical"}:
        return True, "deterministic_high_or_critical"
    return False, "deterministic_below_issue_threshold"


def find_duplicate(issues: list[dict[str, Any]], finding: dict[str, Any]) -> dict[str, Any] | None:
    matches = _matching_issues(issues, finding)
    return next((issue for issue in matches if issue.get("state", "open") == "open"), matches[0] if matches else None)


def _matching_issues(issues: list[dict[str, Any]], finding: dict[str, Any]) -> list[dict[str, Any]]:
    target = fingerprint_for(finding)
    matches: list[dict[str, Any]] = []
    for issue in issues:
        handoff = extract_handoff(str(issue.get("body", "")))
        if handoff and handoff.get("fingerprint") == target:
            matches.append(issue)
    return matches


def _latest_handoff(issue: dict[str, Any], fingerprint: str, api: GitHubAPI) -> dict[str, Any]:
    latest = issue
    for observation in reversed(api.list_issue_comments(int(issue["number"]))):
        handoff = extract_handoff(str(observation.get("body", "")))
        if handoff and handoff.get("fingerprint") == fingerprint:
            latest = observation
            break
    return latest


def duplicate_is_unchanged(issue: dict[str, Any], finding: dict[str, Any]) -> bool:
    handoff = extract_handoff(str(issue.get("body", "")))
    prior = handoff.get("finding") if handoff else None
    if finding.get("check_type") == _HERDR_PROVENANCE_CHECK and isinstance(prior, dict):
        previous_identity = _normalize_provenance_identity(prior.get("evidence_identity"))
        current_identity = _normalize_provenance_identity(finding.get("evidence_identity"))
        return (
            previous_identity is not None
            and current_identity is not None
            and not _has_new_confirmed_evidence(previous_identity, current_identity)
        )
    return isinstance(prior, dict) and prior == finding


def _normalize_provenance_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"pinned_release_tag", "observed_release_tag", "source_files"}:
        return None
    pinned_tag = value.get("pinned_release_tag")
    observed_tag = value.get("observed_release_tag")
    if pinned_tag is None and observed_tag is None:
        release = None
    elif (
        isinstance(pinned_tag, str)
        and isinstance(observed_tag, str)
        and _STABLE_RELEASE_TAG_RE.fullmatch(pinned_tag)
        and _STABLE_RELEASE_TAG_RE.fullmatch(observed_tag)
    ):
        release = (pinned_tag, observed_tag)
    else:
        return None

    files_value = value.get("source_files")
    if not isinstance(files_value, list):
        return None
    files: dict[str, tuple[str, str]] = {}
    for item in files_value:
        if not isinstance(item, dict) or set(item) != {"path", "pinned_sha256", "observed_sha256"}:
            return None
        path = item.get("path")
        pinned_hash = item.get("pinned_sha256")
        observed_hash = item.get("observed_sha256")
        if (
            not _safe_source_path(path)
            or not isinstance(pinned_hash, str)
            or not _SHA256_RE.fullmatch(pinned_hash)
            or not isinstance(observed_hash, str)
            or not _SHA256_RE.fullmatch(observed_hash)
            or path in files
        ):
            return None
        files[path] = (pinned_hash, observed_hash)
    if release is None and not files:
        return None
    return {
        "pinned_release_tag": release[0] if release else None,
        "observed_release_tag": release[1] if release else None,
        "source_files": [
            {"path": path, "pinned_sha256": hashes[0], "observed_sha256": hashes[1]}
            for path, hashes in sorted(files.items())
        ],
    }


def _safe_source_path(value: Any) -> bool:
    if not isinstance(value, str) or not _SOURCE_PATH_RE.fullmatch(value) or value.startswith("/"):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _has_new_confirmed_evidence(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_release = (previous["pinned_release_tag"], previous["observed_release_tag"])
    current_release = (current["pinned_release_tag"], current["observed_release_tag"])
    if (
        current_release[0] is not None
        and current_release != previous_release
        and (_release_is_drift(current_release) or _release_is_drift(previous_release))
    ):
        return True
    previous_files = {
        item["path"]: (item["pinned_sha256"], item["observed_sha256"])
        for item in previous["source_files"]
    }
    for item in current["source_files"]:
        current_hashes = (item["pinned_sha256"], item["observed_sha256"])
        prior_hashes = previous_files.get(item["path"])
        if current_hashes != prior_hashes and (
            current_hashes[0] != current_hashes[1]
            or (prior_hashes is not None and prior_hashes[0] != prior_hashes[1])
        ):
            return True
    return False


def _release_is_drift(tags: tuple[str | None, str | None]) -> bool:
    if tags[0] is None or tags[1] is None:
        return False
    pinned = tuple(int(part) for part in tags[0].removeprefix("v").split("."))
    observed = tuple(int(part) for part in tags[1].removeprefix("v").split("."))
    return observed > pinned


def _finding_with_merged_provenance_evidence(
    finding: dict[str, Any],
    previous_handoff: dict[str, Any] | None,
) -> dict[str, Any]:
    """Retain earlier confirmed facts when a new comment has partial evidence."""
    if finding.get("check_type") != _HERDR_PROVENANCE_CHECK:
        return finding
    current = _normalize_provenance_identity(finding.get("evidence_identity"))
    if current is None:
        return finding
    handoff = extract_handoff(str(previous_handoff.get("body", ""))) if previous_handoff else None
    stored = handoff.get("finding") if handoff else None
    previous = _normalize_provenance_identity(stored.get("evidence_identity")) if isinstance(stored, dict) else None
    if previous is None:
        return {**finding, "evidence_identity": current}

    recovered: list[str] = []
    prior_release = (previous["pinned_release_tag"], previous["observed_release_tag"])
    current_release = (current["pinned_release_tag"], current["observed_release_tag"])
    if _release_is_drift(prior_release) and current_release[0] is not None and not _release_is_drift(current_release):
        recovered.append(
            f"- Stable release: observed `{current_release[1]}` no longer exceeds pinned `{current_release[0]}`."
        )
    previous_files = {item["path"]: item for item in previous["source_files"]}
    for item in current["source_files"]:
        prior = previous_files.get(item["path"])
        if (
            prior is not None
            and prior["pinned_sha256"] != prior["observed_sha256"]
            and item["pinned_sha256"] == item["observed_sha256"]
        ):
            recovered.append(
                f"- Watched file `{item['path']}` now matches its pinned hash `{item['pinned_sha256']}`."
            )
    merged_files = {
        item["path"]: {"path": item["path"], "pinned_sha256": item["pinned_sha256"], "observed_sha256": item["observed_sha256"]}
        for item in previous["source_files"]
    }
    merged_files.update({
        item["path"]: {"path": item["path"], "pinned_sha256": item["pinned_sha256"], "observed_sha256": item["observed_sha256"]}
        for item in current["source_files"]
    })
    body = finding["body"]
    if recovered:
        body += "\n\n## Confirmed recovery\n" + "\n".join(recovered)
    return {
        **finding,
        "body": body,
        "evidence_identity": {
            "pinned_release_tag": current["pinned_release_tag"] if current["pinned_release_tag"] is not None else previous["pinned_release_tag"],
            "observed_release_tag": current["observed_release_tag"] if current["observed_release_tag"] is not None else previous["observed_release_tag"],
            "source_files": [merged_files[path] for path in sorted(merged_files)],
        },
    }


def _known_evidence_count(handoff: dict[str, Any]) -> int:
    payload = extract_handoff(str(handoff.get("body", "")))
    stored = payload.get("finding") if payload else None
    identity = _normalize_provenance_identity(stored.get("evidence_identity")) if isinstance(stored, dict) else None
    if identity is None:
        return 0
    return int(identity["pinned_release_tag"] is not None) + len(identity["source_files"])


def write_issues(
    findings: list[dict[str, Any]],
    labels: list[str],
    *,
    dry_run: bool = False,
    api: GitHubAPI | None = None,
    policy_mode: str = "legacy",
    title_prefix: str = "QC",
    discovery_label: str | None = None,
    include_closed: bool = False,
    issue_labels: list[str] | None = None,
    compact_handoff: bool = False,
) -> list[dict[str, Any]]:
    api = api or GitHubAPI.from_env()
    can_observe = api.enabled() and not dry_run
    label = discovery_label or "qc-patrol"
    if can_observe and include_closed:
        existing = api.list_issues(label, state="all")
    elif can_observe:
        existing = api.list_open_issues(label)
    else:
        existing = []

    # Provenance handoffs must finish all reads before the first write so an
    # incomplete issue/comment inventory can never produce partial decisions.
    discovered: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    if include_closed and can_observe:
        for finding in findings:
            allowed, _ = issue_policy(finding, mode=policy_mode)
            if not allowed:
                continue
            fingerprint = fingerprint_for(finding)
            matches = _matching_issues(existing, finding)
            duplicate = next(
                (issue for issue in matches if issue.get("state", "open") == "open"),
                None,
            )
            if duplicate is not None:
                # An open issue retains the established update behavior. Closed
                # history does not override its current evidence state.
                latest = _latest_handoff(duplicate, fingerprint, api)
            else:
                histories = [
                    (issue, _latest_handoff(issue, fingerprint, api))
                    for issue in matches
                ]
                identical = next(
                    (
                        (issue, latest)
                        for issue, latest in histories
                        if duplicate_is_unchanged(latest, finding)
                    ),
                    None,
                )
                duplicate, latest = identical or (
                    max(histories, key=lambda item: _known_evidence_count(item[1]))
                    if histories
                    else (None, None)
                )
            discovered[fingerprint] = (duplicate, latest)

    results: list[dict[str, Any]] = []
    for finding in findings:
        title = f"[{title_prefix}][{finding['category']}] {finding['title']}"
        allowed, policy_reason = issue_policy(finding, mode=policy_mode)
        if not allowed:
            results.append({"action": "skip", "title": title, "fingerprint": fingerprint_for(finding), "policy_reason": policy_reason})
            continue
        fingerprint = fingerprint_for(finding)
        if include_closed and can_observe:
            duplicate, latest = discovered.get(fingerprint, (None, None))
        else:
            duplicate = find_duplicate(existing, finding)
            latest = duplicate
            if duplicate is not None and can_observe:
                latest = _latest_handoff(duplicate, fingerprint, api)
        if duplicate:
            action = "unchanged" if duplicate_is_unchanged(latest, finding) else "comment"
            if include_closed and duplicate.get("state") == "closed" and action != "unchanged":
                action = "create"
                if can_observe:
                    applied_labels = issue_labels if issue_labels is not None else labels_for(finding, labels)
                    write_finding = _finding_with_merged_provenance_evidence(finding, latest)
                    created = api.create_issue(title, issue_body(write_finding, compact=compact_handoff), applied_labels)
                    existing.append(created)
            elif action == "comment" and can_observe:
                write_finding = _finding_with_merged_provenance_evidence(finding, latest)
                api.comment_issue(int(duplicate["number"]), issue_body(write_finding, compact=compact_handoff))
        else:
            action = "create"
            if can_observe:
                applied_labels = issue_labels if issue_labels is not None else labels_for(finding, labels)
                created = api.create_issue(title, issue_body(finding, compact=compact_handoff), applied_labels)
                existing.append(created)
        results.append({"action": action, "title": title, "fingerprint": fingerprint_for(finding), "policy_reason": policy_reason})
    return results
