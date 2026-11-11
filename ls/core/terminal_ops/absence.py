"""Retire a proven absent local generation without resolving its command outcome."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .models import PaneBinding, PaneError, PaneRequest
from .shell import _operation_guard, _read_json, _read_operation, _state_directory
from ..herdr_ops.errors import HerdrOpsError

if TYPE_CHECKING:
    from .pane import ManagedPaneService

_FIELDS = (
    "binding_key", "pane_id", "shell_pid", "pane_tty", "shell_path",
    "machine_generation", "endpoint", "controller_id",
)


def _scope(request: PaneRequest, stored: dict[str, Any]) -> PaneBinding:
    if (request.backend != "herdr" or stored.get("request") != request.to_dict()
            or stored.get("binding_key") != request.binding_key):
        raise PaneError("absence_scope_mismatch", "The original Herdr request changed.")
    binding = PaneBinding.from_dict(stored.get("last_binding"))
    fields = ("backend", "binding_key", "endpoint", "parent_id", "repository",
              "controller_id", "host_alias", "workspace_id", "session_id", "cwd")
    if (binding.state not in {"ready", "busy"}
            or any(getattr(binding, key) != getattr(request, key) for key in fields)):
        raise PaneError("absence_scope_mismatch", "The original pane generation changed.")
    return binding


def _operation(service: ManagedPaneService, request: PaneRequest, stored: dict[str, Any],
               binding: PaneBinding, operation_id: str) -> dict[str, Any]:
    _, operation = _read_operation(str(service.state_dir), request.binding_key, operation_id)
    pending = stored.get("operation")
    transition = operation.get("transition")
    if (not isinstance(pending, dict) or pending.get("operation_id") != operation_id
            or operation.get("operation_id") != operation_id
            or any(operation.get(key) != binding.to_dict().get(key) for key in _FIELDS)
            or any(pending.get(key) != operation.get(key) for key in
                   (*_FIELDS, "operation_id", "lease_token", "transition", "working_directory", "target"))
            or operation.get("target") != (request.host_alias or request.repository)
            or operation.get("working_directory") != request.cwd
            or operation.get("state") not in {"running", "unknown"}
            or not isinstance(transition, dict) or transition.get("kind") != "command"
            or transition.get("requires_sudo_receipt") is not False
            or not isinstance(operation.get("lease_token"), str)
            or not operation["lease_token"]):
        raise PaneError("absence_operation_mismatch", "The exact non-sudo operation receipt changed.")
    return operation


def _lease(path: Path, request: PaneRequest, operation: dict[str, Any]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_dir():
        raise PaneError("absence_lease_mismatch", "The exact writer lease is unavailable.")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PaneError("absence_lease_mismatch", "The writer lease is not owner-private.")
    value = _read_json(path / "lease.json")
    if (value.get("binding_key") != request.binding_key
            or value.get("operation_id") != operation["operation_id"]
            or value.get("token") != operation["lease_token"]
            or {item.name for item in path.iterdir()} != {"lease.json"}):
        raise PaneError("absence_lease_mismatch", "The writer lease does not belong to the original operation.")
    return value


def _archive(service: ManagedPaneService, request: PaneRequest, raw: bytes) -> dict[str, str]:
    from ..herdr_ops.layout import _archive_owner_bytes
    try:
        path, digest = _archive_owner_bytes(service.state_dir, request.binding_key[:32], raw)
    except HerdrOpsError as error:
        raise PaneError(error.code, error.message) from error
    return {"path": path, "sha256": digest}


def _archived(service: ManagedPaneService, request: PaneRequest, item: Any) -> dict[str, Any]:
    from ..herdr_ops.layout import _read_archive_file
    if not isinstance(item, dict):
        raise PaneError("absence_archive_invalid", "An original generation archive is unavailable.")
    digest = item.get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or item.get("path") != f"panes/archive/{request.binding_key[:32]}/{digest}.json"):
        raise PaneError("absence_archive_invalid", "An original generation archive has invalid scope.")
    try:
        raw = _read_archive_file(service.state_dir / item["path"])
        if hashlib.sha256(raw).hexdigest() != digest:
            raise PaneError("absence_archive_invalid", "An original generation archive changed.")
        value = json.loads(raw)
    except (HerdrOpsError, OSError, ValueError) as error:
        raise PaneError("absence_archive_invalid", "An original generation archive cannot be verified.") from error
    if not isinstance(value, dict):
        raise PaneError("absence_archive_invalid", "An original generation archive is malformed.")
    return value


def _historical_creation_retirement(service: ManagedPaneService, request: PaneRequest,
                                    marker: Any, binding: PaneBinding) -> None:
    """Accept only a settled, archived predecessor of this owned generation."""
    from .backends import _managed_owner_metadata
    from ..herdr_ops.layout import _retirement_owner

    if (not isinstance(marker, dict) or marker.get("state") != "retired"
            or not isinstance(marker.get("creation_token"), str)
            or len(marker["creation_token"]) != 32
            or any(c not in "0123456789abcdef" for c in marker["creation_token"])
            or not isinstance(marker.get("retirement_id"), str) or not marker["retirement_id"]
            or not isinstance(marker.get("observation"), dict)
            or any(not isinstance(marker.get(k), str) or not marker[k]
                   for k in ("pane_id", "terminal_id"))
            or marker["pane_id"] == binding.pane_id
            or marker["terminal_id"] == binding.terminal_id):
        raise PaneError("absence_scope_mismatch", "Another generation transition is reserved or unresolved.")
    owner = _archived(service, request, {"path": marker.get("owner_archive"),
                                         "sha256": marker.get("owner_sha256")})
    reservation = _archived(service, request, {"path": marker.get("reservation_archive"),
                                               "sha256": marker.get("reservation_sha256")})
    try:
        current = _retirement_owner(service.state_dir, request.endpoint, request.binding_key[:32],
                                    request.workspace_id or "", request.parent_id, request.cwd,
                                    _managed_owner_metadata(request), binding.pane_id,
                                    binding.terminal_id)
    except HerdrOpsError as error:
        raise PaneError("absence_scope_mismatch", "The current managed owner cannot prove its predecessor.") from error
    previous_marker = dict(marker)
    previous_marker.pop("creation_token")
    if (current.get("state") != "owned" or current.get("retired_creation") != previous_marker
            or owner.get("state") != "owned" or owner.get("retirement") is not None
            or any(owner.get(k) != current.get(k) for k in
                   ("schema_version", "owner_id", "purpose", "endpoint", "workspace_id",
                    "caller_pane_id", "cwd", "ratio", "owner_metadata"))
            or owner.get("pane_id") != marker["pane_id"]
            or owner.get("terminal_id") != marker["terminal_id"]
            or reservation.get("binding_key") != request.binding_key
            or reservation.get("request") != request.to_dict()
            or reservation.get("creation") != {"state": "reserved", "token": marker["creation_token"]}
            or reservation.get("retirement") is not None
            or reservation.get("operation") is not None or reservation.get("last_binding") is not None
            or not isinstance(reservation.get("status"), dict)
            or reservation["status"].get("binding") is not None
            or reservation["status"].get("operation_id") is not None):
        raise PaneError("absence_scope_mismatch", "The historical retirement proof does not match its successor.")


def retirement_proof(service: ManagedPaneService, request: PaneRequest, stored: dict[str, Any],
                     *, final: bool = True) -> tuple[PaneBinding, dict[str, Any]]:
    """Validate a local-only retirement; never infer that its command succeeded."""
    binding = _scope(request, stored)
    marker = stored.get("absent_retirement")
    if (not isinstance(marker, dict) or marker.get("state") not in
            ({"retired"} if final else {"retiring", "retired"})
            or marker.get("pane_id") != binding.pane_id
            or marker.get("terminal_id") != binding.terminal_id
            or marker.get("operation_outcome") != "unknown"):
        raise PaneError("absence_fence_invalid", "The exact absent-generation fence is unavailable.")
    operation_id = marker.get("operation_id")
    if not isinstance(operation_id, str):
        raise PaneError("absence_fence_invalid", "The retirement operation identity is unavailable.")
    operation = _operation(service, request, stored, binding, operation_id)
    archives = marker.get("archives")
    if not isinstance(archives, dict):
        raise PaneError("absence_archive_invalid", "The original generation archives are unavailable.")
    original = _archived(service, request, archives.get("store"))
    if (_scope(request, original).to_dict() != binding.to_dict()
            or _archived(service, request, archives.get("operation")) != operation):
        raise PaneError("absence_archive_invalid", "The archived generation or operation changed.")
    _operation(service, request, original, binding, operation_id)
    directory = _state_directory(service.state_dir, request.binding_key, create=False)
    lease = directory / "writer-lease"
    retired = directory / ("retired-lease-" + operation_id)
    if final:
        if lease.exists() or lease.is_symlink():
            raise PaneError("absence_lease_mismatch", "A writer lease still fences this binding.")
        if _lease(retired, request, operation) != _archived(service, request, archives.get("lease")):
            raise PaneError("absence_archive_invalid", "The retired writer lease changed.")
    return binding, operation


def retire_absent(service: ManagedPaneService, request: PaneRequest, *,
                  expected_pane_id: str, expected_operation_id: str) -> dict[str, Any]:
    """Fence one missing local shell; descendants and command outcome stay unknown.

    Application callers must settle their actual workload separately. This never
    sends input, closes a pane, signals a process, or replays an operation.
    """
    from ..herdr_ops.layout import _fsync_directory
    if not isinstance(expected_pane_id, str) or not isinstance(expected_operation_id, str):
        raise PaneError("absence_scope_mismatch", "Exact pane and operation identities are required.")

    with service._lock(request.binding_key):
        stored, raw = service._retirement_store(request)
        if stored.get("creation") is not None:
            raise PaneError("absence_scope_mismatch", "Another generation transition is reserved.")
        binding = _scope(request, stored)
        if stored.get("retirement") is not None:
            _historical_creation_retirement(service, request, stored["retirement"], binding)
        if binding.pane_id != expected_pane_id:
            raise PaneError("absence_scope_mismatch", "The expected original pane changed.")
        directory = _state_directory(service.state_dir, request.binding_key, create=False)
        with _operation_guard(directory):
            operation = _operation(service, request, stored, binding, expected_operation_id)
            native = service._adapter(request).inspect(request, previous_binding=binding)
            if native.state != "missing":
                raise PaneError("absence_not_proven", "The exact original native generation is not proven absent.")
            lease = directory / "writer-lease"
            retired = directory / ("retired-lease-" + expected_operation_id)
            marker = stored.get("absent_retirement")
            if marker is None:
                lease_value = _lease(lease, request, operation)
                if retired.exists() or retired.is_symlink():
                    raise PaneError("absence_lease_mismatch", "A different retired lease already exists.")
                # All original bytes are retained before fencing or moving the lease.
                marker = {
                    "state": "retiring", "pane_id": binding.pane_id,
                    "terminal_id": binding.terminal_id, "operation_id": expected_operation_id,
                    "operation_outcome": "unknown",
                    "archives": {
                        "store": _archive(service, request, raw),
                        "operation": _archive(service, request, json.dumps(operation, sort_keys=True).encode()),
                        "lease": _archive(service, request, json.dumps(lease_value, sort_keys=True).encode()),
                    },
                }
                stored["absent_retirement"] = marker
                service._put(request.binding_key, stored)
            else:
                retirement_proof(service, request, stored, final=False)
                if marker.get("operation_id") != expected_operation_id:
                    raise PaneError("absence_operation_mismatch", "A different generation retirement is fenced.")
            if service._adapter(request).inspect(request, previous_binding=binding).state != "missing":
                raise PaneError("absence_not_proven", "The original generation absence changed.")
            if lease.exists() or lease.is_symlink():
                _lease(lease, request, operation)
                if retired.exists() or retired.is_symlink():
                    raise PaneError("absence_lease_mismatch", "Both active and retired leases exist.")
                os.rename(lease, retired)
                _fsync_directory(directory)
            elif not retired.exists():
                raise PaneError("absence_lease_mismatch", "The original writer lease cannot be reconciled.")
            marker = dict(marker, state="retired")
            stored["absent_retirement"] = marker
            retirement_proof(service, request, stored)
            service._put(request.binding_key, stored)
            return {"api_version": 1, "state": "retired", "binding_key": request.binding_key,
                    "pane_id": binding.pane_id, "operation_id": expected_operation_id,
                    "operation_outcome": "unknown", "native_absence_verified": True,
                    "process_exit_claimed": False, "replayed": False}
