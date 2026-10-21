"""Visible managed-pane dispatch and transient controller credential delivery."""

from __future__ import annotations

from contextlib import contextmanager
import json
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
from .shell import _read_operation


def _current(predicate: Callable[[], bool] | None) -> bool:
    if predicate is None:
        return True
    try:
        return predicate() is True
    except Exception:
        return False


@contextmanager
def _credential_server(
    root: Path, request: PaneRequest, operation_id: str, shell_pid: int,
    candidate_factory: Callable[[], Iterator[Any]], validate_current: Callable[[], bool] | None,
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

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                try:
                    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                    peer_pid, peer_uid, _ = struct.unpack("3i", credentials)
                    stat_text = Path(f"/proc/{peer_pid}/stat").read_text(encoding="ascii")
                    peer_parent = int(stat_text[stat_text.rfind(")") + 1 :].split()[1])
                    supplied = connection.recv(160).strip().decode("ascii")
                    _, record = _read_operation(str(root), request.binding_key, operation_id)
                    if (peer_uid != os.getuid() or peer_parent != shell_pid or supplied != operation_id
                            or record.get("state") != "running" or record.get("shell_pid") != shell_pid):
                        continue
                    if not _current(validate_current):
                        continue
                    candidate = next(candidates, None)
                    payload = b"" if candidate is None else candidate.variable.encode("ascii") + b"\0" + candidate.value.encode("utf-8")
                    connection.sendall(struct.pack("!I", len(payload)) + payload)
                except (OSError, ValueError, UnicodeError, StopIteration):
                    continue

    thread = threading.Thread(target=serve, name="managed-sudo-candidates", daemon=True)
    thread.start()
    try:
        yield str(path)
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=1)
        path.unlink(missing_ok=True)
        temporary.rmdir()


def run_visible_sudo(
    service: ManagedPaneService,
    request: PaneRequest,
    argv: Sequence[str],
    *,
    timeout: float,
    runas_user: str | None = None,
    runas_group: str | None = None,
    environment: Mapping[str, str] | None = None,
    validate_current: Callable[[], bool] | None = None,
    single_password_qualified: bool = False,
) -> dict[str, Any]:
    """Dispatch exact privileged argv through an existing visible managed pane.

    The caller retains the current controller process and its private environment
    until a completion receipt arrives. A timeout is unknown and never replays.
    """
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise PaneError("invalid_timeout", "The visible sudo operation needs a positive timeout.")
    if validate_current is not None and (not callable(validate_current) or not _current(validate_current)):
        raise PaneError("owner_not_current", "The controller owner is no longer current for this sudo operation.")
    if type(single_password_qualified) is not bool:
        raise PaneError("invalid_authentication_policy", "Single-password qualification must be a policy-bound boolean.")
    root = service.state_dir
    current = service.inspect(request)
    if current.state != "ready" or current.binding is None or current.binding.shell_pid is None:
        return current.to_dict()
    operation_id = uuid.uuid4().hex
    def candidate_factory() -> Iterator[Any]:
        return iter_candidates(request.host_alias or request.repository,
                               os.environ if environment is None else environment)

    with _credential_server(root, request, operation_id, current.binding.shell_pid,
                            candidate_factory, validate_current) as socket_path:
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
        dispatch = service.transition(request, transition)
        if dispatch.state != "busy":
            return dispatch.to_dict()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
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
                                "run_id": operation_id, "target": current.binding.pane_id, "tail": ""})
                path = root / "terminal_ops" / request.binding_key / f"{operation_id}.sudo.json"
                try:
                    settled["sudo"] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    settled["sudo"] = {"status": "unknown", "action_required": True}
                return settled
            time.sleep(0.05)
        unknown = service.reconcile(request, operation_id).to_dict()
        unknown.update({"status": "unknown", "run_id": operation_id,
                        "target": current.binding.pane_id,
                        "detail": "No completion receipt was observed; reconcile without replay.", "tail": ""})
        return unknown
