"""One-off, controller-owned remote sudo without FleetOps host enrollment."""

from __future__ import annotations

import base64
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any
import uuid

from ..herdr_ops.errors import HerdrOpsError
from ..herdr_ops.transport import (
    atomic_write_json, read_json_state, request as native_request,
    state_directory, verify_local_herdr_endpoint, writer_lock,
)
from .backends import HerdrAdapter
from .controller import caller_owner
from .credentials import iter_candidates
from .models import PaneRequest, PaneTransition
from .pane import FilePaneStore, ManagedPaneService
from .remote_diagnostic_runtime import REMOTE_DIAGNOSTIC
from .remote_diagnostic_transport import DiagnosticSSH, IDENTITY, STAGE, canonical
from .remote_sudo_bundle import sudo_runtime_bundle
from .remote_sudo_channel import deliver_candidate
from .shell import _state_directory
from .sudo import payload_digest


def _identity(endpoint: str, workspace: str, parent: str, cwd: str) -> dict[str, Any]:
    verify_local_herdr_endpoint(endpoint)
    observed = native_request(endpoint, "pane.process_info", {"pane_id": parent})
    owner = caller_owner(observed, parent)
    if Path(f"/proc/{owner['pid']}/cwd").resolve() != Path(cwd).resolve():
        raise ValueError("controller working directory differs from the selected checkout")
    snapshot = native_request(endpoint, "session.snapshot", {})
    body = snapshot.get("snapshot", snapshot)
    panes = body.get("panes", [])
    if not any(isinstance(row, dict) and row.get("pane_id") == parent
               and row.get("workspace_id") == workspace for row in panes):
        raise ValueError("controller pane is outside the selected workspace")
    return owner


def _path(root: Path, binding_key: str, operation_id: str, *, create: bool = True) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("diagnostic operation ID must be 32 lowercase hex characters")
    return _state_directory(root, binding_key, create=create) / (operation_id + ".remote.json")


def _service(root: Path) -> ManagedPaneService:
    return ManagedPaneService(FilePaneStore(root), {"herdr": HerdrAdapter(state_dir=root)}, state_dir=root)


def _observe(transport: DiagnosticSSH, record: dict[str, Any]) -> dict[str, Any]:
    return transport.read(["python3", "-c", "exec(" + repr(REMOTE_DIAGNOSTIC) + ")",
                           record["remote_root"], "observe", record["claim_sha256"]])


def _accept_receipt(record: dict[str, Any], observed: Mapping[str, Any]) -> bool:
    receipt = observed.get("receipt")
    if receipt is None:
        return False
    if (not isinstance(receipt, dict)
            or receipt.get("claim_sha256") != record["claim_sha256"]
            or receipt.get("operation_id") != record["operation_id"]):
        raise ValueError("remote diagnostic receipt does not match its original claim")
    sudo = receipt.get("sudo")
    remote = record["claim"]["remote"]
    expected = {"operation_id": record["operation_id"], "target": record["target"],
                "owner_id": remote["session"], "endpoint_id": remote["endpoint"],
                "live_owner_id": remote["process_generation"]}
    if not isinstance(sudo, dict) or any(sudo.get(key) != value for key, value in expected.items()):
        raise ValueError("remote sudo receipt identity is unavailable")
    record["receipt"] = receipt
    record["state"] = receipt.get("state", "unknown")
    return True


def status_diagnostic(*, state_dir: str, binding_key: str, operation_id: str) -> dict[str, Any]:
    """Read the original claim/receipt only; never dispatch or supply a credential."""
    root = state_directory(state_dir, create=False)
    path = _path(root, binding_key, operation_id, create=False)
    record = read_json_state(path)
    if record.get("operation_id") != operation_id or record.get("binding_key") != binding_key:
        raise ValueError("diagnostic local receipt identity mismatch")
    if record.get("state") == "completed":
        return record
    transport = DiagnosticSSH(record["target"])
    if transport.settings != record["ssh_settings"]:
        raise ValueError("diagnostic SSH target configuration changed")
    observed = _observe(transport, record)
    if _accept_receipt(record, observed):
        atomic_write_json(path, record)
    return record


