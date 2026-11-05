"""Owned temporary pane allocation, movement, resizing, and cleanup."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
import stat
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
        reservation_record: dict[str, Any] | None = None
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
            reservation_record = existing
        caller = get_pane(endpoint, caller_pane_id, workspace_id=workspace_id, state_dir=root)
        before = request(endpoint, "pane.layout", {"pane_id": caller_pane_id}).get("layout")
        if not isinstance(before, dict) or before.get("tab_id") != caller.get("tab_id"):
            raise HerdrOpsError("invalid_response", "Herdr did not return the caller pane's current layout.")
        # Herdr's split ratio sizes the first (parent) pane, whereas the
        # managed owner record's ratio is the desired new child fraction.
        result = request(
            endpoint,
            "pane.split",
            {
                "workspace_id": workspace_id,
                "target_pane_id": caller_pane_id,
                "direction": "down",
                "ratio": 1.0 - float(ratio) if purpose == "managed_ops" else float(ratio),
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
        try:
            _check_workspace(pane, workspace_id, field="created pane")
        except HerdrOpsError:
            raise HerdrOpsError("split_scope_unknown", "Herdr created a pane but its returned workspace scope is unverified.", "unknown") from None
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
        replacement = reservation_record.get("replacement") if reservation_record is not None else None
        generation_id = reservation_record.get("generation_id") if reservation_record is not None else None
        if isinstance(replacement, dict) and isinstance(generation_id, str):
            owner["generation_id"] = generation_id
            owner["replacement"] = dict(replacement) | {"state": "owned"}
        if reservation_record is not None and isinstance(reservation_record.get("retired_creation"), dict):
            owner["retired_creation"] = dict(reservation_record["retired_creation"])
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


def _complete_endpoint_inventory(endpoint: str) -> list[dict[str, Any]]:
    response = request(endpoint, "session.snapshot", {})
    body: Any = response
    for _ in range(3):
        if not isinstance(body, dict):
            break
        if isinstance(body.get("snapshot"), dict):
            body = body["snapshot"]
        elif isinstance(body.get("result"), dict):
            body = body["result"]
        else:
            break
    panes = body.get("panes") if isinstance(body, dict) else None
    if (not isinstance(panes, list)
            or any(not isinstance(pane, dict)
                   or not isinstance(pane.get("pane_id"), str) or not pane["pane_id"]
                   or not isinstance(pane.get("terminal_id"), str) or not pane["terminal_id"]
                   for pane in panes)):
        raise HerdrOpsError("inventory_unknown", "Herdr did not return a complete endpoint pane inventory.")
    pane_ids = [pane["pane_id"] for pane in panes]
    terminal_ids = [pane["terminal_id"] for pane in panes]
    if len(set(pane_ids)) != len(pane_ids) or len(set(terminal_ids)) != len(terminal_ids):
        raise HerdrOpsError("inventory_ambiguous", "Herdr's endpoint pane inventory contains ambiguous identities.")
    return panes


def _replacement_source(
    endpoint: str,
    root: Path,
    identity: str,
    workspace_id: str,
    caller_pane_id: str,
    cwd: str,
    ratio: float,
    owner_metadata: Mapping[str, Any],
    previous_binding: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, dict[str, Any] | None]:
    binding_key = previous_binding.get("binding_key")
    repository = owner_metadata.get("repository")
    host_alias = owner_metadata.get("host_alias")
    if (not isinstance(repository, str) or not repository
            or (host_alias is not None and (not isinstance(host_alias, str) or not host_alias))):
        raise HerdrOpsError("replacement_scope_invalid", "The managed owner metadata cannot identify its exact pane name.")
    expected_name = f"{repository}{'-' + host_alias if host_alias is not None else ''}-sudo"
    pane_id = previous_binding.get("pane_id")
    endpoint_value = previous_binding.get("endpoint")
    if (previous_binding.get("name") != expected_name
            or previous_binding.get("state") not in ("ready", "busy")
            or previous_binding.get("backend") != "herdr"
            or binding_key != owner_metadata.get("binding_key")
            or not isinstance(binding_key, str) or binding_key[:32] != identity
            or not isinstance(pane_id, str) or not pane_id or pane_id == caller_pane_id
            or not isinstance(previous_binding.get("terminal_id"), str)
            or not previous_binding.get("terminal_id")
            or not isinstance(endpoint_value, str) or not endpoint_value
            or _endpoint_identity(endpoint_value) != _endpoint_identity(endpoint)
            or previous_binding.get("parent_id") != caller_pane_id
            or previous_binding.get("workspace_id") != workspace_id
            or previous_binding.get("repository") != repository
            or previous_binding.get("controller_id") != owner_metadata.get("controller_id")
            or previous_binding.get("host_alias") != host_alias
            or not isinstance(previous_binding.get("shell_path"), str)
            or Path(previous_binding["shell_path"]).name != "bash"
            or isinstance(previous_binding.get("shell_pid"), bool)
            or not isinstance(previous_binding.get("shell_pid"), int)
            or previous_binding.get("shell_pid") <= 0
            or not isinstance(previous_binding.get("pane_tty"), str)
            or not previous_binding.get("pane_tty")
            or not isinstance(previous_binding.get("machine_generation"), str)
            or not previous_binding.get("machine_generation")):
        raise HerdrOpsError("replacement_binding_mismatch", "The previous managed pane binding is not an exact Herdr generation.")
    path = _owner_path(root, identity)
    try:
        owner_info = path.lstat()
    except OSError:
        raise HerdrOpsError("replacement_owner_missing", "The exact managed pane owner record is unavailable.") from None
    if (not stat.S_ISREG(owner_info.st_mode) or owner_info.st_uid != os.getuid()
            or owner_info.st_mode & 0o077):
        raise HerdrOpsError("replacement_owner_unsafe", "The exact managed pane owner record is not owner-private.")
    current = _owner_record(root, identity)
    marker = current.get("replacement")
    if current.get("state") == "owned":
        source = current
        try:
            source_bytes = path.read_bytes()
        except OSError:
            raise HerdrOpsError("replacement_owner_missing", "The exact managed pane owner record cannot be preserved.") from None
        prior_marker = None
    elif (current.get("state") == "failed" and isinstance(marker, dict)
          and marker.get("state") == "failed"):
        digest = marker.get("previous_owner_sha256")
        archive = marker.get("owner_archive")
        generation = marker.get("generation_id")
        expected_archive = f"panes/archive/{identity}/{digest}.json"
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or archive != expected_archive
                or not isinstance(generation, str) or len(generation) != 32
                or any(char not in "0123456789abcdef" for char in generation)):
            raise HerdrOpsError("replacement_archive_invalid", "The previous owner archive receipt is invalid.")
        archive_path = root / archive
        source_bytes = _read_archive_file(archive_path)
        if hashlib.sha256(source_bytes).hexdigest() != digest:
            raise HerdrOpsError("replacement_archive_invalid", "The immutable previous owner archive has changed.")
        source = read_json_state(archive_path)
        prior_marker = dict(marker)
        if (current.get("owner_id") != identity
                or current.get("pane_id") != previous_binding.get("pane_id")
                or current.get("terminal_id") != previous_binding.get("terminal_id")
                or current.get("endpoint") != _endpoint_identity(endpoint)
                or current.get("workspace_id") != workspace_id
                or current.get("caller_pane_id") != caller_pane_id
                or current.get("cwd") != cwd
                or current.get("ratio") != float(ratio)
                or current.get("owner_metadata") != dict(owner_metadata)
                or marker.get("previous_pane_id") != previous_binding.get("pane_id")
                or marker.get("previous_terminal_id") != previous_binding.get("terminal_id")):
            raise HerdrOpsError("replacement_owner_mismatch", "The failed replacement reservation does not match its original generation.")
    else:
        raise HerdrOpsError("replacement_owner_conflict", "The managed pane owner is active, unknown, or already reserved.")
    if (source.get("owner_id") != identity
            or source.get("purpose") != "managed_ops"
            or source.get("state") != "owned"
            or source.get("endpoint") != _endpoint_identity(endpoint)
            or source.get("workspace_id") != workspace_id
            or source.get("caller_pane_id") != caller_pane_id
            or source.get("pane_id") != previous_binding.get("pane_id")
            or source.get("terminal_id") != previous_binding.get("terminal_id")
            or source.get("cwd") != cwd
            or source.get("ratio") != float(ratio)
            or source.get("owner_metadata") != dict(owner_metadata)):
        raise HerdrOpsError("replacement_owner_mismatch", "The original managed pane owner metadata does not match this exact scope.")
    panes = _complete_endpoint_inventory(endpoint)
    parents = [pane for pane in panes if pane["pane_id"] == caller_pane_id]
    if (len(parents) != 1 or parents[0].get("workspace_id") != workspace_id
            or not isinstance(parents[0].get("tab_id"), str)
            or source.get("tab_id") != parents[0].get("tab_id")):
        raise HerdrOpsError("replacement_parent_mismatch", "The exact original Herdr parent is not present in its recorded scope.")
    if any(pane["pane_id"] == previous_binding["pane_id"]
           or pane["terminal_id"] == previous_binding["terminal_id"] for pane in panes):
        raise HerdrOpsError("replacement_generation_present", "The original pane or stable terminal identity is still present on this endpoint.")
    from ..terminal_ops.shell import machine_generation
    try:
        current_generation = machine_generation()
    except Exception:
        raise HerdrOpsError("shell_generation_unknown", "The original controlled Bash generation cannot be qualified.") from None
    if previous_binding["machine_generation"] == current_generation:
        if not Path("/proc").is_dir():
            raise HerdrOpsError("shell_generation_unknown", "The original controlled Bash process cannot be verified as gone.")
        try:
            os.stat(f"/proc/{previous_binding['shell_pid']}")
        except FileNotFoundError:
            pass
        except OSError:
            raise HerdrOpsError("shell_generation_unknown", "The original controlled Bash process cannot be verified as gone.") from None
        else:
            raise HerdrOpsError("shell_generation_live", "The original controlled Bash process is still live; replacement is blocked.")
    return source, source_bytes, prior_marker


def verify_missing_managed_pane(
    endpoint: str,
    workspace_id: str,
    caller_pane_id: str,
    cwd: str | os.PathLike[str],
    owner_id: str,
    previous_binding: Mapping[str, Any],
    *,
    state_dir: str | os.PathLike[str] | None = None,
    ratio: float = 1 / 3,
    owner_metadata: Mapping[str, Any],
) -> None:
    """Prove one exact managed generation absent without changing its owner record."""
    identity = validate_owner_id(owner_id)
    working_directory = Path(cwd).expanduser()
    root = state_directory(state_dir, create=False)
    with writer_lock(root):
        _replacement_source(
            endpoint, root, identity, workspace_id, caller_pane_id,
            str(working_directory), ratio, owner_metadata, previous_binding,
        )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _archive_directory(parent: Path, name: str) -> Path:
    path = parent / name
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise HerdrOpsError("owner_archive_failed", "Could not prepare the immutable managed pane owner archive.") from None
    try:
        info = path.lstat()
    except OSError:
        raise HerdrOpsError("unsafe_owner_archive", "The immutable managed pane owner archive is unavailable.") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise HerdrOpsError("unsafe_owner_archive", "The immutable managed pane owner archive is not owner-private.")
    try:
        _fsync_directory(parent)
    except OSError:
        raise HerdrOpsError("owner_archive_failed", "Could not persist the immutable managed pane owner archive directory.") from None
    return path


def _read_archive_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise HerdrOpsError("unsafe_owner_archive", "The immutable managed pane owner archive file is unsafe.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return stream.read()
    except HerdrOpsError:
        raise
    except OSError:
        raise HerdrOpsError("owner_archive_invalid", "The immutable managed pane owner archive cannot be read safely.") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _archive_owner_bytes(root: Path, identity: str, raw: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(raw).hexdigest()
    owners = root / "panes"
    try:
        info = owners.lstat()
    except OSError:
        raise HerdrOpsError("unsafe_owner_state", "Managed pane ownership state is unavailable.") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise HerdrOpsError("unsafe_owner_state", "Managed pane ownership state is not a safe directory.")
    archive_root = _archive_directory(owners, "archive")
    archive_dir = _archive_directory(archive_root, identity)
    path = archive_dir / f"{digest}.json"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(archive_dir)
    except FileExistsError:
        if _read_archive_file(path) != raw:
            raise HerdrOpsError("owner_archive_collision", "The immutable managed pane owner archive conflicts with different bytes.")
        try:
            _fsync_directory(archive_dir)
        except OSError:
            raise HerdrOpsError("owner_archive_failed", "Could not persist the immutable managed pane owner archive.") from None
    except OSError:
        cleanup_failed = False
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                cleanup_failed = True
        if created:
            try:
                path.unlink(missing_ok=True)
                _fsync_directory(archive_dir)
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            raise HerdrOpsError("owner_archive_cleanup_failed", "Could not safely remove the incomplete owner archive; no pane was created.") from None
        raise HerdrOpsError("owner_archive_failed", "Could not preserve the original managed pane owner bytes; no pane was created.") from None
    return f"panes/archive/{identity}/{digest}.json", digest


def _retirement_owner(root: Path, endpoint: str, identity: str, workspace: str,
                      parent: str, cwd: str, metadata: Mapping[str, Any],
                      pane_id: str, terminal_id: str) -> dict[str, Any]:
    path = _owner_path(root, identity)
    _read_archive_file(path)
    owner = read_json_state(path)
    before = owner.get("before_layout")
    if (owner.get("schema_version") != 1 or owner.get("owner_id") != identity
            or owner.get("purpose") != "managed_ops"
            or owner.get("endpoint") != _endpoint_identity(endpoint)
            or owner.get("workspace_id") != workspace or owner.get("caller_pane_id") != parent
            or owner.get("cwd") != cwd or owner.get("ratio") != 1 / 3
            or owner.get("owner_metadata") != dict(metadata)
            or not isinstance(owner.get("created_at"), str) or not owner["created_at"]
            or not isinstance(before, dict) or before.get("tab_id") != owner.get("tab_id")
            or owner.get("pane_id") != pane_id or owner.get("terminal_id") != terminal_id
            or pane_id == parent):
        raise HerdrOpsError("retirement_owner_mismatch", "The managed owner does not match the exact retirement scope.")
    # Any duplicate claim is ambiguous, including malformed or unsafe records.
    for other in (root / "panes").glob("*.json"):
        if other == path:
            continue
        _read_archive_file(other)
        candidate = read_json_state(other)
        other_metadata = candidate.get("owner_metadata")
        if ((candidate.get("endpoint") == _endpoint_identity(endpoint)
             and (candidate.get("pane_id") == pane_id or candidate.get("terminal_id") == terminal_id))
                or candidate.get("owner_id") == identity
                or (isinstance(other_metadata, dict)
                    and other_metadata.get("binding_key") == metadata.get("binding_key"))):
            raise HerdrOpsError("retirement_owner_ambiguous", "Another owner record claims the pane or terminal.")
    return owner


def _retirement_absent(endpoint: str, pane_id: str, terminal_id: str) -> bool:
    return not any(row["pane_id"] == pane_id or row["terminal_id"] == terminal_id
                   for row in _complete_endpoint_inventory(endpoint))


def retire_managed_creation(endpoint: str, workspace: str, parent: str, cwd: str,
                            identity: str, metadata: Mapping[str, Any], pane_id: str,
                            terminal_id: str, retirement_id: str, reservation: bytes,
                            qualify: Any, fence: Any, *,
                            state_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Archive and fence one exact uninitialized managed owner before one close."""
    root = state_directory(state_dir, create=False)
    identity = validate_owner_id(identity)
    with writer_lock(root):
        owner = _retirement_owner(root, endpoint, identity, workspace, parent, cwd,
                                  metadata, pane_id, terminal_id)
        if owner.get("state") != "owned" or owner.get("retirement") is not None:
            raise HerdrOpsError("retirement_owner_conflict", "The owner is not an unretired owned generation.")
        panes = _complete_endpoint_inventory(endpoint)
        matches = [row for row in panes if row["pane_id"] == pane_id]
        parents = [row for row in panes if row["pane_id"] == parent]
        if (len(matches) != 1 or len(parents) != 1
                or matches[0]["terminal_id"] != terminal_id
                or matches[0].get("workspace_id") != workspace
                or parents[0].get("workspace_id") != workspace
                or not isinstance(owner.get("tab_id"), str)
                or matches[0].get("tab_id") != owner["tab_id"]
                or parents[0].get("tab_id") != owner["tab_id"]):
            raise HerdrOpsError("retirement_native_mismatch", "The native pane, stable terminal, parent or tab changed.")
        observation = qualify(matches[0])
        owner_raw = _read_archive_file(_owner_path(root, identity))
        owner_archive, owner_digest = _archive_owner_bytes(root, identity, owner_raw)
        reservation_archive, reservation_digest = _archive_owner_bytes(root, identity, reservation)
        marker = {"retirement_id": retirement_id, "pane_id": pane_id, "terminal_id": terminal_id,
                  "state": "unknown", "owner_archive": owner_archive,
                  "owner_sha256": owner_digest, "reservation_archive": reservation_archive,
                  "reservation_sha256": reservation_digest, "observation": observation}
        # Both journals are durable before close. Either incomplete fence prevents replay.
        fence(marker)
        owner["retirement"] = marker
        owner["state"] = "retirement_unknown"
        _write_owner(root, owner)
        try:
            current = _complete_endpoint_inventory(endpoint)
            rows = [row for row in current if row["pane_id"] == pane_id]
            if (len(rows) != 1 or rows[0] != matches[0]
                    or qualify(rows[0]) != observation):
                return marker
            request(endpoint, "pane.close", {"pane_id": pane_id}, mutation=True)
        except Exception:
            return marker
        if _retirement_absent(endpoint, pane_id, terminal_id):
            marker["state"] = "retired"
            owner["state"] = "retired"
            owner["retirement"] = marker
            _write_owner(root, owner)
        return marker


