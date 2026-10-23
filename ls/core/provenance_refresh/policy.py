"""Read-only adapter for the registered shared-policy stitcher owner."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


_MAX_OUTPUT_BYTES = 128 * 1024
_OWNER_TIMEOUT_SECONDS = 10.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_TARGET_STATUSES = {"noop", "would_create", "would_update", "refused"}


def inspect_policy_owner(
    owner_path: Path | None,
    registry_path: Path | None,
) -> dict[str, Any]:
    """Inspect the explicitly configured policy owner without writing state.

    The controller must bind both paths. This adapter does not discover the
    owner's private installation, bootstrap a missing registry, or infer that a
    running client loaded the rendered files. It executes only the owner's
    bounded read-only ``--check --json`` surface and projects validated fields.
    """
    if owner_path is None or registry_path is None:
        return _result("unavailable", reason="notconfigured")

    owner = Path(owner_path).expanduser()
    registry = Path(registry_path).expanduser()
    for candidate in (owner, registry):
        if candidate.is_symlink():
            return _result("refused", reason="symlink_path")
        if not candidate.exists():
            return _result("unavailable", reason="missing")
        if not candidate.is_file():
            return _result("refused", reason="not_regular_file")
        try:
            mode = candidate.stat().st_mode
        except OSError:
            return _result("unavailable", reason="missing")
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            return _result("refused", reason="writable_by_others")

    if owner.name != "policy_stitch.py" or owner.parent.name != "ops" or owner.parent.parent.name != "scripts":
        return _result("refused", reason="outside_owner_boundary")
    try:
        if owner.stat().st_size > 2 * 1024 * 1024 or registry.stat().st_size > 1024 * 1024:
            return _result("refused", reason="input_too_large")
        resolved_owner = owner.resolve(strict=True)
        resolved_registry = registry.resolve(strict=True)
    except OSError:
        return _result("unavailable", reason="missing")
    if (resolved_owner.name != "policy_stitch.py" or resolved_owner.parent.name != "ops"
            or resolved_owner.parent.parent.name != "scripts"):
        return _result("refused", reason="outside_owner_boundary")

    # The caller supplies the trusted owner and registry binding. The structural
    # owner boundary above prevents substituting an arbitrary script while
    # keeping the actual MyRig checkout path configurable.
    argv = [
        sys.executable,
        str(resolved_owner),
        "--registry",
        str(resolved_registry),
        "--check",
        "--json",
        "--target",
        "all",
    ]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1",
    }
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(resolved_owner.parent.parent.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError:
        return _result("invalid", reason="owner_launch_failed")

    stdout = bytearray()
    stderr = bytearray()
    output_limit = threading.Event()

    def drain(stream: Any, buffer: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                remaining = _MAX_OUTPUT_BYTES - len(buffer)
                if len(chunk) > remaining:
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    output_limit.set()
                    return
                buffer.extend(chunk)
        except (OSError, ValueError):
            return

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    def stop_owner() -> None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + _OWNER_TIMEOUT_SECONDS
    failure: str | None = None
    while process.poll() is None:
        if output_limit.is_set():
            failure = "output_limit"
            stop_owner()
            break
        if time.monotonic() >= deadline:
            failure = "timeout"
            stop_owner()
            break
        time.sleep(0.01)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        stop_owner()
        process.wait()
        failure = failure or "timeout"
    for reader in readers:
        reader.join(timeout=1.0)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    if output_limit.is_set():
        failure = "output_limit"
    if failure:
        return _result("invalid", reason=failure)

    try:
        report = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _result("invalid", reason="invalid_json")
    return _classify_report(report, process.returncode)


def _result(status: str, *, reason: str | None = None, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "loaded_by_client": "unverified"}
    if reason is not None:
        result["reason"] = reason
    result.update(fields)
    return result


def _classify_report(report: Any, returncode: int) -> dict[str, Any]:
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report.get("schema_version") != 1):
        return _result("invalid", reason="invalid_report")
    summary = report.get("summary")
    targets = report.get("targets")
    if not isinstance(summary, dict) or not isinstance(targets, list):
        return _result("invalid", reason="invalid_report")
    if any(type(summary.get(key)) is not int or summary[key] < 0
           for key in ("noop", "would_create", "would_update", "refused")):
        return _result("invalid", reason="invalid_summary")

    projected_targets: list[dict[str, Any]] = []
    for row in targets:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            return _result("invalid", reason="invalid_target")
        status = row.get("status")
        if status not in _KNOWN_TARGET_STATUSES:
            return _result("invalid", reason="invalid_target")
        projected: dict[str, Any] = {"id": row["id"], "status": status}
        action = row.get("action")
        if isinstance(action, str):
            projected["action"] = action
        for key in ("content_sha256", "head_sha256", "orientation_sha256"):
            value = row.get(key)
            if value is not None:
                if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                    return _result("invalid", reason="invalid_hash")
                projected[key] = value
        projected_targets.append(projected)

    global_info = report.get("global")
    if global_info is not None:
        if not isinstance(global_info, dict):
            return _result("invalid", reason="invalid_global")
        digest = global_info.get("content_sha256")
        lines = global_info.get("content_lines")
        if (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)
                or type(lines) is not int or lines < 0
                or not isinstance(global_info.get("source"), str)):
            return _result("invalid", reason="invalid_global")
        global_projection: dict[str, Any] = {
            "content_sha256": digest,
            "content_lines": lines,
        }
    else:
        global_projection = None

    orientation = report.get("orientation")
    orientation_projection = None
    if orientation is not None:
        if (not isinstance(orientation, dict)
                or not isinstance(orientation.get("heading"), str)
                or orientation.get("source") != "global"
                or orientation.get("status") not in {"ok", "invalid"}):
            return _result("invalid", reason="invalid_orientation")
        orientation_projection = {
            "heading": orientation["heading"],
            "source": "global",
            "status": orientation["status"],
        }
        if orientation["status"] == "ok":
            digest = orientation.get("content_sha256")
            lines = orientation.get("content_lines")
            if (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)
                    or type(lines) is not int or lines < 0):
                return _result("invalid", reason="invalid_orientation")
            orientation_projection.update({"content_sha256": digest, "content_lines": lines})
        elif orientation.get("reason") not in {"missing", "ambiguous"}:
            return _result("invalid", reason="invalid_orientation")

    has_error = isinstance(report.get("error"), str)
    actual_counts = {
        "noop": sum(row["status"] == "noop" for row in targets),
        "would_create": sum(row["status"] == "would_create" for row in targets),
        "would_update": sum(row["status"] == "would_update" for row in targets),
        "refused": sum(row["status"] == "refused" for row in targets),
    }
    if (not has_error and actual_counts != {key: summary[key] for key in actual_counts}) or (
        has_error and (returncode != 2 or summary["refused"] == 0)
    ):
        return _result("invalid", reason="inconsistent_summary")
    result_fields = {
        "summary": {key: summary[key] for key in ("noop", "would_create", "would_update", "refused")},
        "targets": projected_targets,
        "global": global_projection,
    }
    if orientation is not None:
        result_fields["orientation"] = orientation_projection

    if returncode == 2 and (has_error or summary["refused"] > 0
                            or any(row["status"] == "refused" for row in targets)):
        return _result("refused", reason="owner_refused", **result_fields)
    if returncode == 0 and global_projection is not None and targets and all(
        row["status"] == "noop" for row in targets
    ) and summary["refused"] == 0 and not has_error:
        return _result("aligned", **result_fields)
    if returncode == 1 and targets and any(
        row["status"] in {"would_create", "would_update"} for row in targets
    ) and summary["refused"] == 0 and not has_error:
        return _result("drift", reason="target_drift", **result_fields)
    return _result("invalid", reason="inconsistent_owner_report")
