"""Visible managed-pane dispatch and transient controller credential delivery."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid

from .credentials import iter_candidates
from .models import PaneError, PaneRequest, PaneTransition
from .pane import ManagedPaneService
from .shell import _atomic_json, _operation_guard, _read_operation, _state_directory


DEFAULT_AUTHENTICATION_TIMEOUT = 120.0
_MAX_CREDENTIAL_REQUEST = 256


def _status_handle(binding_key: str, operation_id: str) -> str:
    return f"lsop1.{binding_key}.{operation_id}"


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    """Read a bounded frame even when the stream fragments individual reads."""
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise OSError("credential channel closed before the frame completed")
        result.extend(block)
    return bytes(result)


def _current(predicate: Callable[[], bool] | None) -> bool:
    if predicate is None:
        return True
    try:
        return predicate() is True
    except Exception:
        return False


def _record_authentication_expired_locked(
    directory: Path, request: PaneRequest, operation_id: str,
) -> bool:
    """Expire only a lookup that has not delivered its response."""
    path = directory / f"{operation_id}.sudo-lookup.json"
    if path.is_symlink():
        return False
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if (not isinstance(existing, dict)
                or existing.get("credential_lookup_status") != "pending"):
            return False
    _atomic_json(path, {
        "binding_key": request.binding_key,
        "operation_id": operation_id,
        "credential_lookup_status": "expired",
    })
    return True


def _record_authentication_expired(root: Path, request: PaneRequest, operation_id: str) -> bool:
    """Record terminal expiry under the same guard as broker lookup receipts."""
    try:
        directory = _state_directory(root, request.binding_key, create=False)
        with _operation_guard(directory):
            _, operation = _read_operation(str(root), request.binding_key, operation_id)
            if operation.get("state") not in {"staged", "running"}:
                return False
            return _record_authentication_expired_locked(directory, request, operation_id)
    except (OSError, PaneError):
        return False


@contextmanager
def _credential_server(
    root: Path, request: PaneRequest, operation_id: str, shell_pid: int,
    candidate_factory: Callable[[], Iterator[Any]], validate_current: Callable[[], bool] | None,
    authentication_deadline: float,
    credential_statuses: dict[str, str],
    authentication_requested: threading.Event,
) -> Iterator[str]:
    """Serve one operation's lazily resolved candidates to its direct pane child."""
    runtime_base = Path(f"/run/user/{os.getuid()}")
    if not runtime_base.is_dir() or runtime_base.stat().st_uid != os.getuid():
        runtime_base = Path("/tmp")
    temporary = Path(tempfile.mkdtemp(prefix="ls-terminal-", dir=runtime_base))
    temporary.chmod(0o700)
    path = temporary / "c.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(2)
    listener.settimeout(0.2)
    stop = threading.Event()
    candidates = iter(candidate_factory())
    active_lock = threading.Lock()
    active_connection: list[socket.socket | None] = [None]

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(5.0)
                with active_lock:
                    active_connection[0] = connection
                try:
                    if stop.is_set() or time.monotonic() >= authentication_deadline:
                        continue
                    if not _current(validate_current):
                        continue
                    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                    peer_pid, peer_uid, _ = struct.unpack("3i", credentials)
                    stat_text = Path(f"/proc/{peer_pid}/stat").read_text(encoding="ascii")
                    peer_parent = int(stat_text[stat_text.rfind(")") + 1 :].split()[1])
                    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
                    if not 1 <= size <= _MAX_CREDENTIAL_REQUEST:
                        continue
                    supplied = _recv_exact(connection, size).decode("ascii", "strict")
                    _, record = _read_operation(str(root), request.binding_key, operation_id)
                    if (peer_uid != os.getuid() or peer_parent != shell_pid or supplied != operation_id
                            or record.get("state") != "running" or record.get("shell_pid") != shell_pid):
                        continue
                    owner_current = _current(validate_current)
                    if not owner_current or stop.is_set() or time.monotonic() >= authentication_deadline:
                        if owner_current and not stop.is_set():
                            try:
                                _record_authentication_expired(root, request, operation_id)
                            except (OSError, PaneError):
                                pass
                        continue
                    authentication_requested.set()
                    candidate = next(candidates, None)
                    if stop.is_set():
                        continue
                    # Candidate loading may perform a bounded EnvMan call. Do
                    # not deliver its result if ownership or the operation
                    # changed while that lookup was in flight.
                    _, record = _read_operation(str(root), request.binding_key, operation_id)
                    if stop.is_set():
                        continue
                    owner_current = _current(validate_current)
                    # Test the clock after the potentially slow owner validator:
                    # a valid answer must not revive an expired auth window.
                    if (not owner_current or stop.is_set() or time.monotonic() >= authentication_deadline
                            or record.get("state") != "running" or record.get("shell_pid") != shell_pid):
                        if (owner_current and not stop.is_set() and time.monotonic() >= authentication_deadline
                                and record.get("state") == "running"
                                and record.get("shell_pid") == shell_pid):
                            _record_authentication_expired(root, request, operation_id)
                        continue
                    lookup_status = "available" if candidate is not None else credential_statuses.get(
                        "GENERIC_SUDO_KEY", "unavailable"
                    )
                    operation_path, _ = _read_operation(str(root), request.binding_key, operation_id)
                    with _operation_guard(operation_path.parent):
                        # The lock may itself be contended past the auth budget.
                        # Recheck ownership after taking it, then recheck the
                        # deadline and stop state before changing the receipt.
                        if stop.is_set():
                            continue
                        owner_current = _current(validate_current)
                        _, guarded_record = _read_operation(
                            str(root), request.binding_key, operation_id,
                        )
                        if (not owner_current or guarded_record.get("state") != "running"
                                or guarded_record.get("shell_pid") != shell_pid):
                            continue
                        if stop.is_set() or time.monotonic() >= authentication_deadline:
                            if not stop.is_set():
                                _record_authentication_expired_locked(
                                    operation_path.parent, request, operation_id,
                                )
                            continue
                        lookup_path = operation_path.parent / f"{operation_id}.sudo-lookup.json"
                        if lookup_path.is_symlink():
                            continue
                        if lookup_path.exists():
                            try:
                                previous_lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
                            except (OSError, ValueError):
                                continue
                            if (not isinstance(previous_lookup, dict)
                                    or previous_lookup.get("credential_lookup_status") == "expired"):
                                continue
                        payload = (b"" if candidate is None else
                                   candidate.variable.encode("ascii") + b"\0" + candidate.value.encode("utf-8"))
                        _atomic_json(lookup_path, {
                            "binding_key": request.binding_key,
                            "operation_id": operation_id,
                            "credential_lookup_status": "pending",
                        })
                        # Receipt writes can consume the remaining budget. Keep
                        # expiry serialized through delivery so the foreground
                        # timeout cannot mark this candidate expired as it sends.
                        if stop.is_set() or time.monotonic() >= authentication_deadline:
                            if not stop.is_set():
                                _record_authentication_expired_locked(
                                    operation_path.parent, request, operation_id,
                                )
                            continue
                        connection.sendall(struct.pack("!I", len(payload)) + payload)
                        _atomic_json(lookup_path, {
                            "binding_key": request.binding_key,
                            "operation_id": operation_id,
                            "credential_lookup_status": lookup_status,
                        })
                except (OSError, ValueError, UnicodeError, StopIteration):
                    continue
                finally:
                    with active_lock:
                        active_connection[0] = None

    thread = threading.Thread(target=serve, name="managed-sudo-candidates", daemon=True)
    thread.start()
    try:
        yield str(path)
    finally:
        stop.set()
        listener.close()
        with active_lock:
            connection = active_connection[0]
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        # Credential resolution is bounded by EnvMan's five-second timeout;
        # allow that in-flight read to unwind before deleting its private socket.
        thread.join(timeout=6)
        path.unlink(missing_ok=True)
        temporary.rmdir()


