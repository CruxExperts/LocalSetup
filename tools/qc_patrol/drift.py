from __future__ import annotations

import hashlib
import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .schemas import DRIFT_PACKETS_SCHEMA, validate_payload
from .redaction import redact_text




def _fingerprint(kind: str, affected_paths: list[str], facts: dict[str, Any]) -> str:
    material = json.dumps({"kind": kind, "paths": affected_paths, "facts": facts}, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def make_packet(
    *,
    kind: str,
    reason: str,
    severity_hint: str,
    affected_paths: list[str],
    facts: dict[str, Any],
    deterministic_evidence: list[Any],
    snippets: list[Any] | None = None,
    question: str,
    redaction_applied: bool = False,
) -> dict[str, Any]:
    fingerprint = _fingerprint(kind, affected_paths, facts)
    return {
        "packet_id": f"{kind}:{fingerprint[:16]}",
        "fingerprint": fingerprint,
        "kind": kind,
        "reason": reason,
        "severity_hint": severity_hint,
        "affected_paths": affected_paths,
        "facts": facts,
        "deterministic_evidence": deterministic_evidence,
        "snippets": snippets or [],
        "question": question,
        "redaction_applied": redaction_applied,
    }


def load_baseline(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") == "qc.inventory.v2":
        return payload
    if payload.get("schema_version") == "qc.ledger.v2":
        inventory = payload.get("inventory")
        return inventory if isinstance(inventory, dict) else None
    return None


def _files(inventory: dict[str, Any] | None) -> dict[str, str]:
    if not inventory:
        return {}
    return {
        str(row["path"]): str(row["hash"])
        for row in inventory.get("files", [])
        if isinstance(row, dict) and "path" in row and "hash" in row
    }


def _paths(rows: list[dict[str, Any]], key: str = "path") -> set[str]:
    return {str(row.get(key, "")) for row in rows if isinstance(row, dict) and row.get(key)}


def _tracked_source_path(path: str) -> bool:
    return path.startswith(("ls/skills/", "ls/workflows/")) or (
        path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
    )


def _source_snippets(repo_root: Path | None, paths: list[str]) -> list[dict[str, Any]]:
    if repo_root is None:
        return []
    root = repo_root.resolve()
    snippets: list[dict[str, Any]] = []
    remaining = 10_240
    for path in paths:
        if len(snippets) >= 20 or remaining <= 0:
            break
        parts = Path(path).parts
        if Path(path).is_absolute() or ".." in parts:
            continue
        source = root.joinpath(*parts)
        try:
            if source.is_symlink():
                continue
            resolved = source.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            with resolved.open("rb") as handle:
                raw = handle.read(min(512, remaining) + 1)
        except OSError:
            continue
        content = raw[: min(512, remaining)]
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        snippets.append(
            {
                "path": path,
                "text": redact_text(text),
                "truncated": len(raw) > len(content),
            }
        )
        remaining -= len(content)
    return snippets


def _surface(inventory: dict[str, Any], name: str) -> list[dict[str, Any]]:
    rows = (inventory.get("surfaces") or {}).get(name, [])
    return rows if isinstance(rows, list) else []


def build_drift_packets(
    current: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    packets: list[dict[str, Any]] = []
    current_files = _files(current)
    baseline_files = _files(baseline)
    if baseline:
        added = sorted(set(current_files) - set(baseline_files))
        removed = sorted(set(baseline_files) - set(current_files))
        changed = sorted(path for path in set(current_files) & set(baseline_files) if current_files[path] != baseline_files[path])
        source_changes = [path for path in changed if _tracked_source_path(path)]
        source_batch_size = 20
        source_batch_count = (len(source_changes) + source_batch_size - 1) // source_batch_size
        for batch_index, start in enumerate(range(0, len(source_changes), source_batch_size), start=1):
            batch_paths = source_changes[start : start + source_batch_size]
            hash_changes = [
                {
                    "path": path,
                    "expected_sha256": baseline_files[path],
                    "observed_sha256": current_files[path],
                }
                for path in batch_paths
            ]
            packets.append(
                make_packet(
                    kind="source.file_hashes_changed",
                    reason="Tracked skill, workflow-package, or GitHub workflow files differ from the successful baseline; this hash change alone does not establish stale upstream provenance.",
                    severity_hint="low",
                    affected_paths=batch_paths,
                    facts={
                        "changed_file_count": len(batch_paths),
                        "total_changed_file_count": len(source_changes),
                        "batch_index": batch_index,
                        "batch_count": source_batch_count,
                        "hash_changes": hash_changes,
                    },
                    deterministic_evidence=hash_changes,
                    snippets=_source_snippets(repo_root, batch_paths),
                    question="Review only the supplied file excerpts and check results. A changed file or recorded source commit alone is not proof that upstream provenance is stale.",
                )
            )

        if added:
            packets.append(
                make_packet(
                    kind="shape.files_added",
                    reason="Tracked files were added since the baseline inventory.",
                    severity_hint="low",
                    affected_paths=added[:20],
                    facts={"added_count": len(added)},
                    deterministic_evidence=added[:50],
                    question="Do these new tracked files introduce a QC surface that needs deterministic coverage?",
                )
            )
        if removed:
            packets.append(
                make_packet(
                    kind="shape.files_removed",
                    reason="Tracked files were removed since the baseline inventory.",
                    severity_hint="low",
                    affected_paths=removed[:20],
                    facts={"removed_count": len(removed)},
                    deterministic_evidence=removed[:50],
                    question="Do these removals orphan generated docs, workflow references, or package boundaries?",
                )
            )
        changed_manifests = [path for path in changed if path in {"pyproject.toml", "uv.lock", "VERSION", "ls/config/pack.yaml"}]
        if changed_manifests:
            packets.append(
                make_packet(
                    kind="shape.manifests_changed",
                    reason="Package or registry manifests changed since the baseline.",
                    severity_hint="medium",
                    affected_paths=changed_manifests,
                    facts={"changed_count": len(changed_manifests)},
                    deterministic_evidence=changed_manifests,
                    question="Do changed manifests imply stale generated docs or new release boundaries?",
                )
            )
        baseline_workflows = _paths(_surface(baseline, "workflows")) if baseline else set()
        current_workflows = _paths(_surface(current, "workflows"))
        new_workflows = sorted(current_workflows - baseline_workflows)
        if new_workflows:
            packets.append(
                make_packet(
                    kind="shape.workflows_added",
                    reason="New GitHub workflows appeared since the baseline.",
                    severity_hint="medium",
                    affected_paths=new_workflows[:20],
                    facts={"new_workflow_count": len(new_workflows)},
                    deterministic_evidence=[row for row in _surface(current, "workflows") if row.get("path") in new_workflows],
                    question="Should new workflows be added to QC workflow contracts, release exclusions, or permissions checks?",
                )
            )
        baseline_generated = _paths(_surface(baseline, "generated_artifacts")) if baseline else set()
        current_generated = _paths(_surface(current, "generated_artifacts"))
        new_generated = sorted(current_generated - baseline_generated)
        if new_generated:
            packets.append(
                make_packet(
                    kind="shape.generated_artifacts_added",
                    reason="New generated artifacts appeared since the baseline.",
                    severity_hint="low",
                    affected_paths=new_generated[:20],
                    facts={"new_generated_count": len(new_generated)},
                    deterministic_evidence=[row for row in _surface(current, "generated_artifacts") if row.get("path") in new_generated],
                    question="Are these generated artifacts registered with source inputs and refresh tooling?",
                )
            )
    else:
        packets.append(
            make_packet(
                kind="shape.no_baseline",
                reason="No baseline inventory was available for this patrol run.",
                severity_hint="low",
                affected_paths=[],
                facts={"tracked_file_count": current.get("tracked_file_count", 0)},
                deterministic_evidence=[],
                question="Establish this run as the first adaptive QC baseline.",
            )
        )
    payload = {"schema_version": "qc.drift-packets.v1", "packets": packets}
    errors = validate_payload(payload, DRIFT_PACKETS_SCHEMA)
    if errors:
        raise ValueError("invalid QC drift packets: " + "; ".join(errors))
    return payload
def load_pending_packets(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError) as exc:
        raise ValueError("QC pending packet artifact is unavailable or invalid") from exc
    packets = payload.get("packets") if isinstance(payload, dict) and payload.get("schema_version") == "qc.pending-packets.v1" else None
    if not isinstance(packets, list) or any(
        not isinstance(packet, dict) or not isinstance(packet.get("packet_id"), str) for packet in packets
    ):
        raise ValueError("QC pending packet artifact has an invalid packet list")
    if validate_payload({"schema_version": "qc.drift-packets.v1", "packets": packets}, DRIFT_PACKETS_SCHEMA):
        raise ValueError("QC pending packet artifact has an invalid packet list")
    return packets


def merge_pending_packets(
    current_payload: dict[str, Any],
    pending: list[dict[str, Any]],
    current_inventory: dict[str, Any],
    dispositions: dict[str, str] | None = None,
) -> dict[str, Any]:
    disposed_ids = set(dispositions or {})
    current_files = _files(current_inventory)
    retained_pending = []
    for packet in pending:
        if packet.get("kind") == "source.file_hashes_changed":
            evidence = packet.get("deterministic_evidence", [])
            if not evidence or any(
                not isinstance(row, dict)
                or current_files.get(str(row.get("path", ""))) != row.get("observed_sha256")
                for row in evidence
            ):
                continue
        retained_pending.append(packet)
    packets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for packet in [*retained_pending, *current_payload.get("packets", [])]:
        packet_id = str(packet.get("packet_id", ""))
        if packet_id and packet_id not in seen and packet_id not in disposed_ids:
            packets.append(packet)
            seen.add(packet_id)
    payload = {"schema_version": "qc.drift-packets.v1", "packets": packets}
    errors = validate_payload(payload, DRIFT_PACKETS_SCHEMA)
    if errors:
        raise ValueError("invalid merged QC drift packets: " + "; ".join(errors))
    return payload


def pending_packets_payload(packets: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": "qc.pending-packets.v1", "packets": packets}




def summarize_drift(packets_payload: dict[str, Any]) -> dict[str, Any]:
    packets = packets_payload.get("packets", [])
    return {
        "packet_count": len(packets),
        "kinds": sorted({str(packet.get("kind")) for packet in packets if isinstance(packet, dict)}),
    }
