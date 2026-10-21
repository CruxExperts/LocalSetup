"""Operation-bound sudo execution through one transient askpass broker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .credentials import (
    CredentialCandidate,
    candidate_names,
    child_environment,
    iter_candidates,
    normalize_candidate,
    valid_credential,
)

API_VERSION = 1
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_RUNAS = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.-]{0,63}\$?|#[0-9]{1,10})\Z")
_MAX_REQUEST = 4096
_MAX_COMMAND_BYTES = 1024 * 1024
_META_PREFIX = "LS_TERMINAL_SUDO_"


@dataclass(frozen=True, slots=True)
class OperationBinding:
    """Immutable identity supplied by the existing operation owner.

    ``validate_current`` must return exactly ``True`` only while this operation,
    target endpoint and live owner remain current. It is checked before launch
    and again for every askpass request.
    """

    operation_id: str
    owner_id: str
    target: str
    endpoint_id: str
    live_owner_id: str
    payload_digest: str
    validate_current: Callable[[], bool] = field(repr=False, compare=False)
    single_password_qualified: bool = False


@dataclass(frozen=True, slots=True)
class CredentialAttempt:
    variable: str
    status: str


@dataclass(frozen=True, slots=True)
class SudoResult:
    status: str
    capability_status: str
    returncode: int | None
    action_required: bool
    operation_id: str
    owner_id: str
    target: str
    endpoint_id: str
    live_owner_id: str
    credential_attempts: tuple[CredentialAttempt, ...]
    sudo_pid: int | None = None
    sudo_start_time: int | None = None
    detail: str = ""
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False, compare=False)

    def safe_state(self) -> dict[str, object]:
        """Return a JSON-safe receipt fragment with no command or secret data."""
        return {
            "api_version": API_VERSION,
            "status": self.status,
            "capability_status": self.capability_status,
            "returncode": self.returncode,
            "action_required": self.action_required,
            "operation_id": self.operation_id,
            "owner_id": self.owner_id,
            "target": self.target,
            "endpoint_id": self.endpoint_id,
            "live_owner_id": self.live_owner_id,
            "credential_candidates": [
                {"variable": item.variable, "status": item.status}
                for item in self.credential_attempts
            ],
            "sudo_pid": self.sudo_pid,
            "sudo_start_time": self.sudo_start_time,
            "detail": self.detail,
        }


def payload_digest(
    argv: Sequence[str],
    *,
    runas_user: str | None = None,
    runas_group: str | None = None,
) -> str:
    """Return the canonical operation digest for exact argv and runas options."""
    clean_argv = _validate_argv(argv)
    user = _validate_runas("runas_user", runas_user)
    group = _validate_runas("runas_group", runas_group)
    payload = {
        "api_version": API_VERSION,
        "argv": clean_argv,
        "runas_user": user,
        "runas_group": group,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
        raise ValueError("argv must be a non-empty sequence of strings")
    result: list[str] = []
    total = 0
    for part in argv:
        if not isinstance(part, str) or "\0" in part:
            raise ValueError("argv contains an invalid value")
        try:
            encoded = part.encode("utf-8", "strict")
        except UnicodeError as error:
            raise ValueError("argv contains invalid Unicode") from error
        total += len(encoded) + 1
        if total > _MAX_COMMAND_BYTES:
            raise ValueError("argv exceeds the bounded size")
        result.append(part)
    return result


def _validate_runas(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _RUNAS.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


def _safe_identity(name: str, value: str, *, max_length: int = 256) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(character in value for character in "\0\r\n")
    ):
        raise ValueError(f"{name} is invalid")


def _validate_binding(binding: OperationBinding, argv_digest: str) -> None:
    if not isinstance(binding, OperationBinding):
        raise TypeError("binding must be an OperationBinding")
    for name in ("operation_id", "owner_id", "target", "endpoint_id", "live_owner_id"):
        _safe_identity(name, getattr(binding, name))
    if not _HEX64.fullmatch(binding.payload_digest) or binding.payload_digest != argv_digest:
        raise ValueError("operation payload digest does not match exact argv")
    if not callable(binding.validate_current) or type(binding.single_password_qualified) is not bool:
        raise ValueError("operation binding is incomplete")


def _current(binding: OperationBinding) -> bool:
    try:
        return binding.validate_current() is True
    except Exception:
        return False


def _status_callback(
    callback: Callable[[Mapping[str, object]], object] | None,
    binding: OperationBinding,
    event: str,
    status: str,
    *,
    variable: str | None = None,
    attempt: int | None = None,
    sudo_pid: int | None = None,
    sudo_start_time: int | None = None,
) -> bool:
    if callback is None:
        return True
    state: dict[str, object] = {
        "api_version": API_VERSION,
        "operation_id": binding.operation_id,
        "owner_id": binding.owner_id,
        "target": binding.target,
        "endpoint_id": binding.endpoint_id,
        "live_owner_id": binding.live_owner_id,
        "event": event,
        "status": status,
    }
    if variable is not None:
        state["credential_variable"] = variable
    if attempt is not None:
        state["attempt"] = attempt
    if sudo_pid is not None:
        state["sudo_pid"] = sudo_pid
    if sudo_start_time is not None:
        state["sudo_start_time"] = sudo_start_time
    try:
        callback(state)
        return True
    except Exception:
        return False


def _proc_identity(pid: int) -> tuple[int, int] | None:
    """Read Linux proc start time and parent PID for PID-reuse-safe ancestry."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        if closing < 0:
            return None
        fields = raw[closing + 1 :].split()
        # The suffix begins at field 3 (state); starttime is field 22.
        parent_pid = int(fields[1])
        start_time = int(fields[19])
        if parent_pid < 0 or start_time < 0:
            return None
        return parent_pid, start_time
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def _proc_cmdline(pid: int) -> tuple[bytes, ...] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if not raw.endswith(b"\0"):
            return None
        values = tuple(part for part in raw.split(b"\0")[:-1])
        return values if values and all(b"\0" not in item for item in values) else None
    except OSError:
        return None