def run_visible_sudo(
    service: ManagedPaneService,
    request: PaneRequest,
    argv: Sequence[str],
    *,
    timeout: float,
    authentication_timeout: float = DEFAULT_AUTHENTICATION_TIMEOUT,
    runas_user: str | None = None,
    runas_group: str | None = None,
    environment: Mapping[str, str] | None = None,
    validate_current: Callable[[], bool] | None = None,
    single_password_qualified: bool = False,
) -> dict[str, Any]:
    """Dispatch exact privileged argv through an existing visible managed pane.

    The caller retains the current controller process and its private environment
    through the independently bounded authentication window. An observation
    timeout returns a running handle; it does not demote a live operation or
    close its credential broker early.
    """
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise PaneError("invalid_timeout", "The visible sudo operation needs a positive timeout.")
    if (not isinstance(authentication_timeout, (int, float))
            or isinstance(authentication_timeout, bool) or not math.isfinite(authentication_timeout)
            or authentication_timeout <= 0):
        raise PaneError("invalid_authentication_timeout", "The sudo authentication budget must be positive.")
    if validate_current is not None and (not callable(validate_current) or not _current(validate_current)):
        raise PaneError("owner_not_current", "The controller owner is no longer current for this sudo operation.")
    if validate_current is None:
        raise PaneError("owner_validator_required", "Managed sudo requires a live controller-owner validator.")
    if type(single_password_qualified) is not bool:
        raise PaneError("invalid_authentication_policy", "Single-password qualification must be a policy-bound boolean.")
    root = service.state_dir
    current = service.inspect(request)
    if current.state != "ready" or current.binding is None or current.binding.shell_pid is None:
        return current.to_dict()
    operation_id = uuid.uuid4().hex
    if validate_current is not None and not _current(validate_current):
        raise PaneError("owner_not_current", "The controller owner changed before sudo dispatch.")
    credential_statuses: dict[str, str] = {}
    authentication_requested = threading.Event()
    authentication_deadline = time.monotonic() + float(authentication_timeout)
    def candidate_factory() -> Iterator[Any]:
        return iter_candidates(request.host_alias or request.repository,
                               os.environ if environment is None else environment,
                               statuses=credential_statuses)

    with _credential_server(root, request, operation_id, current.binding.shell_pid,
                            candidate_factory, validate_current, authentication_deadline,
                            credential_statuses, authentication_requested) as socket_path:
        source_root = Path(__file__).resolve().parents[3]
        prefix = [shutil.which("env") or "/usr/bin/env", f"PYTHONPATH={source_root}",
                  sys.executable, "-m", "ls.core.terminal_ops.sudo_bridge",
                  "--state-dir", str(root), "--binding-key", request.binding_key,
                  "--operation-id", operation_id, "--socket", socket_path]
        if single_password_qualified:
            prefix.append("--single-password-qualified")
        if runas_user is not None:
            prefix.extend(("--runas-user", runas_user))
        if runas_group is not None:
            prefix.extend(("--runas-group", runas_group))
        transition = PaneTransition(operation_id, tuple([*prefix, "--", *argv]),
                                    requires_sudo_receipt=True)
        if validate_current is not None and not _current(validate_current):
            raise PaneError("owner_not_current", "The controller owner changed before sudo dispatch.")
        dispatch = service.transition(request, transition)
        if dispatch.state != "busy":
            return dispatch.to_dict()
        deadline = time.monotonic() + timeout
        owner_lost = False
        while time.monotonic() < deadline:
            if validate_current is not None and not _current(validate_current):
                owner_lost = True
                break
            try:
                _, receipt = _read_operation(str(root), request.binding_key, operation_id)
            except PaneError:
                break
            if receipt.get("state") == "unknown":
                unknown = service.reconcile(request, operation_id).to_dict()
                unknown.update({"status": "unknown", "run_id": operation_id,
                                "target": current.binding.pane_id, "tail": ""})
                return unknown
            if receipt.get("state") == "completed":
                prompt_deadline = min(deadline, time.monotonic() + 1.0)
                while time.monotonic() < prompt_deadline and service.inspect(request).state != "ready":
                    time.sleep(0.02)
                settled = service.reconcile(request, operation_id).to_dict()
                settled.update({"status": "completed", "exit_code": receipt.get("exit_code"),
                                "run_id": operation_id, "status_handle": _status_handle(request.binding_key, operation_id),
                                "target": current.binding.pane_id, "tail": ""})
                path = root / "terminal_ops" / request.binding_key / f"{operation_id}.sudo.json"
                try:
                    settled["sudo"] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    settled["sudo"] = {"status": "unknown", "action_required": True}
                return settled
            time.sleep(0.05)
        if not owner_lost:
            # Keep the existing foreground broker alive through its independent
            # authentication budget. The workload may outlive this window.
            while time.monotonic() < authentication_deadline:
                if validate_current is not None and not _current(validate_current):
                    break
                try:
                    _, receipt = _read_operation(str(root), request.binding_key, operation_id)
                except PaneError:
                    break
                if receipt.get("state") in {"completed", "unknown"}:
                    break
                time.sleep(min(0.05, max(0.001, authentication_deadline - time.monotonic())))
        try:
            _, receipt = _read_operation(str(root), request.binding_key, operation_id)
        except PaneError:
            receipt = {"state": "unknown"}
        authentication_expired = (
            receipt.get("state") in {"staged", "running"}
            and authentication_requested.is_set()
            and time.monotonic() >= authentication_deadline
            and _current(validate_current)
            and _record_authentication_expired(root, request, operation_id)
        )
        handle = _status_handle(request.binding_key, operation_id)
        result = service.reconcile(request, operation_id).to_dict() if _current(validate_current) else {
            "api_version": 1, "state": receipt.get("state", "unknown"),
            "binding_key": request.binding_key, "operation_id": operation_id,
            "detail": "Controller ownership changed; the running operation remains observable by its status handle.",
        }
        if owner_lost:
            detail = "Controller ownership changed; the operation remains observable by its status handle."
        elif receipt.get("state") in {"staged", "running"}:
            detail = "Execution is still running; use the durable status handle to observe it without replay."
        else:
            detail = receipt.get("detail") or "The operation outcome remains uncertain; reconcile without replay."
        result.update({"status": "running" if receipt.get("state") in {"staged", "running"} else receipt.get("state", "unknown"),
                       "run_id": operation_id, "status_handle": handle,
                       "target": current.binding.pane_id,
                       "detail": detail,
                       "tail": ""})
        if authentication_expired:
            result["authentication_status"] = "expired"
            result["authentication_detail"] = (
                "Automatic authentication expired before credential delivery; reconcile status without replay."
            )
        if receipt.get("state") == "completed":
            result["exit_code"] = receipt.get("exit_code")
            sudo_path = root / "terminal_ops" / request.binding_key / f"{operation_id}.sudo.json"
            try:
                result["sudo"] = json.loads(sudo_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result["sudo"] = {"status": "unknown", "action_required": True}
        return result
