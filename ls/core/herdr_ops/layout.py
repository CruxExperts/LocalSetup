"""Owned temporary pane allocation, movement, resizing, and cleanup."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time
from typing import Any, Mapping
import uuid

from .errors import HerdrOpsError
from .transport import atomic_write_json, owner_id as validate_owner_id, read_json_state, request, state_directory, writer_lock


DEFAULT_RATIO = 0.3
_PROTECTED_LABEL = "sudo"
_DIRECTIONS = {"up", "down", "left", "right"}


def _required_text(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value or any(ord(ch) < 32 for ch in value):
        raise HerdrOpsError("target_required", f"An explicit {name} is required.")
    return value


def _endpoint_identity(endpoint: str | os.PathLike[str]) -> str:
    return str(Path(endpoint).expanduser().absolute())


def _check_workspace(value: Mapping[str, Any], workspace_id: str, *, field: str = "pane") -> None:
    if value.get("workspace_id") != workspace_id:
        raise HerdrOpsError("scope_mismatch", f"The {field} is outside the explicitly selected workspace.")


def _managed_ops_owner(endpoint: str, pane_id: str, state_dir: str | os.PathLike[str] | None) -> dict[str, Any] | None:
    try:
        root = state_directory(state_dir, create=False)
    except HerdrOpsError as exc:
        if exc.code == "state_dir_unavailable":
            return None
        raise
    owners = root / "panes"
    if not owners.exists():
        return None
    if owners.is_symlink() or not owners.is_dir():
        raise HerdrOpsError("unsafe_owner_state", "Managed pane ownership state is not a safe directory.")
    for path in owners.glob("*.json"):
        if path.is_symlink():
            raise HerdrOpsError("unsafe_owner_state", "Managed pane ownership state contains a symbolic link.")
        record = read_json_state(path)
        if (
            record.get("purpose") == "managed_ops"
            and record.get("endpoint") == _endpoint_identity(endpoint)
            and record.get("pane_id") == pane_id
            and record.get("state") in {"owned", "creating", "unknown"}
        ):
            return record
    return None


def get_pane(
    endpoint: str,
    pane_id: str,
    *,
    workspace_id: str | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    allow_managed_ops: bool = False,
) -> dict[str, Any]:
    pane_id = _required_text(pane_id, "pane ID")
    result = request(endpoint, "pane.get", {"pane_id": pane_id})
    pane = result.get("pane")
    if not isinstance(pane, dict) or pane.get("pane_id") != pane_id:
        raise HerdrOpsError("invalid_response", "Herdr did not return the requested pane.")
    if workspace_id is not None:
        _check_workspace(pane, workspace_id)
    if pane.get("label") == _PROTECTED_LABEL:
        raise HerdrOpsError("protected_pane", "The sudo pane is reserved for its controller-owned workflow.")
    if not allow_managed_ops and _managed_ops_owner(endpoint, pane_id, state_dir) is not None:
        raise HerdrOpsError("protected_pane", "This managed operations pane is reserved for its controller-owned workflow.")
    return pane


def inspect_pane(
    endpoint: str,
    workspace_id: str,
    pane_id: str,
    *,
    state_dir: str | os.PathLike[str] | None = None,
    allow_managed_ops: bool = False,
) -> dict[str, Any]:
    workspace_id = _required_text(workspace_id, "workspace ID")
    pane = get_pane(endpoint, pane_id, workspace_id=workspace_id, state_dir=state_dir, allow_managed_ops=allow_managed_ops)
    layout_result = request(endpoint, "pane.layout", {"pane_id": pane_id})
    layout = layout_result.get("layout")
    if not isinstance(layout, dict):
        raise HerdrOpsError("invalid_response", "Herdr did not return pane layout data.")
    _check_workspace(layout, workspace_id, field="layout")
    return {"pane": pane, "layout": layout}


def _owner_path(root: Path, identity: str) -> Path:
    return root / "panes" / f"{validate_owner_id(identity)}.json"


def _owner_record(root: Path, identity: str) -> dict[str, Any]:
    record = read_json_state(_owner_path(root, identity))
    if record.get("owner_id") != identity:
        raise HerdrOpsError("ownership_mismatch", "Pane owner metadata does not match the requested operation.")
    return record


def _write_owner(root: Path, owner: Mapping[str, Any]) -> None:
    atomic_write_json(_owner_path(root, str(owner["owner_id"])), owner)


def create_owned_pane(
    endpoint: str,
    workspace_id: str,
    caller_pane_id: str,
    cwd: str | os.PathLike[str],
    *,
    state_dir: str | os.PathLike[str] | None = None,
    owner_id: str | None = None,
    purpose: str = "pane",
    ratio: float = DEFAULT_RATIO,
    owner_metadata: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
    _reservation: bool = False,
) -> dict[str, Any]:
    """Create a non-focused sibling directly below an explicit caller pane."""
    workspace_id = _required_text(workspace_id, "workspace ID")
    caller_pane_id = _required_text(caller_pane_id, "caller pane ID")
    working_directory = Path(cwd).expanduser()
    if not working_directory.is_absolute() or any(ord(ch) < 32 or ord(ch) == 127 for ch in str(working_directory)):
        raise HerdrOpsError("invalid_cwd", "A temporary pane requires an explicit absolute working directory.")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.05 <= ratio <= 0.8:
        raise HerdrOpsError("invalid_ratio", "The temporary pane ratio must be between 0.05 and 0.8.")
    identity = validate_owner_id(owner_id or uuid.uuid4().hex)
    if purpose == "managed_ops" and not isinstance(owner_metadata, Mapping):
        raise HerdrOpsError("invalid_owner_metadata", "Managed operations panes require exact controller scope metadata.")
    if environment is not None and any(not isinstance(key, str) or not key or not isinstance(value, str) or "\x00" in value for key, value in environment.items()):
        raise HerdrOpsError("invalid_environment", "Pane environment values must be safe strings.")
    root = state_directory(state_dir)
    with writer_lock(root):
        owner_path = _owner_path(root, identity)
        if owner_path.is_symlink():
            raise HerdrOpsError("owner_conflict", "The requested owner ID has unsafe existing state.")
        if owner_path.exists():
            existing = _owner_record(root, identity)
            reserved = (
                _reservation
                and existing.get("purpose") == purpose
                and existing.get("state") == "creating"
                and existing.get("endpoint") == _endpoint_identity(endpoint)
                and existing.get("workspace_id") == workspace_id
                and existing.get("caller_pane_id") == caller_pane_id
                and existing.get("cwd") == str(working_directory)
                and existing.get("ratio") == float(ratio)
            )
            if not reserved:
                raise HerdrOpsError("owner_conflict", "The requested owner ID already exists; refusing to overwrite it.")
        caller = get_pane(endpoint, caller_pane_id, workspace_id=workspace_id, state_dir=root)
        before = request(endpoint, "pane.layout", {"pane_id": caller_pane_id}).get("layout")
        if not isinstance(before, dict) or before.get("tab_id") != caller.get("tab_id"):
            raise HerdrOpsError("invalid_response", "Herdr did not return the caller pane's current layout.")
        result = request(
            endpoint,
            "pane.split",
            {
                "workspace_id": workspace_id,
                "target_pane_id": caller_pane_id,
                "direction": "down",
                "ratio": float(ratio),
                "cwd": str(working_directory),
                "focus": False,
                **({"env": dict(environment)} if environment is not None else {}),
            },
            mutation=True,
        )
        pane = result.get("pane")
        if not isinstance(pane, dict):
            raise HerdrOpsError("split_unconfirmed", "Herdr did not identify the created pane; reconcile before retrying.", "unknown")
        new_pane_id = pane.get("pane_id")
        terminal_id = pane.get("terminal_id")
        if not isinstance(new_pane_id, str) or not new_pane_id or not isinstance(terminal_id, str) or not terminal_id:
            raise HerdrOpsError("split_unconfirmed", "Herdr returned incomplete pane identity; reconcile before retrying.", "unknown")
        _check_workspace(pane, workspace_id, field="created pane")
        owner = {
            "schema_version": 1,
            "owner_id": identity,
            "purpose": purpose,
            "endpoint": _endpoint_identity(endpoint),
            "workspace_id": workspace_id,
            "caller_pane_id": caller_pane_id,
            "pane_id": new_pane_id,
            "terminal_id": terminal_id,
            "tab_id": pane.get("tab_id"),
            "cwd": str(working_directory),
            "ratio": float(ratio),
            "before_layout": before,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "state": "owned",
        }
        if owner_metadata is not None:
            owner["owner_metadata"] = dict(owner_metadata)
        try:
            _write_owner(root, owner)
        except HerdrOpsError:
            raise HerdrOpsError("owner_receipt_uncertain", "The pane was created but its owner receipt could not be written; reconcile before retrying.", "unknown") from None
        return {"owner_id": identity, "pane": pane, "state": "owned"}


def _idle_signature(endpoint: str, pane: Mapping[str, Any]) -> str:
    """Fingerprint an owned pane only when its shell is observably idle."""
    if pane.get("agent") or pane.get("agent_session") or pane.get("agent_status") not in {None, "idle", "unknown"}:
        raise HerdrOpsError("pane_busy", "The owned pane has an attached or non-idle agent state.")
    pane_id = pane.get("pane_id")
    if pane.get("agent_status") == "unknown":
        snapshot = request(endpoint, "session.snapshot", {})
        body = snapshot.get("snapshot", snapshot.get("result", snapshot))
        if isinstance(body, dict) and isinstance(body.get("snapshot"), dict):
            body = body["snapshot"]
        agents = body.get("agents") if isinstance(body, dict) else None
        if not isinstance(agents, list) or any(not isinstance(item, dict) or item.get("pane_id") == pane_id for item in agents):
            raise HerdrOpsError("pane_busy", "Could not prove the owned pane has no attached agent.")
    process_result = request(endpoint, "pane.process_info", {"pane_id": pane_id})
    process = process_result.get("process_info")
    shell_pid = process.get("shell_pid") if isinstance(process, dict) else None
    foreground = process.get("foreground_processes") if isinstance(process, dict) else None
    if not isinstance(shell_pid, int) or shell_pid <= 0 or not isinstance(foreground, list):
        raise HerdrOpsError("pane_busy", "Could not verify that the owned pane is at an idle shell prompt.")
    if any(not isinstance(item, dict) or item.get("pid") != shell_pid for item in foreground):
        raise HerdrOpsError("pane_busy", "The owned pane has a foreground process and cannot be reused.")
    read_result = request(endpoint, "pane.read", {"pane_id": pane_id, "source": "recent", "lines": 80})
    read = read_result.get("read")
    text = read.get("text") if isinstance(read, dict) else None
    if not isinstance(text, str):
        raise HerdrOpsError("pane_busy", "Could not verify the owned pane's current prompt state.")
    identity = {"pane_id": pane_id, "shell_pid": shell_pid, "revision": pane.get("revision"), "text": text}
    return hashlib.sha256(repr(sorted(identity.items())).encode("utf-8")).hexdigest()


def _new_pane_idle_signature(endpoint: str, pane: Mapping[str, Any]) -> str:
    deadline = time.monotonic() + 10.0
    while True:
        try:
            return _idle_signature(endpoint, pane)
        except HerdrOpsError as exc:
            if exc.code != "pane_busy" or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def ensure_owned_pane(
    endpoint: str,
    workspace_id: str,
    caller_pane_id: str,
    cwd: str | os.PathLike[str],
    owner_id: str,
    *,
    state_dir: str | os.PathLike[str] | None = None,
    ratio: float = DEFAULT_RATIO,
    purpose: str = "pane",
    owner_metadata: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create a stable owned pane once, or return it only if its idle baseline is unchanged."""
    identity = validate_owner_id(owner_id)
    working_directory = Path(cwd).expanduser()
    if not working_directory.is_absolute() or any(ord(ch) < 32 or ord(ch) == 127 for ch in str(working_directory)):
        raise HerdrOpsError("invalid_cwd", "A stable owned pane requires an explicit absolute working directory.")
    root = state_directory(state_dir)
    path = _owner_path(root, identity)
    with writer_lock(root):
        if path.is_symlink():
            raise HerdrOpsError("owner_conflict", "The stable owner ID has unsafe existing state.")
        if not path.exists():
            reservation = {
                "schema_version": 1,
                "owner_id": identity,
                "purpose": purpose,
                "endpoint": _endpoint_identity(endpoint),
                "workspace_id": workspace_id,
                "caller_pane_id": caller_pane_id,
                "cwd": str(working_directory),
                "ratio": float(ratio),
                **({"owner_metadata": dict(owner_metadata)} if owner_metadata is not None else {}),
                "state": "creating",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_owner(root, reservation)
            try:
                created = create_owned_pane(
                    endpoint,
                    workspace_id,
                    caller_pane_id,
                    working_directory,
                    state_dir=root,
                    owner_id=identity,
                    purpose=purpose,
                    ratio=ratio,
                    owner_metadata=owner_metadata,
                    environment=environment,
                    _reservation=True,
                )
            except HerdrOpsError as exc:
                reservation.update({"state": "unknown" if exc.effect == "unknown" else "failed", "error": exc.as_dict()})
                _write_owner(root, reservation)
                raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", exc.effect) from None
            owner, pane = _load_owned(endpoint, root, identity, allow_managed_ops=purpose == "managed_ops")[:2]
            if pane is None:
                raise HerdrOpsError("owned_pane_missing", f"Herdr created the pane but it is no longer available. Stable owner ID: {identity}.", "unknown")
            if purpose == "managed_ops":
                if owner.get("owner_metadata") != dict(owner_metadata or {}):
                    raise HerdrOpsError("owner_scope_mismatch", "The managed pane owner metadata does not match its exact controller scope.")
            else:
                try:
                    owner["idle_baseline"] = _new_pane_idle_signature(endpoint, pane)
                except HerdrOpsError as exc:
                    raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", exc.effect) from None
            _write_owner(root, owner)
            return created
        existing = _owner_record(root, identity)
        if existing.get("endpoint") != _endpoint_identity(endpoint):
            raise HerdrOpsError("endpoint_mismatch", "The stable owner ID belongs to a different Herdr endpoint.")
        if existing.get("purpose") != purpose or existing.get("state") != "owned":
            raise HerdrOpsError("owner_conflict", "The stable owner ID already belongs to a different or closed operation.")
        owner, pane, rebound = _load_owned(endpoint, root, identity, allow_managed_ops=purpose == "managed_ops")
        if (
            owner.get("workspace_id") != workspace_id
            or owner.get("caller_pane_id") != caller_pane_id
            or owner.get("cwd") != str(working_directory)
            or owner.get("ratio") != float(ratio)
        ):
            raise HerdrOpsError("owner_scope_mismatch", "The stable owner ID is already bound to a different pane scope.")
        if purpose == "managed_ops" and owner.get("owner_metadata") != dict(owner_metadata or {}):
            raise HerdrOpsError("owner_scope_mismatch", "The managed pane owner metadata does not match its exact controller scope.")
        if pane is None:
            raise HerdrOpsError("owned_pane_missing", "The stable owned pane no longer exists; refusing to create a duplicate.")
        if pane.get("workspace_id") != workspace_id:
            raise HerdrOpsError("owner_scope_mismatch", "The stable owned pane has moved outside the requested workspace.")
        if purpose != "managed_ops":
            baseline = owner.get("idle_baseline")
            if not isinstance(baseline, str) or not baseline:
                raise HerdrOpsError("pane_busy", "The stable pane has no verified idle baseline and cannot be reused safely.")
            current = _idle_signature(endpoint, pane)
            if current != baseline:
                raise HerdrOpsError("pane_changed", "The owned pane changed after its idle baseline; refusing to reuse it.")
        owner["last_verified_at"] = datetime.now(timezone.utc).isoformat()
        if rebound:
            _write_owner(root, owner)
        return {"owner_id": identity, "pane": pane, "state": "owned", "reused": True}


def _rebind_owner(endpoint: str, owner: dict[str, Any], *, state_dir: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any] | None, bool]:
    current_id = owner.get("pane_id")
    if not isinstance(current_id, str) or not current_id:
        raise HerdrOpsError("ownership_mismatch", "Pane owner metadata has no current pane ID.")
    try:
        pane = get_pane(endpoint, current_id, state_dir=state_dir, allow_managed_ops=owner.get("purpose") == "managed_ops")
    except HerdrOpsError as exc:
        if exc.code not in {"not_found", "pane_not_found"}:
            raise
        if owner.get("purpose") == "managed_ops":
            raise HerdrOpsError("managed_pane_missing", "The managed pane identity changed; reconcile it without rebinding or creating a duplicate.", "unknown") from None
        snapshot_result = request(endpoint, "session.snapshot", {})
        snapshot = snapshot_result.get("snapshot")
        panes = snapshot.get("panes") if isinstance(snapshot, dict) else None
        if not isinstance(panes, list):
            raise HerdrOpsError("rebind_failed", "Could not read the session to rebind the owned pane.") from None
        matches = [pane for pane in panes if isinstance(pane, dict) and pane.get("terminal_id") == owner.get("terminal_id")]
        if not matches:
            return None, False
        if len(matches) != 1:
            raise HerdrOpsError("ambiguous_owner", "More than one pane matched the recorded terminal identity.")
        pane = matches[0]
        if pane.get("label") == _PROTECTED_LABEL:
            raise HerdrOpsError("protected_pane", "The sudo pane is reserved for its controller-owned workflow.")
        if not isinstance(pane.get("pane_id"), str) or not isinstance(pane.get("workspace_id"), str):
            raise HerdrOpsError("rebind_failed", "The moved pane has incomplete identity data.")
        owner.update({"pane_id": pane["pane_id"], "workspace_id": pane["workspace_id"], "tab_id": pane.get("tab_id")})
        return pane, True
    if pane.get("terminal_id") != owner.get("terminal_id"):
        raise HerdrOpsError("ownership_mismatch", "The recorded pane ID now belongs to a different terminal.")
    return pane, False


