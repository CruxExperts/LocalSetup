"""Visible managed-pane dispatch and transient controller credential delivery."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import shutil
import select
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
    *, protocol_digest: str | None = None, dialog_context: Any | None = None,
    single_password_qualified: bool = False, expected_bridge: list[str] | None = None,
) -> Iterator[str]:
    """Own the existing operation's credential channel, including private input."""
    from .command_state import process_info
    from .dialog_channel import ChannelClosed
    from .sudo_channel import receive_request, send_reply

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
    admitted_bridge: tuple[int, int] | None = None
    automatic_ordinal, automatic_delivered, manual_requested = 0, False, False
    last_lookup = "unavailable"

    def serve() -> None:
        nonlocal admitted_bridge, automatic_ordinal, automatic_delivered, manual_requested, last_lookup
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(.2 if protocol_digest is not None else 5.0)
                with active_lock:
                    active_connection[0] = connection
                secret = bytearray()
                try:
                    peer_pid, peer_uid, _ = struct.unpack("3i", connection.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                    peer = process_info(peer_pid)
                    if peer_uid != os.getuid() or peer is None or peer["ppid"] != shell_pid:
                        continue
                    peer_generation = (peer_pid, peer["starttime"])
                    if protocol_digest is not None:
                        actual = Path(f"/proc/{peer_pid}/cmdline").read_bytes().split(b"\0")
                        if (not expected_bridge or actual != [os.fsencode(part) for part in expected_bridge] + [b""]
                                or admitted_bridge not in (None, peer_generation)):
                            continue
                        admitted_bridge = peer_generation

                    def current() -> bool:
                        try:
                            if stop.is_set() or not _current(validate_current):
                                return False
                            observed = process_info(peer_pid)
                            _, record = _read_operation(str(root), request.binding_key, operation_id)
                            command_matches = (protocol_digest is None or
                                Path(f"/proc/{peer_pid}/cmdline").read_bytes().split(b"\0") ==
                                [os.fsencode(part) for part in expected_bridge] + [b""])
                            return (not stop.is_set() and command_matches and observed is not None
                                    and observed["starttime"] == peer["starttime"] and observed["ppid"] == shell_pid
                                    and record.get("state") == "running" and record.get("shell_pid") == shell_pid
                                    and time.monotonic() < authentication_deadline)
                        except (OSError, PaneError):
                            return False

                    def connected() -> bool:
                        if not current():
                            return False
                        readable, _, _ = select.select([connection], [], [], 0)
                        return not readable  # EOF or unsolicited extra input invalidates this exchange.

                    if not current():
                        continue
                    if protocol_digest is None:
                        size = struct.unpack("!I", _recv_exact(connection, 4))[0]
                        if not 1 <= size <= _MAX_CREDENTIAL_REQUEST:
                            continue
                        supplied = _recv_exact(connection, size).decode("ascii", "strict")
                        if supplied != operation_id:
                            continue
                        kind, ordinal = "automatic", automatic_ordinal + 1
                    else:
                        metadata = receive_request(connection, current)
                        if metadata["operation_id"] != operation_id or metadata["payload_digest"] != protocol_digest:
                            continue
                        kind, ordinal = metadata["kind"], metadata["ordinal"]
                    if not connected():
                        continue
                    authentication_requested.set()
                    reply_started = False

                    def deliver(status: str, source: str, value: bytearray | None,
                                *, manual_state: str | None = None) -> bool:
                        nonlocal reply_started
                        operation_path, _ = _read_operation(str(root), request.binding_key, operation_id)
                        with _operation_guard(operation_path.parent):
                            if not connected() or reply_started:
                                return False
                            lookup_path = operation_path.parent / f"{operation_id}.sudo-lookup.json"
                            if lookup_path.is_symlink():
                                return False
                            if lookup_path.exists():
                                prior = json.loads(lookup_path.read_text(encoding="utf-8"))
                                if not isinstance(prior, dict) or prior.get("credential_lookup_status") == "expired":
                                    return False
                            receipt = {"binding_key": request.binding_key, "operation_id": operation_id,
                                       "credential_lookup_status": "pending"}
                            if protocol_digest is not None:
                                receipt["payload_digest"] = protocol_digest
                            if dialog_context is not None:
                                receipt["dialog_handle"] = dialog_context.handle
                            if manual_state is not None:
                                receipt["manual_authentication"] = "pending"
                            _atomic_json(lookup_path, receipt)
                            if not connected():
                                if time.monotonic() >= authentication_deadline and not stop.is_set():
                                    _record_authentication_expired_locked(operation_path.parent, request, operation_id)
                                return False
                            reply_started = True  # A partial write must never be repeated.
                            if protocol_digest is not None:
                                send_reply(connection, status, source, value, current)
                            else:
                                payload = (b"" if value is None else b"GENERIC_SUDO_KEY\0" + value)
                                connection.sendall(struct.pack("!I", len(payload)) + payload)
                            receipt["credential_lookup_status"] = last_lookup
                            if manual_state is not None:
                                receipt["manual_authentication"] = manual_state
                            _atomic_json(lookup_path, receipt)
                            return True

                    if kind == "automatic":
                        if protocol_digest is not None and (manual_requested or ordinal != automatic_ordinal + 1
                                or (ordinal == 2 and (not automatic_delivered or not single_password_qualified))):
                            continue
                        automatic_ordinal = ordinal
                        candidate = next(candidates, None)
                        last_lookup = "available" if candidate is not None else credential_statuses.get(
                            "GENERIC_SUDO_KEY", "unavailable")
                        if candidate is None and last_lookup == "available":
                            last_lookup = "unavailable"  # Previously supplied generic input was rejected.
                        if not connected():
                            if not stop.is_set() and time.monotonic() >= authentication_deadline and _current(validate_current):
                                _record_authentication_expired(root, request, operation_id)
                            continue
                        if candidate is not None:
                            secret.extend(candidate.value.encode("utf-8"))
                        sent = deliver(last_lookup, "generic" if candidate is not None else "none",
                                       secret if candidate is not None else None)
                        automatic_delivered |= sent and candidate is not None
                    else:
                        if (manual_requested or ordinal != automatic_ordinal or last_lookup not in {"unavailable", "empty", "invalid"}
                                or (automatic_delivered and (ordinal != 2 or not single_password_qualified))
                                or (not automatic_delivered and ordinal != 1)):
                            continue
                        manual_requested = True
                        if dialog_context is None:
                            deliver("unavailable", "none", None, manual_state="unavailable")
                            continue
                        outcome = dialog_context.present(
                            operation_id=operation_id, target=request.host_alias or request.repository,
                            deadline=authentication_deadline, validate_current=connected,
                            deliver=lambda value: deliver("available", "manual", value, manual_state="provided_to_bridge"),
                        )
                        if not reply_started and connected():
                            state = outcome.get("state")
                            status = state if state in {"cancelled", "expired", "delivery_unknown"} else "blocked"
                            deliver(status, "none", None, manual_state=status)
                except (OSError, ValueError, UnicodeError, StopIteration, PaneError, ChannelClosed):
                    # Neither private input nor provider exception text enters output or state.
                    continue
                finally:
                    secret[:] = b"\0" * len(secret)
                    secret.clear()
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
    dialog_context: Any | None = None,
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
    if dialog_context is not None:
        from .sudo_dialog import SudoDialogContext
        if not isinstance(dialog_context, SudoDialogContext) or not dialog_context.current():
            raise PaneError("dialog_owner_unavailable", "The explicit private dialog session is not current.")
        controller_current = validate_current
        validate_current = lambda: _current(controller_current) and dialog_context.current()
    from .command_state import process_info
    from .sudo import payload_digest
    controller = process_info(os.getpid())
    if controller is None:
        raise PaneError("owner_not_current", "The native controller process is unavailable.")
    digest = payload_digest(argv, runas_user=runas_user, runas_group=runas_group)
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

    expected_bridge: list[str] = []
    with _credential_server(root, request, operation_id, current.binding.shell_pid,
                            candidate_factory, validate_current, authentication_deadline,
                            credential_statuses, authentication_requested, protocol_digest=digest,
                            dialog_context=dialog_context, single_password_qualified=single_password_qualified,
                            expected_bridge=expected_bridge) as socket_path:
        source_root = Path(__file__).resolve().parents[3]
        prefix = [shutil.which("env") or "/usr/bin/env", f"PYTHONPATH={source_root}",
                  sys.executable, "-m", "ls.core.terminal_ops.sudo_bridge",
                  "--state-dir", str(root), "--binding-key", request.binding_key,
                  "--operation-id", operation_id, "--socket", socket_path,
                  "--private-input-protocol", "--authentication-deadline", str(authentication_deadline),
                  "--controller-pid", str(controller["pid"]),
                  "--controller-starttime", str(controller["starttime"])]
        if dialog_context is not None:
            prefix.append("--manual-input-enabled")
        if single_password_qualified:
            prefix.append("--single-password-qualified")
        if runas_user is not None:
            prefix.extend(("--runas-user", runas_user))
        if runas_group is not None:
            prefix.extend(("--runas-group", runas_group))
        expected_bridge.extend([*prefix[2:], "--", *argv])
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
                                "status_handle": _status_handle(request.binding_key, operation_id),
                                "target": current.binding.pane_id, "tail": ""})
                path = root / "terminal_ops" / request.binding_key / f"{operation_id}.sudo.json"
                try:
                    unknown["sudo"] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    unknown["sudo"] = {"status": "unknown", "action_required": True}
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
