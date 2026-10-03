#!/usr/bin/env python3
"""Pure, offline LocalSetup Agent-* routing selector."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


REQUEST_SCHEMA = "agent_routing_request_v1"
RECEIPT_SCHEMA = "agent_routing_receipt_v1"
REQUEST_KEYS = {"schema", "task_class", "risk", "required_capabilities"}
TASK_CLASSES = {"discovery", "routine", "implementation", "research", "review"}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
CAPABILITIES = {"vision"}
LANE_ORDER = {
    "Agent-Compact": 0,
    "Agent-Efficient": 1,
    "Agent-Balanced": 2,
    "Agent-Frontier": 3,
    "Agent-Realtime": 4,
}
MODEL_RE = re.compile(r"^(gpt-6-astra|gpt-6\.1-sol|gpt-6-luna)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_STATIC_RESOURCE_BYTES = 1_048_576
MAX_REQUEST_BYTES = 65_536

# These anchors are intentionally in the isolated standard-library selector, not
# in a mutable manifest.  A valid manifest digest cannot substitute for one.
CANONICAL_MANIFEST_SHA256 = "56a7b4c66b7bb0a37871135f087077ae5624789aa3a83fd1a736a4103b94c651"
CANONICAL_MATRIX_SCHEMA_SHA256 = "d2a63c63450debba81eafc34f1baf613a97e2327f52c629d0131614fbab0ba2b"
CANONICAL_SNAPSHOT_SHA256 = "db483c1ee106cc982ed78f538270b83674b6fc6bc0f63212f3ed645918f11838"
CANONICAL_REQUEST_SCHEMA_SHA256 = "f05f89d84b8a4617855cf51a23663e08709051b9020f6cfdf4d22ee46d0ed562"
CANONICAL_RECEIPT_SCHEMA_SHA256 = "b9c8076f8af65af04e214d9729d21f2200e9b5f9397dab8a0943b95aa693f1f5"

CANONICAL_EVIDENCE = (
    ("api-family-gpt-6", "https://developers.openai.com/api/docs/guides/latest-model", "2026-10-03", "api_family"),
    ("model-gpt-6-astra", "https://developers.openai.com/api/docs/models/gpt-6-astra", "2026-10-03", "model_version"),
    ("model-gpt-6.1-sol", "https://developers.openai.com/api/docs/models/gpt-6.1-sol", "2026-10-03", "model_version"),
    ("model-gpt-6-luna", "https://developers.openai.com/api/docs/models/gpt-6-luna", "2026-10-03", "model_version"),
    ("client-codex-subagents", "https://learn.chatgpt.com/docs/agent-configuration/subagents", "2026-10-03", "client_product"),
    ("accounting-api-pricing-standard-short-context", "https://developers.openai.com/api/docs/pricing", "2026-10-03", "accounting"),
    ("accounting-codex-token-rate-card", "https://learn.chatgpt.com/docs/pricing", "2026-10-03", "accounting"),
)

CANONICAL_FACT_PAYLOADS: dict[str, dict[str, Any]] = {
    "model-gpt-6-astra": {
        "record_kind": "model",
        "model_id": "gpt-6-astra",
        "facts": {"page_label": "Default", "description": "Most capable model for the most demanding work."},
    },
    "model-gpt-6.1-sol": {
        "record_kind": "model",
        "model_id": "gpt-6.1-sol",
        "facts": {"page_label": "Default", "description": "Near-Astra performance for complex work at a lower cost."},
    },
    "model-gpt-6-luna": {
        "record_kind": "model",
        "model_id": "gpt-6-luna",
        "facts": {"page_label": "Default", "description": "Most efficient model for focused, high-volume tasks."},
    },
    "api-family-gpt-6": {
        "record_kind": "scoped",
        "record_id": "api-family-gpt-6",
        "scope": "api_family",
        "facts": {
            "model_reasoning_effort": {
                "gpt-6-astra": ["low", "medium", "high", "xhigh", "max"],
                "gpt-6.1-sol": ["low", "medium", "high", "xhigh", "max"],
                "gpt-6-luna": ["none", "low", "medium", "high", "xhigh", "max"],
            },
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "context_tokens": 1050000,
            "max_output_tokens": 128000,
            "endpoints": ["responses", "chat_completions", "batch"],
            "tools": ["web_search", "file_search", "image_generation", "code_interpreter", "hosted_shell", "apply_patch", "skills", "computer_use", "mcp", "tool_search"],
        },
    },
    "client-codex-subagents": {
        "record_kind": "scoped",
        "record_id": "codex-client-subagent-defaults",
        "scope": "client_product",
        "facts": {
            "recommended_models": ["gpt-6.1-sol", "gpt-6-luna"],
            "documented_subagent_defaults": {
                "agents_enabled": True,
                "max_concurrent_threads_per_session": "client_selected_when_unset",
                "legacy_max_threads_alias": "agents.max_threads",
                "interrupt_message": True,
            },
        },
    },
    "accounting-api-pricing-standard-short-context": {
        "record_kind": "scoped",
        "record_id": "api-pricing-standard-short-context",
        "scope": "accounting",
        "facts": {
            "rate_class": "API Pricing Standard short-context",
            "unit": "USD_per_1M_tokens",
            "routing_use": "forbidden",
            "account_entitlement": "unknown",
            "plan_migration": "unknown",
            "models": {
                "gpt-6-astra": {"input": 10, "cached_input": 1, "cache_write": 12.5, "output": 50},
                "gpt-6.1-sol": {"input": 2, "cached_input": 0.1, "cache_write": 2.5, "output": 10},
                "gpt-6-luna": {"input": 0.1, "cached_input": 0.01, "cache_write": 0.125, "output": 0.5},
            },
        },
    },
    "accounting-codex-token-rate-card": {
        "record_kind": "scoped",
        "record_id": "codex-token-rate-card",
        "scope": "accounting",
        "facts": {
            "rate_class": "Codex token rate-card",
            "unit": "credits_per_1M_tokens",
            "routing_use": "forbidden",
            "account_entitlement": "unknown",
            "plan_migration": "unknown",
            "models": {
                "gpt-6-astra": {"input": 250, "cached_input": 25, "output": 1250},
                "gpt-6.1-sol": {"input": 50, "cached_input": 2.5, "output": 250},
                "gpt-6-luna": {"input": 2.5, "cached_input": 0.25, "output": 12.5},
            },
        },
    },
}


class RoutingError(ValueError):
    pass


def _skill_root() -> Path:
    return Path(__file__).resolve(strict=True).parent.parent


def _valid_component(name: str) -> bool:
    return isinstance(name, str) and name not in {"", ".", ".."} and "\x00" not in name and "/" not in name and "\\" not in name


def _identity(metadata: Any) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _capture(parent_fd: int, name: str, *, directory: bool) -> Any:
    if not _valid_component(name):
        raise RoutingError("resource path is invalid")
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, ValueError, TypeError, NotImplementedError):
        raise RoutingError("resource entry is unavailable") from None
    if not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)):
        raise RoutingError("resource entry is unavailable")
    if not directory and not 0 <= metadata.st_size <= MAX_STATIC_RESOURCE_BYTES:
        raise RoutingError("resource file is invalid")
    return metadata


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except (OSError, ValueError, TypeError, NotImplementedError):
        raise RoutingError("resource descriptor cleanup failed") from None


def _open_directory(parent_fd: int, name: str) -> int:
    observed = _capture(parent_fd, name, directory=True)
    fd: int | None = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if _identity(opened) != _identity(observed) or not stat.S_ISDIR(opened.st_mode):
            raise RoutingError("resource directory changed during open")
        return fd
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        raise RoutingError("resource directory is unavailable") from None
    finally:
        if fd is not None:
            try:
                if "opened" not in locals() or _identity(opened) != _identity(observed) or not stat.S_ISDIR(opened.st_mode):
                    _close_fd(fd)
            except RoutingError:
                raise


def _open_absolute_directory(path: Path) -> int:
    absolute = path.absolute()
    if not absolute.is_absolute():
        raise RoutingError("resource root is unavailable")
    current_fd: int | None = None
    try:
        current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        root_stat = os.fstat(current_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise RoutingError("resource root is unavailable")
        for component in absolute.parts[1:]:
            child_fd = _open_directory(current_fd, component)
            parent_fd = current_fd
            current_fd = None
            try:
                _close_fd(parent_fd)
            except RoutingError:
                try:
                    _close_fd(child_fd)
                except RoutingError:
                    pass
                raise
            current_fd = child_fd
        result_fd = current_fd
        current_fd = None
        return result_fd
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        raise RoutingError("resource root is unavailable") from None
    finally:
        if current_fd is not None:
            _close_fd(current_fd)


def _read_file(parent_fd: int, name: str) -> bytes:
    observed = _capture(parent_fd, name, directory=False)
    fd: int | None = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if _identity(opened) != _identity(observed) or not stat.S_ISREG(opened.st_mode) or opened.st_size != observed.st_size:
            raise RoutingError("resource file changed during open")
        remaining = observed.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                raise RoutingError("resource file ended early")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise RoutingError("resource file has trailing content")
        final = os.fstat(fd)
        if _identity(final) != _identity(observed) or final.st_size != observed.st_size:
            raise RoutingError("resource file changed during read")
        return b"".join(chunks)
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        raise RoutingError("resource file is invalid") from None
    finally:
        if fd is not None:
            _close_fd(fd)


def _load_json(parent_fd: int, name: str) -> tuple[dict[str, Any], bytes]:
    try:
        data = _read_file(parent_fd, name)
        value = json.loads(data.decode("utf-8"))
    except (RoutingError, UnicodeError, json.JSONDecodeError) as exc:
        raise RoutingError("resource file is invalid") from exc
    if not isinstance(value, dict):
        raise RoutingError("resource file is invalid")
    return value, data


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _descriptor_contract(kind: str, value: dict[str, Any]) -> bool:
    common = {"$schema", "$id", "type", "additionalProperties", "required", "properties"}
    if not common <= set(value) or value.get("type") != "object" or value.get("additionalProperties") is not False:
        return False
    if not isinstance(value.get("required"), list) or not isinstance(value.get("properties"), dict):
        return False
    if kind == "request":
        return set(value) == common and set(value["required"]) == REQUEST_KEYS and set(value["properties"]) == REQUEST_KEYS
    if kind == "receipt":
        required = {"schema", "status", "reason", "snapshot_sha256", "selection_policy", "ultra_selected", "evidence_summary"}
        return set(value) == common | {"allOf"} and set(value["required"]) == required and required <= set(value["properties"])
    matrix_required = {"$schema", "schema_version", "resource_id", "owner_skill", "reviewed_at", "fresh_until", "candidates", "model_records", "scoped_records"}
    return set(value) == common | {"$defs"} and set(value["required"]) == matrix_required and set(value["properties"]) == matrix_required and isinstance(value.get("$defs"), dict)


def _anchored_descriptor(parent_fd: int, name: str, *, digest: str, kind: str) -> dict[str, Any]:
    value, data = _load_json(parent_fd, name)
    if _digest(data) != digest or not _descriptor_contract(kind, value):
        raise RoutingError("descriptor is invalid")
    return value


def _manifest_evidence() -> list[dict[str, str]]:
    return [
        {"evidence_id": evidence_id, "url": url, "access_date": access_date, "claim_scope": claim_scope}
        for evidence_id, url, access_date, claim_scope in CANONICAL_EVIDENCE
    ]


def _parse_date(value: Any) -> dt.date:
    if not isinstance(value, str):
        raise RoutingError("resource date is invalid")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise RoutingError("resource date is invalid") from exc


def _valid_candidate(candidate: Any, model_ids: set[str]) -> bool:
    expected = {"lane", "model_id", "eligibility", "policy_priority", "task_classes", "maximum_risk", "capabilities"}
    if not isinstance(candidate, dict) or set(candidate) != expected:
        return False
    if candidate.get("lane") not in LANE_ORDER or candidate.get("model_id") not in model_ids or not MODEL_RE.fullmatch(str(candidate.get("model_id"))):
        return False
    if candidate.get("eligibility") != "reviewed" or type(candidate.get("policy_priority")) is not int or candidate["policy_priority"] < 0:
        return False
    tasks = candidate.get("task_classes")
    if not isinstance(tasks, list) or not tasks or any(task not in TASK_CLASSES for task in tasks) or len(tasks) != len(set(tasks)):
        return False
    if candidate.get("maximum_risk") not in RISK_ORDER:
        return False
    capabilities = candidate.get("capabilities")
    return capabilities == {"vision": {"value": "unknown"}}


def _validate_evidence_and_facts(snapshot: dict[str, Any]) -> set[str]:
    model_rows = snapshot.get("model_records")
    scoped_rows = snapshot.get("scoped_records")
    if not isinstance(model_rows, list) or not isinstance(scoped_rows, list):
        raise RoutingError("snapshot records are invalid")
    expected_ids = [row[0] for row in CANONICAL_EVIDENCE]
    used_ids: list[str] = []
    model_ids: set[str] = set()
    for row in model_rows:
        if not isinstance(row, dict) or set(row) != {"model_id", "evidence_id", "facts"}:
            raise RoutingError("model record is invalid")
        evidence_id = row.get("evidence_id")
        expected = CANONICAL_FACT_PAYLOADS.get(evidence_id)
        if expected is None or expected.get("record_kind") != "model":
            raise RoutingError("model evidence is invalid")
        if row != {"model_id": expected["model_id"], "evidence_id": evidence_id, "facts": expected["facts"]}:
            raise RoutingError("model fact payload is invalid")
        model_ids.add(row["model_id"])
        used_ids.append(evidence_id)
    for row in scoped_rows:
        if not isinstance(row, dict) or set(row) != {"record_id", "scope", "evidence_id", "facts"}:
            raise RoutingError("scoped record is invalid")
        evidence_id = row.get("evidence_id")
        expected = CANONICAL_FACT_PAYLOADS.get(evidence_id)
        if expected is None or expected.get("record_kind") != "scoped":
            raise RoutingError("scoped evidence is invalid")
        expected_row = {"record_id": expected["record_id"], "scope": expected["scope"], "evidence_id": evidence_id, "facts": expected["facts"]}
        if row != expected_row:
            raise RoutingError("scoped fact payload is invalid")
        used_ids.append(evidence_id)
    if sorted(used_ids) != sorted(expected_ids) or len(used_ids) != len(set(used_ids)) or len(model_ids) != 3:
        raise RoutingError("static evidence coverage is invalid")
    return model_ids


def _load_snapshot_inner() -> tuple[dict[str, Any], str, str | None]:
    descriptors: list[int] = []
    try:
        skill_root = _open_absolute_directory(_skill_root())
        descriptors.append(skill_root)
        schemas_root = _open_directory(skill_root, "schemas")
        descriptors.append(schemas_root)
        resources_root = _open_directory(skill_root, "resources")
        descriptors.append(resources_root)
        matrix_root = _open_directory(resources_root, "model-capability-matrix")
        descriptors.append(matrix_root)
        _anchored_descriptor(schemas_root, "routing-request.schema.json", digest=CANONICAL_REQUEST_SCHEMA_SHA256, kind="request")
        _anchored_descriptor(schemas_root, "routing-receipt.schema.json", digest=CANONICAL_RECEIPT_SCHEMA_SHA256, kind="receipt")
        manifest, manifest_bytes = _load_json(matrix_root, "manifest.json")
        if _digest(manifest_bytes) != CANONICAL_MANIFEST_SHA256:
            raise RoutingError("manifest is invalid")
        expected_manifest_keys = {"schema_version", "resource_id", "owner_skill", "schema_path", "snapshot_path", "matrix_schema_sha256", "snapshot_sha256", "evidence"}
        if (
            set(manifest) != expected_manifest_keys
            or manifest.get("schema_version") != 1
            or manifest.get("resource_id") != "model-capability-matrix"
            or manifest.get("owner_skill") != "ls-agent-routing"
            or manifest.get("schema_path") != "schema.json"
            or manifest.get("snapshot_path") != "snapshot.json"
            or manifest.get("matrix_schema_sha256") != CANONICAL_MATRIX_SCHEMA_SHA256
            or manifest.get("snapshot_sha256") != CANONICAL_SNAPSHOT_SHA256
            or manifest.get("evidence") != _manifest_evidence()
        ):
            raise RoutingError("manifest is invalid")
        _anchored_descriptor(matrix_root, manifest["schema_path"], digest=CANONICAL_MATRIX_SCHEMA_SHA256, kind="matrix")
        snapshot, snapshot_bytes = _load_json(matrix_root, manifest["snapshot_path"])
        digest = _digest(snapshot_bytes)
        if digest != CANONICAL_SNAPSHOT_SHA256:
            raise RoutingError("snapshot is invalid")
        required = {"$schema", "schema_version", "resource_id", "owner_skill", "reviewed_at", "fresh_until", "candidates", "model_records", "scoped_records"}
        if (
            set(snapshot) != required
            or snapshot.get("$schema") != "https://localsetup.dev/resources/model-capability-matrix/v1/schema.json"
            or snapshot.get("schema_version") != 1
            or snapshot.get("resource_id") != "model-capability-matrix"
            or snapshot.get("owner_skill") != "ls-agent-routing"
            or not isinstance(snapshot.get("candidates"), list)
        ):
            raise RoutingError("snapshot is invalid")
        _parse_date(snapshot["reviewed_at"])
        fresh_until = _parse_date(snapshot["fresh_until"])
        model_ids = _validate_evidence_and_facts(snapshot)
        candidates = snapshot["candidates"]
        if len(candidates) != len({candidate.get("lane") for candidate in candidates if isinstance(candidate, dict)}) or not all(_valid_candidate(candidate, model_ids) for candidate in candidates):
            raise RoutingError("candidate is invalid")
        if fresh_until < dt.date.today():
            return {}, digest, "resource_stale"
        return snapshot, digest, None
    finally:
        while descriptors:
            _close_fd(descriptors.pop())


def _load_snapshot() -> tuple[dict[str, Any], str, str | None]:
    try:
        return _load_snapshot_inner()
    except (RoutingError, OSError, ValueError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, NotImplementedError, RuntimeError):
        return {}, "0" * 64, "resource_invalid"


def _request_bytes_from_stdin() -> bytes:
    try:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        data = stream.read(MAX_REQUEST_BYTES + 1)
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        raise RoutingError("request input is unavailable") from None
    if isinstance(data, str):
        try:
            data = data.encode("utf-8")
        except UnicodeError:
            raise RoutingError("request input is invalid") from None
    if not isinstance(data, bytes) or len(data) > MAX_REQUEST_BYTES:
        raise RoutingError("request input is invalid")
    return data


def _request_bytes_from_file(value: str) -> bytes:
    try:
        observed = os.stat(value, follow_symlinks=False)
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        raise RoutingError("request file is unavailable") from None
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise RoutingError("request file is unavailable")
    if not 0 <= observed.st_size <= MAX_REQUEST_BYTES:
        raise RoutingError("request file is invalid")

    fd: int | None = None
    try:
        fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(fd)
        if (
            _identity(opened) != _identity(observed)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size != observed.st_size
            or not 0 <= opened.st_size <= MAX_REQUEST_BYTES
        ):
            raise RoutingError("request file changed during open")
        remaining = observed.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(MAX_REQUEST_BYTES, remaining))
            if not chunk:
                raise RoutingError("request file ended early")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise RoutingError("request file has trailing content")
        final = os.fstat(fd)
        if _identity(final) != _identity(observed) or final.st_size != observed.st_size:
            raise RoutingError("request file changed during read")
        return b"".join(chunks)
    except RoutingError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        raise RoutingError("request file is invalid") from None
    finally:
        if fd is not None:
            _close_fd(fd)


def _validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != REQUEST_KEYS:
        raise RoutingError("request shape is invalid")
    if value.get("schema") != REQUEST_SCHEMA or value.get("task_class") not in TASK_CLASSES or value.get("risk") not in RISK_ORDER:
        raise RoutingError("request is invalid")
    capabilities = value.get("required_capabilities")
    if not isinstance(capabilities, list) or any(not isinstance(item, str) or item not in CAPABILITIES for item in capabilities) or capabilities != sorted(set(capabilities)):
        raise RoutingError("request capabilities are invalid")
    return value


def _receipt(*, status: str, reason: str, digest: str, summary: str, selected: dict[str, str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "status": status,
        "reason": reason,
        "snapshot_sha256": digest,
        "selection_policy": "static_reviewed_only",
        "ultra_selected": False,
        "evidence_summary": summary,
    }
    if selected is not None:
        result["selected"] = selected
    return result


def _receipt_is_valid(value: Any) -> bool:
    base = {"schema", "status", "reason", "snapshot_sha256", "selection_policy", "ultra_selected", "evidence_summary"}
    if not isinstance(value, dict) or (set(value) != base and set(value) != base | {"selected"}):
        return False
    if value.get("schema") != RECEIPT_SCHEMA or value.get("selection_policy") != "static_reviewed_only" or value.get("ultra_selected") is not False:
        return False
    if not isinstance(value.get("snapshot_sha256"), str) or not SHA256_RE.fullmatch(value["snapshot_sha256"]):
        return False
    status, reason = value.get("status"), value.get("reason")
    pairs = {
        ("selected", "selected_static_reviewed"),
        ("rejected", "invalid_request"),
        ("rejected", "resource_invalid"),
        ("rejected", "resource_stale"),
        ("offline", "candidate_evidence_unknown"),
        ("offline", "capability_unsatisfied"),
        ("offline", "risk_floor_unsatisfied"),
        ("offline", "offline_no_eligible_candidate"),
    }
    if (status, reason) not in pairs:
        return False
    expected_summary = "static-reviewed-candidate" if status == "selected" else ("static-resource-validation" if status == "rejected" else "static-reviewed-no-eligible-candidate")
    if value.get("evidence_summary") != expected_summary:
        return False
    selected = value.get("selected")
    if status == "selected":
        return isinstance(selected, dict) and set(selected) == {"lane"} and selected.get("lane") in LANE_ORDER
    return selected is None and "selected" not in value


def _select(snapshot: dict[str, Any], request: dict[str, Any], digest: str) -> dict[str, Any]:
    candidates = snapshot["candidates"]
    task_candidates = [candidate for candidate in candidates if request["task_class"] in candidate["task_classes"]]
    risk_candidates = [candidate for candidate in task_candidates if RISK_ORDER[candidate["maximum_risk"]] >= RISK_ORDER[request["risk"]]]
    required = request["required_capabilities"]
    unknown = [candidate for candidate in risk_candidates if any(candidate["capabilities"].get(capability, {"value": "unknown"})["value"] == "unknown" for capability in required)]
    eligible = [candidate for candidate in risk_candidates if all(candidate["capabilities"].get(capability, {"value": "unknown"})["value"] == "true" for capability in required)]
    if eligible:
        winner = min(eligible, key=lambda candidate: (candidate["policy_priority"], LANE_ORDER[candidate["lane"]], candidate["model_id"]))
        return _receipt(status="selected", reason="selected_static_reviewed", digest=digest, summary="static-reviewed-candidate", selected={"lane": winner["lane"]})
    if unknown:
        reason = "candidate_evidence_unknown"
    elif risk_candidates:
        reason = "capability_unsatisfied"
    else:
        capability_candidates = [candidate for candidate in task_candidates if all(candidate["capabilities"].get(capability, {"value": "unknown"})["value"] == "true" for capability in required)]
        reason = "risk_floor_unsatisfied" if capability_candidates else "offline_no_eligible_candidate"
    return _receipt(status="offline", reason=reason, digest=digest, summary="static-reviewed-no-eligible-candidate")


def _read_request(value: str) -> Any:
    data = _request_bytes_from_stdin() if value == "-" else _request_bytes_from_file(value)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise RoutingError("request input is invalid") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("--request", required=True, metavar="JSON-file|-")
    args = parser.parse_args(argv)
    snapshot, digest, resource_error = _load_snapshot()
    if resource_error:
        receipt = _receipt(status="rejected", reason=resource_error, digest=digest, summary="static-resource-validation")
    else:
        try:
            request = _validate_request(_read_request(args.request))
        except RoutingError:
            receipt = _receipt(status="rejected", reason="invalid_request", digest=digest, summary="static-resource-validation")
        else:
            receipt = _select(snapshot, request, digest)
    if not _receipt_is_valid(receipt):
        receipt = _receipt(status="rejected", reason="resource_invalid", digest="0" * 64, summary="static-resource-validation")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