def _load_owned(
    endpoint: str,
    root: Path,
    identity: str,
    *,
    allow_managed_ops: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    owner = _owner_record(root, identity)
    if owner.get("endpoint") != _endpoint_identity(endpoint):
        raise HerdrOpsError("endpoint_mismatch", "The operation belongs to a different Herdr endpoint.")
    if owner.get("purpose") == "managed_ops" and not allow_managed_ops:
        raise HerdrOpsError("protected_pane", "This managed operations pane is reserved for its controller-owned workflow.")
    pane, rebound = _rebind_owner(endpoint, owner, state_dir=root)
    if pane is not None and pane.get("label") == _PROTECTED_LABEL:
        raise HerdrOpsError("protected_pane", "The sudo pane is reserved for its controller-owned workflow.")
    return owner, pane, rebound


def owned_pane(
    endpoint: str,
    identity: str,
    *,
    state_dir: str | os.PathLike[str] | None = None,
    allow_managed_ops: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return the pane still bound to this owner, rebinding after a Herdr move."""
    root = state_directory(state_dir)
    with writer_lock(root):
        owner, pane, rebound = _load_owned(endpoint, root, validate_owner_id(identity), allow_managed_ops=allow_managed_ops)
        if rebound:
            _write_owner(root, owner)
        if pane is None:
            raise HerdrOpsError("owned_pane_missing", "The owned pane no longer exists on this endpoint.")
        return owner, pane


def resize_owned_pane(
    endpoint: str,
    identity: str,
    direction: str,
    amount: float,
    *,
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    if direction not in _DIRECTIONS:
        raise HerdrOpsError("invalid_direction", "Resize direction must be up, down, left, or right.")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount <= 0.8:
        raise HerdrOpsError("invalid_amount", "Resize amount must be greater than 0 and no more than 0.8.")
    root = state_directory(state_dir)
    with writer_lock(root):
        owner, pane, rebound = _load_owned(endpoint, root, validate_owner_id(identity))
        if rebound:
            _write_owner(root, owner)
        result = request(
            endpoint,
            "pane.resize",
            {"pane_id": pane["pane_id"], "direction": direction, "amount": float(amount)},
            mutation=True,
        )
        resize = result.get("resize")
        if not isinstance(resize, dict) or resize.get("pane_id") != pane.get("pane_id"):
            raise HerdrOpsError("resize_unconfirmed", "Herdr did not confirm the owned pane resize; reconcile before retrying.", "unknown")
        return {"owner_id": identity, "pane_id": pane["pane_id"], "resize": resize}


def move_owned_pane(
    endpoint: str,
    identity: str,
    target_pane_id: str,
    *,
    target_workspace_id: str,
    split: str = "right",
    ratio: float = 0.5,
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    target_pane_id = _required_text(target_pane_id, "destination pane ID")
    target_workspace_id = _required_text(target_workspace_id, "destination workspace ID")
    if split not in {"right", "down"}:
        raise HerdrOpsError("invalid_direction", "Pane moves support right or down splits.")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.05 <= ratio <= 0.95:
        raise HerdrOpsError("invalid_ratio", "The destination split ratio must be between 0.05 and 0.95.")
    root = state_directory(state_dir)
    with writer_lock(root):
        owner, source, rebound = _load_owned(endpoint, root, validate_owner_id(identity))
        if source is None:
            raise HerdrOpsError("owned_pane_missing", "The owned pane no longer exists on this endpoint.")
        destination = get_pane(endpoint, target_pane_id, workspace_id=target_workspace_id, state_dir=root)
        if destination.get("pane_id") == source.get("pane_id"):
            raise HerdrOpsError("invalid_destination", "An owned pane cannot be moved into itself.")
        result = request(
            endpoint,
            "pane.move",
            {
                "pane_id": source["pane_id"],
                "destination": {
                    "type": "tab",
                    "tab_id": destination["tab_id"],
                    "target_pane_id": target_pane_id,
                    "split": split,
                    "ratio": float(ratio),
                },
                "focus": False,
            },
            mutation=True,
        )
        moved = result.get("move_result", {}).get("pane") if isinstance(result.get("move_result"), dict) else None
        if not isinstance(moved, dict) or not isinstance(moved.get("pane_id"), str) or not isinstance(moved.get("workspace_id"), str):
            raise HerdrOpsError("move_unconfirmed", "Herdr did not return the moved pane identity; reconcile before retrying.", "unknown")
        if moved.get("terminal_id") != owner.get("terminal_id"):
            raise HerdrOpsError("ownership_mismatch", "Herdr returned a different terminal after moving the owned pane.", "unknown")
        owner.update({"pane_id": moved["pane_id"], "workspace_id": moved["workspace_id"], "tab_id": moved.get("tab_id")})
        if rebound:
            owner["rebound_before_move"] = True
        _write_owner(root, owner)
        return {"owner_id": identity, "pane": moved, "move_result": result["move_result"]}


def cleanup_owned_pane(
    endpoint: str,
    identity: str,
    *,
    state_dir: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    allow_task_cancel: bool = False,
) -> dict[str, Any]:
    def check_task_owner(root: Path, owner: Mapping[str, Any]) -> None:
        if owner.get("purpose") != "task" or allow_task_cancel:
            return
        task_id = str(owner.get("owner_id", ""))
        task_path = root / "tasks" / f"{validate_owner_id(task_id)}.json"
        task = read_json_state(task_path)
        receipt = task.get("receipt")
        confirmed = (
            task.get("state") in {"completed", "failed"}
            and isinstance(receipt, dict)
            and receipt.get("kind") == "atomic-exit"
            and receipt.get("task_id") == task_id
            and receipt.get("nonce") == task.get("nonce")
            and not isinstance(receipt.get("exit_code"), bool)
            and isinstance(receipt.get("exit_code"), int)
            and 0 <= receipt["exit_code"] <= 255
        )
        if not confirmed:
            raise HerdrOpsError("task_active", "The task has no verified terminal exit receipt; use its explicit cancel operation or reconcile it first.")

    if dry_run:
        root = state_directory(state_dir, create=False)
        owner, pane, _ = _load_owned(endpoint, root, validate_owner_id(identity))
        check_task_owner(root, owner)
        if owner.get("state") == "closed":
            return {"owner_id": identity, "state": "closed", "pane_id": owner.get("pane_id"), "changed": False}
        if pane is None:
            return {"owner_id": identity, "state": "missing", "pane_id": owner.get("pane_id"), "changed": False}
        if not allow_task_cancel:
            _idle_signature(endpoint, pane)
        return {"owner_id": identity, "state": "owned", "pane_id": pane["pane_id"], "changed": False, "planned": "close-owned-pane"}
    root = state_directory(state_dir)
    with writer_lock(root):
        owner, pane, rebound = _load_owned(endpoint, root, validate_owner_id(identity))
        check_task_owner(root, owner)
        if rebound:
            _write_owner(root, owner)
        if owner.get("state") == "closed":
            return {"owner_id": identity, "state": "closed", "pane_id": owner.get("pane_id"), "changed": False}
        if pane is None:
            owner.update({"state": "missing", "missing_at": datetime.now(timezone.utc).isoformat()})
            _write_owner(root, owner)
            return {"owner_id": identity, "state": "missing", "pane_id": owner.get("pane_id"), "changed": False}
        if not allow_task_cancel:
            _idle_signature(endpoint, pane)
        result = request(endpoint, "pane.close", {"pane_id": pane["pane_id"]}, mutation=True)
        if result.get("type") not in {"pane_closed", "ok", None}:
            raise HerdrOpsError("close_unconfirmed", "Herdr did not confirm closing the owned pane; reconcile before retrying.", "unknown")
        owner.update({"state": "closed", "closed_at": datetime.now(timezone.utc).isoformat()})
        _write_owner(root, owner)
        return {"owner_id": identity, "state": "closed", "pane_id": pane["pane_id"], "changed": True}