def _is_descendant_of(peer_pid: int, sudo_pid: int, sudo_start_time: int) -> bool:
    seen: set[int] = set()
    current = peer_pid
    while current > 1 and current not in seen:
        seen.add(current)
        identity = _proc_identity(current)
        if identity is None:
            return False
        parent, start_time = identity
        if current == sudo_pid:
            return start_time == sudo_start_time
        current = parent
    return False


def _read_exact(connection: socket.socket, size: int) -> bytearray:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise OSError("askpass disconnected")
        data.extend(chunk)
    return data


class _AskpassBroker:
    def __init__(
        self,
        binding: OperationBinding,
        environment: Mapping[str, str],
        *,
        resolver: Callable[[str, Mapping[str, str]], Iterable[object]] | None,
        envman_runner: Callable[..., object] | None,
        state_callback: Callable[[Mapping[str, object]], object] | None,
    ) -> None:
        self.binding = binding
        self.environment = environment
        self.resolver = resolver
        self.envman_runner = envman_runner
        self.state_callback = state_callback
        self.nonce = uuid.uuid4().hex
        self.directory: tempfile.TemporaryDirectory[str] | None = None
        self.path: str | None = None
        self.listener: socket.socket | None = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.sudo_pid: int | None = None
        self.sudo_start_time: int | None = None
        self.sudo_cmdline: tuple[bytes, ...] | None = None
        self.callback_count = 0
        self.run_status: str | None = None
        self.detail = ""
        self.statuses: dict[str, str] = {
            variable: "not_checked" for variable in candidate_names(binding.target)
        }
        self._candidate_iterator: Any = None
        self._previous_variable: str | None = None
        self._seen_values: set[str] = set()

    def open(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="ls-sudo-", dir="/tmp")
        os.chmod(self.directory.name, 0o700)
        self.path = os.path.join(self.directory.name, "askpass.sock")
        if len(os.fsencode(self.path)) >= 100:
            raise OSError("private socket path is too long")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(self.path)
            os.chmod(self.path, 0o600)
            directory_info = os.lstat(self.directory.name)
            socket_info = os.lstat(self.path)
            if (
                directory_info.st_uid != os.getuid()
                or not stat.S_ISDIR(directory_info.st_mode)
                or directory_info.st_mode & 0o077
                or socket_info.st_uid != os.getuid()
                or not stat.S_ISSOCK(socket_info.st_mode)
                or socket_info.st_mode & 0o077
            ):
                raise OSError("private socket permissions are invalid")
            listener.listen(2)
            listener.settimeout(0.2)
            self.listener = listener
        except BaseException:
            listener.close()
            raise
        self.thread = threading.Thread(target=self._serve, name="ls-sudo-askpass", daemon=False)
        self.thread.start()

    def register_sudo(self, process: subprocess.Popen[bytes]) -> bool:
        identity = _proc_identity(process.pid)
        command_line = _proc_cmdline(process.pid)
        if identity is None or command_line is None:
            return False
        _parent_pid, start_time = identity
        with self.lock:
            self.sudo_pid = process.pid
            self.sudo_start_time = start_time
            self.sudo_cmdline = command_line
            self.ready.set()
        return True

    def stop(self) -> None:
        # Signal cancellation before waiting for an in-flight lazy EnvMan read.
        # The request handler checks this again immediately before delivery.
        self.stop_event.set()
        with self.lock:
            listener = self.listener
            self.listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def _serve(self) -> None:
        while not self.stop_event.is_set():
            listener = self.listener
            if listener is None:
                return
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                connection.settimeout(2.0)
                self._handle(connection)
            except (OSError, ValueError, TypeError, KeyError, UnicodeError):
                with self.lock:
                    self.run_status = "blocked"
                    self.detail = "askpass request could not be verified"
                self._reply(connection, None)
            finally:
                connection.close()

    def _handle(self, connection: socket.socket) -> None:
        if not self._verify_peer(connection):
            self._set_blocked("askpass peer could not be verified")
            self._reply(connection, None)
            return
        request = _read_exact(connection, 4)
        size = struct.unpack("!I", request)[0]
        if not 1 <= size <= _MAX_REQUEST:
            self._set_blocked("askpass request size was invalid")
            self._reply(connection, None)
            return
        raw = _read_exact(connection, size)
        try:
            payload = json.loads(raw.decode("utf-8", "strict"))
        finally:
            for index in range(len(raw)):
                raw[index] = 0
        if not isinstance(payload, dict) or not self._matches(payload):
            self._set_blocked("askpass operation binding did not match")
            self._reply(connection, None)
            return
        if not self._current_binding():
            self._set_blocked("operation owner changed before askpass")
            self._reply(connection, None)
            return
        with self.lock:
            if self.stop_event.is_set():
                self._reply(connection, None)
                return
            self.callback_count += 1
            ordinal = self.callback_count
            if ordinal > 1 and not self.binding.single_password_qualified:
                self.run_status = "unsupported_authentication"
                self.detail = "repeated askpass prompt is not qualified as a single-password retry"
                self._emit("askpass", "unsupported_authentication", attempt=ordinal)
                self._reply(connection, None)
                return
            if ordinal > 2:
                self.run_status = "unsupported_authentication"
                self.detail = "additional authentication prompts are unsupported"
                self._emit("askpass", "unsupported_authentication", attempt=ordinal)
                self._reply(connection, None)
                return
            if ordinal > 1 and self._previous_variable is not None:
                self.statuses[self._previous_variable] = "retry_requested"
            candidate = self._next_candidate()
            if candidate is None:
                if self.run_status is None:
                    self.run_status = "action_required"
                    self.detail = "no usable automatic credential is available"
                self._emit("askpass", self.run_status, attempt=ordinal)
                self._reply(connection, None)
                return
            if not self._emit("askpass", "candidate_ready", variable=candidate.variable, attempt=ordinal):
                self.statuses[candidate.variable] = "blocked"
                self.run_status = "blocked"
                self.detail = "sanitized operation state could not be recorded"
                self._reply(connection, None)
                return
            if not self._current_binding() or self.stop_event.is_set():
                self.statuses[candidate.variable] = "blocked"
                self.run_status = "blocked"
                self.detail = "operation owner changed before credential delivery"
                self._reply(connection, None)
                return
            secret = bytearray(candidate.value.encode("utf-8", "strict"))
            try:
                delivered = self._reply(connection, secret)
            finally:
                for index in range(len(secret)):
                    secret[index] = 0
            if delivered:
                self._previous_variable = candidate.variable
                self.statuses[candidate.variable] = "provided_to_askpass"
            else:
                self.statuses[candidate.variable] = "blocked"
                self.run_status = "blocked"
                self.detail = "askpass channel disconnected before credential delivery"

    def _current_binding(self) -> bool:
        try:
            return self.binding.validate_current() is True
        except Exception:
            return False

    def _matches(self, payload: Mapping[str, object]) -> bool:
        with self.lock:
            expected = {
                "nonce": self.nonce,
                "operation_id": self.binding.operation_id,
                "owner_id": self.binding.owner_id,
                "target": self.binding.target,
                "endpoint_id": self.binding.endpoint_id,
                "live_owner_id": self.binding.live_owner_id,
                "payload_digest": self.binding.payload_digest,
                "sudo_pid": self.sudo_pid,
                "sudo_start_time": str(self.sudo_start_time),
                "socket": self.path,
            }
            return all(payload.get(name) == value for name, value in expected.items())

    def _verify_peer(self, connection: socket.socket) -> bool:
        peer_option = getattr(socket, "SO_PEERCRED", None)
        if peer_option is None or not self.ready.wait(timeout=2.0):
            return False
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, peer_option, struct.calcsize("3i"))
            peer_pid, peer_uid, _peer_gid = struct.unpack("3i", raw)
        except (OSError, struct.error):
            return False
        with self.lock:
            sudo_pid = self.sudo_pid
            sudo_start_time = self.sudo_start_time
        if (
            peer_pid <= 0
            or peer_uid != os.getuid()
            or sudo_pid is None
            or sudo_start_time is None
            or self.stop_event.is_set()
        ):
            return False
        peer_identity = _proc_identity(peer_pid)
        current_cmdline = _proc_cmdline(sudo_pid)
        return (
            peer_identity is not None
            and peer_identity[0] == sudo_pid
            and _is_descendant_of(peer_pid, sudo_pid, sudo_start_time)
            and current_cmdline is not None
            and current_cmdline == self.sudo_cmdline
        )

    def _next_candidate(self) -> CredentialCandidate | None:
        if self._candidate_iterator is None:
            if self.resolver is None:
                self._candidate_iterator = iter_candidates(
                    self.binding.target,
                    self.environment,
                    envman_runner=self.envman_runner,
                    statuses=self.statuses,
                )
            else:
                self._candidate_iterator = iter(
                    self.resolver(self.binding.target, self.environment)
                )
        for _index in range(2):
            try:
                raw_candidate = next(self._candidate_iterator)
            except StopIteration:
                for variable in candidate_names(self.binding.target):
                    if self.statuses[variable] == "not_checked":
                        self.statuses[variable] = "unavailable"
                return None
            except Exception:
                self.run_status = "blocked"
                self.detail = "credential resolver failed safely"
                return None
            if isinstance(raw_candidate, CredentialCandidate):
                variable, value = raw_candidate.variable, raw_candidate.value
            elif isinstance(raw_candidate, (tuple, list)) and len(raw_candidate) == 2:
                variable, value = raw_candidate
            else:
                variable, value = None, None
            if not isinstance(variable, str) or variable not in candidate_names(self.binding.target):
                self.run_status = "blocked"
                self.detail = "credential resolver returned an invalid candidate"
                return None
            if not valid_credential(value):
                self.statuses[variable] = "invalid"
                continue
            candidate = normalize_candidate(self.binding.target, variable, value)
            if candidate is None:
                self.run_status = "blocked"
                self.detail = "credential resolver returned an invalid candidate"
                return None
            if candidate.value in self._seen_values:
                self.statuses[candidate.variable] = "duplicate"
                continue
            self._seen_values.add(candidate.value)
            self.statuses.setdefault(candidate.variable, "available")
            return candidate
        return None

    def _reply(self, connection: socket.socket, secret: bytearray | None) -> bool:
        try:
            if secret is None:
                connection.sendall(b"N" + struct.pack("!I", 0))
                return True
            if self.stop_event.is_set() or not secret or len(secret) > 4096:
                connection.sendall(b"N" + struct.pack("!I", 0))
                return False
            connection.sendall(b"O" + struct.pack("!I", len(secret)) + secret)
            return True
        except OSError:
            self._set_blocked("askpass channel disconnected")
            return False

    def _set_blocked(self, detail: str) -> None:
        with self.lock:
            self.run_status = "blocked"
            self.detail = detail

    def _emit(
        self,
        event: str,
        status: str,
        *,
        variable: str | None = None,
        attempt: int | None = None,
    ) -> bool:
        return _status_callback(
            self.state_callback,
            self.binding,
            event,
            status,
            variable=variable,
            attempt=attempt,
        )