def reconcile_managed_creation_retirement(endpoint: str, workspace: str, parent: str,
                                          cwd: str, identity: str, metadata: Mapping[str, Any],
                                          marker: Mapping[str, Any], *,
                                          state_dir: str | os.PathLike[str] | None = None,
                                          reservation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    root = state_directory(state_dir, create=False)
    identity = validate_owner_id(identity)
    with writer_lock(root):
        owner = _retirement_owner(root, endpoint, identity, workspace, parent, cwd, metadata,
                                  marker["pane_id"], marker["terminal_id"])
        saved = owner.get("retirement")
        if (not isinstance(saved, dict) or any(saved.get(key) != marker.get(key)
                for key in ("retirement_id", "pane_id", "terminal_id", "owner_sha256",
                            "reservation_sha256", "observation"))):
            raise HerdrOpsError("retirement_fence_mismatch", "The retirement journals do not agree.")
        for prefix in ("owner", "reservation"):
            digest = saved.get(f"{prefix}_sha256")
            archive = saved.get(f"{prefix}_archive")
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(ch not in "0123456789abcdef" for ch in digest)
                    or not isinstance(archive, str)
                    or archive != f"panes/archive/{identity}/{digest}.json"
                    or hashlib.sha256(_read_archive_file(root / archive)).hexdigest() != digest):
                raise HerdrOpsError("retirement_archive_invalid", "The original retirement archive is invalid.")
        archived_owner = read_json_state(root / saved["owner_archive"])
        original_owner = dict(owner)
        original_owner.pop("retirement", None)
        original_owner["state"] = "owned"
        if archived_owner != original_owner:
            raise HerdrOpsError("retirement_archive_invalid", "The archived owner differs from the fenced original owner.")
        archived_reservation = read_json_state(root / saved["reservation_archive"])
        if reservation is not None and archived_reservation != dict(reservation):
            raise HerdrOpsError("retirement_archive_invalid", "The original creation reservation changed.")
        if owner.get("state") not in {"retired", "retirement_unknown"}:
            raise HerdrOpsError("retirement_fence_mismatch", "The owner is not fenced for this retirement.")
        if _retirement_absent(endpoint, marker["pane_id"], marker["terminal_id"]):
            saved = dict(saved) | {"state": "retired"}
            if owner.get("state") != "retired":
                owner["state"] = "retired"
                owner["retirement"] = saved
                _write_owner(root, owner)
        else:
            saved = dict(saved) | {"state": "unknown"}
        return saved

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
    previous_binding: Mapping[str, Any] | None = None,
    retirement_proof: Mapping[str, Any] | None = None,
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
            if previous_binding is not None:
                raise HerdrOpsError("replacement_owner_missing", "The original managed pane owner record is unavailable.")
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
            try:
                _write_owner(root, reservation)
            except HerdrOpsError as exc:
                raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", "unknown") from None
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
                try:
                    _write_owner(root, reservation)
                except HerdrOpsError:
                    raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", "unknown") from None
                raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", exc.effect) from None
            try:
                owner, pane = _load_owned(endpoint, root, identity, allow_managed_ops=purpose == "managed_ops")[:2]
                if pane is None:
                    raise HerdrOpsError("owned_pane_missing", "Herdr created the pane but it is no longer available.")
                if purpose == "managed_ops":
                    if owner.get("owner_metadata") != dict(owner_metadata or {}):
                        raise HerdrOpsError("owner_scope_mismatch", "The managed pane owner metadata does not match its exact controller scope.")
                else:
                    owner["idle_baseline"] = _new_pane_idle_signature(endpoint, pane)
                _write_owner(root, owner)
            except HerdrOpsError as exc:
                raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", "unknown") from None
            return created
        existing = _owner_record(root, identity)
        if retirement_proof is not None:
            if purpose != "managed_ops" or owner_metadata is None or previous_binding is not None:
                raise HerdrOpsError("retirement_scope_invalid", "Only a managed owner can allocate after retirement.")
            marker = reconcile_managed_creation_retirement(
                endpoint, workspace_id, caller_pane_id, str(working_directory), identity,
                owner_metadata, retirement_proof, state_dir=root,
            )
            if marker.get("state") != "retired" or existing.get("state") != "retired":
                raise HerdrOpsError("retirement_unresolved", "The original creation has not been conclusively retired.")
            reservation = dict(existing)
            reservation.update({"state": "creating", "retired_creation": dict(marker)})
            _write_owner(root, reservation)
            try:
                return create_owned_pane(
                    endpoint, workspace_id, caller_pane_id, working_directory,
                    state_dir=root, owner_id=identity, purpose=purpose, ratio=ratio,
                    owner_metadata=owner_metadata, environment=environment, _reservation=True,
                )
            except HerdrOpsError as exc:
                # No automatic retry even if the split failed before dispatch.
                reservation["state"] = "unknown" if exc.effect == "unknown" else "failed"
                _write_owner(root, reservation)
                raise
        if previous_binding is not None:
            if purpose != "managed_ops" or owner_metadata is None:
                raise HerdrOpsError("replacement_scope_invalid", "Only an exact managed-operations owner can replace a missing generation.")
            _, source_bytes, prior_marker = _replacement_source(
                endpoint, root, identity, workspace_id, caller_pane_id,
                str(working_directory), ratio, owner_metadata, previous_binding,
            )
            if prior_marker is None:
                archive, digest = _archive_owner_bytes(root, identity, source_bytes)
                generation_id = uuid.uuid4().hex
                marker = {
                    "generation_id": generation_id,
                    "owner_archive": archive,
                    "previous_owner_sha256": digest,
                    "previous_pane_id": previous_binding["pane_id"],
                    "previous_terminal_id": previous_binding["terminal_id"],
                    "state": "creating",
                }
            else:
                marker = prior_marker
                generation_id = str(marker["generation_id"])
                marker["state"] = "creating"
            reservation = dict(existing)
            reservation.update({
                "state": "creating",
                "generation_id": generation_id,
                "replacement": marker,
                "replacement_started_at": datetime.now(timezone.utc).isoformat(),
            })
            try:
                _write_owner(root, reservation)
            except HerdrOpsError as exc:
                raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", "unknown") from None
            try:
                created = create_owned_pane(
                    endpoint, workspace_id, caller_pane_id, working_directory,
                    state_dir=root, owner_id=identity, purpose=purpose,
                    ratio=ratio, owner_metadata=owner_metadata,
                    environment=environment, _reservation=True,
                )
            except HerdrOpsError as exc:
                try:
                    failed = _owner_record(root, identity)
                    failed["state"] = "failed" if exc.effect == "none" else "unknown"
                    failed["replacement"] = dict(marker) | {
                        "state": "failed" if exc.effect == "none" else "unknown",
                    }
                    failed["error"] = exc.as_dict()
                    _write_owner(root, failed)
                except HerdrOpsError:
                    raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", "unknown") from None
                raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", exc.effect) from None
            try:
                owner, pane, _ = _load_owned(endpoint, root, identity, allow_managed_ops=True)
                if pane is None:
                    raise HerdrOpsError("owned_pane_missing", "Herdr created the pane but it is no longer available.")
                if owner.get("owner_metadata") != dict(owner_metadata):
                    raise HerdrOpsError("owner_scope_mismatch", "The managed pane owner metadata does not match its exact controller scope.")
                owner["generation_id"] = generation_id
                owner["replacement"] = dict(marker) | {"state": "owned"}
                _write_owner(root, owner)
            except HerdrOpsError as exc:
                try:
                    current = _owner_record(root, identity)
                    if (current.get("state") == "owned"
                            and current.get("generation_id") == generation_id
                            and current.get("replacement") == (dict(marker) | {"state": "owned"})
                            and current.get("owner_metadata") == dict(owner_metadata)):
                        current["state"] = "unknown"
                        current["replacement"] = dict(marker) | {"state": "unknown"}
                        current["error"] = {"code": exc.code, "message": exc.message, "effect": "unknown"}
                        _write_owner(root, current)
                except HerdrOpsError:
                    pass
                raise HerdrOpsError(exc.code, f"{exc.message} Stable owner ID: {identity}.", "unknown") from None
            return created
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