def run_diagnostic(*, endpoint: str, workspace: str, parent: str, cwd: str,
                   target: str, argv: Sequence[str], operation_id: str,
                   state_dir: str, timeout: float = 120, remote_cwd: str = "/",
                   environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Dispatch exactly once through a qualified local protected operations pane."""
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 10 <= timeout <= 600:
        raise ValueError("diagnostic timeout must be between 10 and 600 seconds")
    if (not isinstance(argv, (tuple, list)) or not argv or any(
            not isinstance(part, str) or "\0" in part for part in argv)
            or not argv[0] or len(canonical(list(argv))) > 65536):
        raise ValueError("diagnostic requires bounded exact NUL-free argv")
    remote_path = PurePosixPath(remote_cwd)
    if not remote_path.is_absolute() or ".." in remote_path.parts:
        raise ValueError("remote diagnostic working directory must be absolute")
    owner = _identity(endpoint, workspace, parent, cwd)
    controller_id = hashlib.sha256(canonical(owner)).hexdigest()
    pane_request = PaneRequest(backend="herdr", endpoint=endpoint,
        repository=Path(cwd).name.lower(), controller_id=controller_id,
        parent_id=parent, workspace_id=workspace, cwd=cwd)
    root = state_directory(state_dir)
    path = _path(root, pane_request.binding_key, operation_id)
    if path.exists() or path.is_symlink():
        raise ValueError("diagnostic ID already exists; use read-only status, never replay")
    service = _service(root)
    ready = service.ensure(pane_request)
    if ready.state != "ready" or ready.binding is None:
        raise ValueError("protected operations pane is not verified idle")
    bound_pane = ready.binding
    transport = DiagnosticSSH(target)
    remote_identity = transport.read(["python3", "-c", "exec(" + repr(IDENTITY) + ")"])
    state_home = PurePosixPath(remote_identity["state_home"])
    if not state_home.is_absolute() or ".." in state_home.parts:
        raise ValueError("remote state home must be an absolute path")
    remote_root = str(state_home / "localsetup" / "terminal_ops" /
                      pane_request.binding_key / (operation_id + ".remote"))
    runtime_b64, runtime_sha256 = sudo_runtime_bundle()
    claim: dict[str, Any] = {
        "operation_id": operation_id, "argv": list(argv), "cwd": remote_cwd,
        "timeout": timeout, "payload_sha256": payload_digest(argv),
        "runner_sha256": hashlib.sha256(("exec(" + repr(REMOTE_DIAGNOSTIC) + ")").encode()).hexdigest(),
        "remote": {"session": controller_id, "endpoint": transport.endpoint_digest(),
                   "process_generation": remote_identity["generation"]},
        "sudo": {"nonce": uuid.uuid4().hex, "runtime_b64": runtime_b64,
                 "runtime_sha256": runtime_sha256, "target": target,
                 "binding_key": pane_request.binding_key, "single_password_qualified": False},
    }
    record: dict[str, Any] = {"schema": 1, "operation_id": operation_id,
              "binding_key": pane_request.binding_key, "target": target,
              "ssh_settings": transport.settings, "owner": owner,
              "pane_request": pane_request.to_dict(), "local_binding": ready.binding.to_dict(),
              "remote_root": remote_root, "claim": claim,
              "claim_sha256": hashlib.sha256(canonical(claim)).hexdigest(), "state": "staging"}
    with writer_lock(root):
        if path.exists() or path.is_symlink():
            raise ValueError("diagnostic ID already exists; use read-only status, never replay")
        atomic_write_json(path, record)
    try:
        staged = transport.read(["python3", "-c", "exec(" + repr(STAGE) + ")", remote_root], stdin=canonical({"claim": claim}))
        if staged.get("claim_sha256") != record["claim_sha256"]:
            raise ValueError("remote staged claim digest mismatch")
        if _identity(endpoint, workspace, parent, cwd) != owner:
            raise ValueError("native diagnostic controller changed before dispatch")
        remote_command = transport.command(target, ["python3", "-c", "exec(" + repr(REMOTE_DIAGNOSTIC) + ")",
                                                     remote_root, "run", record["claim_sha256"]])
        record["state"] = "dispatching"
        atomic_write_json(path, record)
        service.transition(pane_request, PaneTransition(operation_id, tuple(remote_command), cwd=cwd))
        record["state"] = "running"
        atomic_write_json(path, record)
        deadline = time.monotonic() + timeout
        candidates = iter(iter_candidates(target, os.environ if environment is None else environment))
        served: set[str] = set()

        def authentication_current() -> bool:
            if time.monotonic() >= deadline:
                return False
            fresh = _observe(transport, record)
            if (fresh.get("wrapper_current") is not True
                    or fresh.get("challenge") != "GENERIC_SUDO_KEY"
                    or fresh.get("claim_sha256") != record["claim_sha256"]):
                return False
            current = service.inspect(pane_request)
            if (current.binding is None or current.binding.pane_id != bound_pane.pane_id
                    or current.binding.shell_pid != bound_pane.shell_pid
                    or current.operation_id != operation_id):
                return False
            return (_identity(endpoint, workspace, parent, cwd) == owner
                    and time.monotonic() < deadline)

        while time.monotonic() < deadline:
            observed = _observe(transport, record)
            if _accept_receipt(record, observed):
                service.reconcile(pane_request, operation_id)
                atomic_write_json(path, record)
                return record
            challenge = observed.get("challenge")
            if challenge is not None and challenge not in served:
                if (challenge != "GENERIC_SUDO_KEY" or observed.get("wrapper_current") is not True
                        or observed.get("claim_sha256") != record["claim_sha256"]
                        or _identity(endpoint, workspace, parent, cwd) != owner):
                    raise ValueError("remote authentication challenge is not current")
                current = service.inspect(pane_request)
                if (current.binding is None or current.binding.pane_id != bound_pane.pane_id
                        or current.binding.shell_pid != bound_pane.shell_pid
                        or current.operation_id != operation_id):
                    raise ValueError("protected pane no longer owns this diagnostic")
                candidate = next(candidates, None)
                credential = candidate.value if candidate is not None and candidate.variable == challenge else None
                served.add(challenge)
                if not deliver_candidate(transport, target, claim["sudo"]["nonce"], credential,
                        environment=os.environ if environment is None else environment,
                        deadline=deadline, validate_current=authentication_current):
                    raise ValueError("private credential delivery outcome is unknown")
                credential = None
                candidate = None
            time.sleep(0.1)
        record["state"] = "unknown"
        atomic_write_json(path, record)
        return record
    except Exception:
        record["state"] = "unknown"
        atomic_write_json(path, record)
        raise HerdrOpsError("remote_diagnostic_unknown",
            "Diagnostic outcome requires read-only status; do not replay the operation.", "unknown") from None
    finally:
        candidates_value = locals().get("candidates")
        if candidates_value is not None and hasattr(candidates_value, "close"):
            candidates_value.close()


def public_result(record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose bounded workload output and safe receipts, not runtime or claim data."""
    result = {key: record[key] for key in ("schema", "operation_id", "binding_key", "target", "state")}
    receipt = record.get("receipt")
    if isinstance(receipt, dict):
        result["sudo"] = receipt.get("sudo")
        result["exit_code"] = receipt.get("exit_code")
        for stream in ("stdout", "stderr"):
            encoded = receipt.get(stream + "_b64", "")
            if not isinstance(encoded, str) or len(encoded) > 22500:
                raise ValueError("diagnostic output exceeds its encoded bound")
            data = base64.b64decode(encoded, validate=True)
            if len(data) > 16384:
                raise ValueError("diagnostic output exceeds its byte bound")
            decoded = data.decode("utf-8", errors="replace")
            bounded = decoded.encode("utf-8")[-16384:].decode("utf-8", errors="ignore")
            lines = bounded.splitlines(keepends=True)
            result[stream] = "".join(lines[-100:])
            result[stream + "_truncated"] = (receipt.get(stream + "_truncated", False)
                or bounded != decoded or len(lines) > 100)
    return result
