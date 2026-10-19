"""Explicit Unix-socket transport and private operation-state helpers."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from typing import Any, Iterator, Mapping
import uuid

from .errors import HerdrOpsError


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_UNIX_SOCKET_PATH_BYTES = 103
DEFAULT_TIMEOUT = 5.0
MAX_COMMAND_TIMEOUT = 30.0
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MACHINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_LOCK_DEPTH = threading.local()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _session_path(session: str, config_path: str | None = None) -> Path:
    if not _SESSION.fullmatch(session):
        raise HerdrOpsError("invalid_session", "Session names may use letters, numbers, underscores, and hyphens.")
    config = Path(config_path).expanduser() if config_path else Path.home() / ".config" / "herdr" / "config.toml"
    return config.parent / "herdr.sock" if session == "default" else config.parent / "sessions" / session / "herdr.sock"


def resolve_endpoint(
    *,
    endpoint: str | os.PathLike[str] | None = None,
    session: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> str:
    """Resolve one local Unix socket without consulting focused UI state."""
    env = os.environ if environ is None else environ
    if endpoint:
        path = Path(endpoint).expanduser()
    elif session:
        config_path = env.get("HERDR_CONFIG_PATH")
        if home is not None and not config_path:
            base = Path(home).expanduser() / ".config" / "herdr"
            path = base / "herdr.sock" if session == "default" else base / "sessions" / session / "herdr.sock"
        else:
            path = _session_path(session, config_path)
    elif env.get("HERDR_SOCKET_PATH"):
        path = Path(env["HERDR_SOCKET_PATH"]).expanduser()
    elif env.get("HERDR_SESSION") and env.get("HERDR_ENV") == "1":
        path = _session_path(env["HERDR_SESSION"], env.get("HERDR_CONFIG_PATH"))
    elif env.get("HERDR_ENV") != "1":
        raise HerdrOpsError(
            "endpoint_required",
            "Select an existing Herdr session with --session or provide --socket; implicit lookup requires HERDR_ENV=1.",
        )
    else:
        config = Path(env.get("HERDR_CONFIG_PATH", "")).expanduser() if env.get("HERDR_CONFIG_PATH") else None
        path = (config.parent if config else Path(home).expanduser() / ".config" / "herdr" if home else Path.home() / ".config" / "herdr") / "herdr.sock"
    if not str(path) or "\x00" in str(path) or not path.is_absolute():
        raise HerdrOpsError("invalid_endpoint", "The Herdr socket endpoint must be an absolute local socket path.")
    if len(os.fsencode(path)) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise HerdrOpsError("socket_path_too_long", "Herdr socket paths are limited to 103 bytes on supported Unix systems; use a shorter --socket endpoint.")
    return str(path)


def machine_snapshot(machine: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Read a saved machine snapshot using Herdr's supported CLI forwarding."""
    if not isinstance(machine, str) or not _MACHINE.fullmatch(machine):
        raise HerdrOpsError("invalid_machine", "Provide a valid saved Herdr machine label or ID.")
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    try:
        completed = subprocess.run(
            [binary, "--machine", machine, "api", "snapshot"],
            capture_output=True,
            text=True,
            timeout=min(max(timeout, 0.1), MAX_COMMAND_TIMEOUT),
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise HerdrOpsError("machine_unavailable", "Saved-machine snapshot request failed or timed out.") from None
    if completed.returncode:
        raise HerdrOpsError("machine_request_failed", "Saved-machine snapshot request was rejected.")
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
        raise HerdrOpsError("response_too_large", "Saved-machine snapshot exceeded the response limit.")
    try:
        value = json.loads(completed.stdout, object_pairs_hook=_strict_object)
    except (ValueError, json.JSONDecodeError):
        raise HerdrOpsError("invalid_response", "Saved-machine snapshot was not valid JSON.") from None
    if not isinstance(value, dict):
        raise HerdrOpsError("invalid_response", "Saved-machine snapshot was not a JSON object.")
    return value


def verify_local_herdr_endpoint(endpoint: str | os.PathLike[str], *, timeout: float = 3.0) -> None:
    """Require a local Herdr server peer before staging shell-visible receipts."""
    path = Path(endpoint).expanduser()
    if os.name == "nt":
        raise HerdrOpsError("same_host_unverified", "Task receipts require a verified local Herdr server socket.")
    peer_pid: int | None = None
    if hasattr(socket, "SO_PEERCRED"):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(timeout)
            client.connect(str(path))
            raw = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            peer_pid = int.from_bytes(raw[:4], byteorder=os.sys.byteorder, signed=True)
            peer_uid = int.from_bytes(raw[4:8], byteorder=os.sys.byteorder, signed=False)
            if peer_uid != os.getuid():
                raise HerdrOpsError("same_host_unverified", "Task receipts require a Herdr server owned by the current user.")
        except HerdrOpsError:
            raise
        except OSError:
            raise HerdrOpsError("same_host_unverified", "Could not verify the local Herdr socket peer for task receipts.") from None
        finally:
            client.close()
        if peer_pid is not None and _herdr_server_process(peer_pid):
            return
    elif os.uname().sysname == "Darwin":
        lsof = shutil.which("lsof")
        if lsof:
            try:
                listing = subprocess.run([lsof, "-n", "-U", "-t", str(path)], capture_output=True, text=True, timeout=timeout, check=False)
                pids = [int(line) for line in listing.stdout.splitlines() if line.isdigit()]
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pids = []
            if pids and any(_herdr_server_process(pid, darwin=True) for pid in pids):
                return
    raise HerdrOpsError(
        "same_host_unverified",
        "Task receipt files require a verified local Herdr server; SSH-forwarded endpoints cannot write to this host's state directory.",
    )


def _herdr_server_process(pid: int, *, darwin: bool = False) -> bool:
    if pid <= 0:
        return False
    if not darwin and Path(f"/proc/{pid}/cmdline").is_file():
        try:
            argv = [part.decode("utf-8", errors="replace") for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00") if part]
        except OSError:
            return False
        return bool(argv and Path(argv[0]).name.lower() in {"herdr", "herdr.exe"} and "server" in argv[1:])
    if darwin:
        ps = shutil.which("ps")
        if not ps:
            return False
        try:
            comm = subprocess.run([ps, "-p", str(pid), "-o", "comm="], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
            args = subprocess.run([ps, "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return False
        return Path(comm).name.lower() in {"herdr", "herdr.exe"} and args.split()[1:2] == ["server"]
    return False


def request(
    endpoint: str | os.PathLike[str],
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    mutation: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Send exactly one NDJSON request and never retry a lost response."""
    path = Path(endpoint).expanduser()
    if not path.is_absolute() or "\x00" in str(path):
        raise HerdrOpsError("invalid_endpoint", "The Herdr socket endpoint must be an absolute local socket path.")
    if len(os.fsencode(path)) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise HerdrOpsError("socket_path_too_long", "Herdr socket paths are limited to 103 bytes on supported Unix systems; use a shorter --socket endpoint.")
    if not isinstance(method, str) or not method or any(ord(ch) < 32 for ch in method):
        raise HerdrOpsError("invalid_method", "A valid Herdr API method is required.")
    request_id = f"ls_{uuid.uuid4().hex}"
    payload = json.dumps({"id": request_id, "method": method, "params": dict(params or {})}, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_REQUEST_BYTES:
        raise HerdrOpsError("request_too_large", "Herdr API request exceeded the configured size limit.")
    sent = False
    client: socket.socket | None = None
    try:
        if os.name == "nt":
            raise HerdrOpsError("unsupported_transport", "Raw Herdr sockets currently require a Unix-like local socket endpoint.")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(min(max(timeout, 0.05), MAX_COMMAND_TIMEOUT))
        client.connect(str(path))
        client.sendall(payload)
        sent = True
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise HerdrOpsError("response_too_large", "Herdr API response exceeded the configured size limit.", "unknown" if mutation else "none")
        line = bytes(chunks).split(b"\n", 1)[0]
        if not line:
            raise HerdrOpsError("response_lost", "Herdr closed the socket before returning a response.", "unknown" if mutation and sent else "none")
        response = json.loads(line.decode("utf-8"), object_pairs_hook=_strict_object)
    except HerdrOpsError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise HerdrOpsError("socket_path_too_long", "Herdr rejected the long socket path; use a shorter --socket endpoint.", "unknown" if mutation and sent else "none") from None
        raise HerdrOpsError(
            "transport_error",
            "Herdr socket request failed. Reconcile the operation before retrying." if mutation and sent else "Herdr socket request failed.",
            "unknown" if mutation and sent else "none",
        ) from None
    except (TimeoutError, UnicodeError, ValueError, json.JSONDecodeError):
        raise HerdrOpsError(
            "transport_error",
            "Herdr socket request failed. Reconcile the operation before retrying." if mutation and sent else "Herdr socket request failed.",
            "unknown" if mutation and sent else "none",
        ) from None
    finally:
        if client is not None:
            client.close()
    if not isinstance(response, dict) or response.get("id") != request_id:
        raise HerdrOpsError(
            "response_mismatch",
            "Herdr returned a response for a different request. Reconcile the operation before retrying." if mutation else "Herdr returned a response for a different request.",
            "unknown" if mutation else "none",
        )
    if "error" in response:
        error = response["error"]
        if isinstance(error, dict):
            raise HerdrOpsError(
                str(error.get("code") or "herdr_error"),
                str(error.get("message") or "Herdr rejected the request."),
                "none",
            )
        raise HerdrOpsError("herdr_error", "Herdr rejected the request.", "none")
    result = response.get("result")
    if not isinstance(result, dict):
        raise HerdrOpsError(
            "invalid_response",
            "Herdr returned no structured result. Reconcile the operation before retrying." if mutation else "Herdr returned no structured result.",
            "unknown" if mutation else "none",
        )
    return result


def state_directory(path: str | os.PathLike[str] | None = None, *, create: bool = True) -> Path:
    """Create the caller-selected private state root with owner-only access."""
    if path is None:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        root = base / "localsetup" / "herdr-ops"
    else:
        root = Path(path).expanduser()
    if not root.is_absolute():
        raise HerdrOpsError("invalid_state_dir", "The state directory must be an absolute path.")
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise HerdrOpsError("invalid_state_dir", "The state directory must be a real directory.")
        if create:
            os.chmod(root, 0o700)
        elif not root.exists():
            raise HerdrOpsError("state_dir_unavailable", "The private Herdr state directory does not exist.")
    except OSError:
        raise HerdrOpsError("state_dir_unavailable", "Could not prepare the private Herdr state directory.") from None
    return root


@contextmanager
def writer_lock(path: str | os.PathLike[str] | None = None) -> Iterator[Path]:
    """Serialize state-changing operations across threads and processes."""
    root = state_directory(path)
    key = str(root)
    with _PROCESS_LOCKS_GUARD:
        local_lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())
    with local_lock:
        depths = getattr(_LOCK_DEPTH, "values", None)
        if depths is None:
            depths = _LOCK_DEPTH.values = {}
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield root
            finally:
                depths[key] -= 1
            return
        lock_path = root / ".herdr-ops.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            depths[key] = 1
            try:
                yield root
            finally:
                depths.pop(key, None)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            raise HerdrOpsError("state_lock_failed", "Could not serialize Herdr state changes.") from None
        finally:
            os.close(descriptor)


def atomic_write_json(path: str | os.PathLike[str], value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
    atomic_write_bytes(target, raw)


def atomic_write_bytes(path: str | os.PathLike[str], raw: bytes) -> None:
    """Write private bytes through a synced sibling and atomic replacement."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
        raise HerdrOpsError("state_write_failed", "Could not write an atomic Herdr state receipt.") from None


def read_json_state(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    except FileNotFoundError:
        raise HerdrOpsError("not_found", "No matching Herdr operation was found.") from None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise HerdrOpsError("state_invalid", "Herdr operation state is unreadable or malformed.") from None
    if not isinstance(value, dict):
        raise HerdrOpsError("state_invalid", "Herdr operation state is not a JSON object.")
    return value


def owner_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise HerdrOpsError("invalid_operation_id", "Provide a valid Herdr operation ID.")
    return value


def bounded_wait(value: float, *, maximum: float = 600.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise HerdrOpsError("invalid_timeout", "Wait timeout must be a non-negative number of seconds.")
    return min(float(value), maximum)