def _askpass_executable() -> str:
    path = Path(__file__).resolve().parents[2] / "tools" / "sudo_askpass"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise OSError("fixed askpass executable is unavailable")
    if path.stat().st_mode & 0o022:
        raise OSError("fixed askpass executable is writable by another user")
    return str(path)


def _sudo_arguments(
    sudo_path: str,
    argv: list[str],
    *,
    runas_user: str | None,
    runas_group: str | None,
) -> list[str]:
    args = [sudo_path, "-A"]
    if runas_user is not None:
        args.extend(("-u", runas_user))
    if runas_group is not None:
        args.extend(("-g", runas_group))
    args.append("--")
    args.extend(argv)
    return args


def _result(
    binding: OperationBinding,
    broker: _AskpassBroker | None,
    *,
    status: str,
    capability_status: str = "available",
    returncode: int | None = None,
    action_required: bool = False,
    detail: str = "",
    process: subprocess.Popen[bytes] | None = None,
    sudo_pid: int | None = None,
    sudo_start_time: int | None = None,
) -> SudoResult:
    attempts: tuple[CredentialAttempt, ...] = ()
    if broker is not None:
        attempts = tuple(
            CredentialAttempt(variable, value)
            for variable, value in broker.statuses.items()
        )
    return SudoResult(
        status=status,
        capability_status=capability_status,
        returncode=returncode,
        action_required=action_required,
        operation_id=binding.operation_id,
        owner_id=binding.owner_id,
        target=binding.target,
        endpoint_id=binding.endpoint_id,
        live_owner_id=binding.live_owner_id,
        credential_attempts=attempts,
        sudo_pid=sudo_pid,
        sudo_start_time=sudo_start_time,
        detail=detail,
        process=process,
    )


def _wait_for_owned_process(
    process: subprocess.Popen[bytes],
    binding: OperationBinding,
    timeout: float | None,
) -> tuple[int | None, str | None]:
    """Observe completion while checking that the operation owner stays live."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if not _current(binding):
            return None, "owner_changed"
        returncode = process.poll()
        if returncode is not None:
            return returncode, None
        wait_for = 0.25
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "timeout"
            wait_for = min(wait_for, remaining)
        try:
            return process.wait(timeout=wait_for), None
        except subprocess.TimeoutExpired:
            continue


def run_sudo(
    binding: OperationBinding,
    argv: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    resolver: Callable[[str, Mapping[str, str]], Iterable[object]] | None = None,
    envman_runner: Callable[..., object] | None = None,
    runas_user: str | None = None,
    runas_group: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    timeout: float | None = None,
    state_callback: Callable[[Mapping[str, object]], object] | None = None,
) -> SudoResult:
    """Run exact argv through one sudo ``-A`` process and a private broker.

    The broker returns credentials only through the askpass helper's stdout
    pipe. It never takes workload stdin. Manual entry is left to the existing
    visible-terminal owner; this API reports ``action_required`` when automatic
    candidates are exhausted. A timeout returns the still-running process handle
    for caller-owned read-only reconciliation and does not kill or replay it.
    """
    clean_argv = _validate_argv(argv)
    checked_user = _validate_runas("runas_user", runas_user)
    checked_group = _validate_runas("runas_group", runas_group)
    if cwd is not None:
        cwd = os.fspath(cwd)
        if not os.path.isabs(cwd) or "\0" in cwd:
            raise ValueError("cwd must be an absolute NUL-free path")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ValueError("timeout must be positive or None")
    digest = payload_digest(
        clean_argv,
        runas_user=checked_user,
        runas_group=checked_group,
    )
    _validate_binding(binding, digest)
    source_environment = os.environ if environment is None else environment
    child_env = child_environment(source_environment)
    for key in tuple(child_env):
        if key.startswith(_META_PREFIX):
            child_env.pop(key, None)
    if not _current(binding):
        return _result(binding, None, status="blocked", detail="operation owner is not current")
    if not _status_callback(state_callback, binding, "sudo", "starting"):
        return _result(binding, None, status="blocked", detail="sanitized operation state could not be recorded")

    sudo_path = shutil.which("sudo", path=child_env.get("PATH"))
    if not sudo_path:
        return _result(
            binding,
            None,
            status="unavailable",
            capability_status="sudo_missing",
            detail="sudo executable is unavailable",
        )
    try:
        askpass_path = _askpass_executable()
    except OSError:
        return _result(
            binding,
            None,
            status="unavailable",
            capability_status="askpass_missing",
            detail="fixed askpass executable is unavailable",
        )
    if getattr(socket, "SO_PEERCRED", None) is None or not Path("/proc/self/stat").is_file():
        return _result(
            binding,
            None,
            status="unavailable",
            capability_status="peer_verification_unavailable",
            detail="native askpass peer verification is unavailable",
        )
    broker = _AskpassBroker(
        binding,
        source_environment,
        resolver=resolver,
        envman_runner=envman_runner,
        state_callback=state_callback,
    )
    try:
        broker.open()
    except OSError:
        broker.stop()
        return _result(
            binding,
            None,
            status="unavailable",
            capability_status="private_channel_unavailable",
            detail="private askpass channel is unavailable",
        )

    broker_env = child_env
    broker_env["SUDO_ASKPASS"] = askpass_path
    broker_env["LS_TERMINAL_SUDO_SOCKET"] = broker.path or ""
    broker_env["LS_TERMINAL_SUDO_NONCE"] = broker.nonce
    broker_env["LS_TERMINAL_SUDO_OPERATION_ID"] = binding.operation_id
    broker_env["LS_TERMINAL_SUDO_OWNER_ID"] = binding.owner_id
    broker_env["LS_TERMINAL_SUDO_TARGET"] = binding.target
    broker_env["LS_TERMINAL_SUDO_ENDPOINT_ID"] = binding.endpoint_id
    broker_env["LS_TERMINAL_SUDO_LIVE_OWNER_ID"] = binding.live_owner_id
    broker_env["LS_TERMINAL_SUDO_PAYLOAD_DIGEST"] = binding.payload_digest

    process: subprocess.Popen[bytes] | None = None
    sudo_pid: int | None = None
    sudo_start_time: int | None = None
    try:
        if not _current(binding):
            broker.stop()
            return _result(binding, broker, status="blocked", detail="operation owner changed before launch")
        command = _sudo_arguments(
            sudo_path,
            clean_argv,
            runas_user=checked_user,
            runas_group=checked_group,
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=broker_env,
            close_fds=True,
            shell=False,
        )
        sudo_pid = process.pid
        registered = broker.register_sudo(process)
        identity = _proc_identity(process.pid)
        sudo_start_time = identity[1] if identity is not None else None
        if not registered:
            # A NOPASSWD command can exit before /proc exposes a usable
            # command line. Close the broker before observing the exact Popen
            # child; a failed registration must never admit an askpass peer.
            broker.stop()
            with broker.lock:
                no_credential_peer = (
                    not broker.ready.is_set()
                    and broker.callback_count == 0
                    and broker.run_status is None
                )
            if not no_credential_peer or process.poll() is None or not _current(binding):
                return _result(
                    binding,
                    broker,
                    status="unknown",
                    detail="sudo start receipt could not be verified after launch",
                    process=process,
                    sudo_pid=sudo_pid,
                    sudo_start_time=sudo_start_time,
                )
        if not _status_callback(
            state_callback,
            binding,
            "sudo",
            "started",
            sudo_pid=sudo_pid,
            sudo_start_time=sudo_start_time,
        ):
            broker.stop()
            return _result(
                binding,
                broker,
                status="unknown",
                detail="sanitized start state could not be recorded after launch",
                process=process,
                sudo_pid=sudo_pid,
                sudo_start_time=sudo_start_time,
            )
        returncode, wait_status = _wait_for_owned_process(process, binding, timeout)
        if wait_status is not None:
            broker.stop()
            if wait_status == "owner_changed":
                detail = "operation owner changed after launch"
            else:
                detail = "sudo operation timed out; process left running for owner reconciliation"
            _status_callback(
                state_callback,
                binding,
                "sudo",
                "unknown",
                sudo_pid=sudo_pid,
                sudo_start_time=sudo_start_time,
            )
            return _result(
                binding,
                broker,
                status="unknown",
                detail=detail,
                process=process,
                sudo_pid=sudo_pid,
                sudo_start_time=sudo_start_time,
            )
        if not _current(binding):
            broker.stop()
            _status_callback(
                state_callback,
                binding,
                "sudo",
                "unknown",
                sudo_pid=sudo_pid,
                sudo_start_time=sudo_start_time,
            )
            return _result(
                binding,
                broker,
                status="unknown",
                returncode=returncode,
                detail="operation owner changed as sudo completed",
                process=process,
                sudo_pid=sudo_pid,
                sudo_start_time=sudo_start_time,
            )
        broker.stop()
        if broker.run_status == "action_required":
            status = "action_required"
            detail = broker.detail or "manual sudo action is required in the owning terminal"
        elif broker.run_status in {"blocked", "unsupported_authentication"}:
            status = broker.run_status
            detail = broker.detail
        elif returncode == 0:
            status = "completed"
            detail = ""
        else:
            status = "failed"
            detail = "sudo command returned a nonzero status"
        _status_callback(state_callback, binding, "sudo", status)
        return _result(
            binding,
            broker,
            status=status,
            returncode=returncode,
            action_required=status in {"action_required", "unsupported_authentication", "blocked"},
            detail=detail,
            process=process,
            sudo_pid=sudo_pid,
            sudo_start_time=sudo_start_time,
        )
    except (OSError, subprocess.SubprocessError):
        broker.stop()
        return _result(
            binding,
            broker,
            status="unknown" if process is not None else "unavailable",
            action_required=process is None,
            detail=(
                "sudo launch outcome could not be reconciled"
                if process is not None
                else "sudo could not be launched"
            ),
            process=process,
            sudo_pid=sudo_pid,
            sudo_start_time=sudo_start_time,
        )
    finally:
        if process is None:
            broker.stop()


__all__ = [
    "API_VERSION",
    "CredentialAttempt",
    "OperationBinding",
    "SudoResult",
    "payload_digest",
    "run_sudo",
]
