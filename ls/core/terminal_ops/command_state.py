"""Private state, process identity, and locking for agent-owned shell sessions."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import tempfile
import time
from typing import Any, Iterator, Mapping

from .shell import machine_generation


_HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AGENT_ENV_NAMES = (
    "LOCALSETUP_NATIVE_AGENT_ID", "CODEX_AGENT_ID", "CLAUDE_AGENT_ID",
    "CURSOR_AGENT_ID", "OPENCODE_AGENT_ID", "GEMINI_AGENT_ID",
)
_OWNER_PID_ENV = "LOCALSETUP_SHELL_OWNER_PID"
_OWNER_START_ENV = "LOCALSETUP_SHELL_OWNER_STARTTIME"


class ShellCommandError(RuntimeError):
    """A safe shell lifecycle error with a truthful effect classification."""

    def __init__(self, code: str, message: str, *, effect: str = "none") -> None:
        self.code = code
        self.message = message
        self.effect = effect
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "effect": self.effect}


def validate_identifier(value: object, label: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or len(value) > maximum or not _HANDLE.fullmatch(value):
        raise ShellCommandError("invalid_" + label, f"{label.replace('_', ' ')} must be a bounded safe identifier.")
    return value


def private_root_path(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the terminal-ops state root without creating filesystem state."""
    if override is None:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        root = base / "localsetup" / "terminal-ops"
    else:
        root = Path(override).expanduser()
    if not root.is_absolute():
        raise ShellCommandError("invalid_state_root", "Terminal operation state must use an absolute path.")
    return root


def private_root(override: str | os.PathLike[str] | None = None) -> Path:
    """Create or validate the private terminal-ops state root on demand."""
    root = private_root_path(override)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()):
            raise ShellCommandError("unsafe_state_root", "Terminal operation state must be an owned real directory.")
        root.chmod(0o700)
    except OSError as exc:
        raise ShellCommandError("state_unavailable", "Terminal operation state is unavailable.") from exc
    return root


def _safe_directory(path: Path, *, create: bool = False) -> Path:
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
    except OSError as exc:
        raise ShellCommandError("state_unavailable", "The private shell session record is unavailable.") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ShellCommandError("unsafe_state_dir", "Shell session state must be an owner-private directory.")
    return path


def atomic_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool = False) -> None:
    """Write owner-private JSON atomically, optionally claiming a new name."""
    _safe_directory(path.parent, create=True)
    if path.is_symlink():
        raise ShellCommandError("unsafe_state_file", "Shell session state cannot be a symbolic link.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise ShellCommandError("record_exists", "This operation ID already has a durable record.") from exc
            temporary.unlink()
        else:
            os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ShellCommandError("state_unavailable", "Shell operation state could not be stored safely.") from exc
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, *, limit: int = 1_100_000) -> dict[str, Any]:
    try:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit):
            raise ShellCommandError("unsafe_state_file", "The shell operation record is not a bounded private file.")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except ShellCommandError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise ShellCommandError("state_unavailable", "The shell operation record could not be read safely.") from exc
    if not isinstance(value, dict):
        raise ShellCommandError("invalid_state", "The shell operation record must be a JSON object.")
    return value


@contextmanager
def session_lock(directory: Path, *, blocking: bool = False) -> Iterator[None]:
    _safe_directory(directory, create=True)
    path = directory / "session.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ShellCommandError("state_unavailable", "The shell session lock could not be opened safely.") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ShellCommandError("unsafe_state_file", "The shell session lock is not owner-private.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ShellCommandError("busy", "Another shell lifecycle action owns this session.") from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def log_lock(cursor_path: Path) -> Iterator[None]:
    """Serialize one log file's bytes and cursor generation across processes."""
    _safe_directory(cursor_path.parent)
    path = cursor_path.with_name(cursor_path.name + ".lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ShellCommandError("log_unavailable", "The operation log lock is unavailable.") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ShellCommandError("unsafe_state_file", "The operation log lock is not owner-private.")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def process_info(pid: int) -> dict[str, int] | None:
    """Return Linux process ancestry and generation fields, or unknown."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        info = Path(f"/proc/{pid}").stat()
        if info.st_uid != os.getuid():
            return None
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 1 :].split()
        # Fields begin at process state (field 3); starttime is field 22.
        return {
            "pid": pid,
            "ppid": int(fields[1]),
            "pgrp": int(fields[2]),
            "session": int(fields[3]),
            "tty_nr": int(fields[4]),
            "tpgid": int(fields[5]),
            "starttime": int(fields[19]),
        }
    except (OSError, ValueError, IndexError):
        return None


def process_state(pid: int) -> str | None:
    """Return the Linux process state character, including exited zombies."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 1 :].split()
        return fields[0] if fields else None
    except (OSError, ValueError, IndexError):
        return None


def process_environment(pid: int) -> dict[str, str] | None:
    try:
        info = Path(f"/proc/{pid}").stat()
        if info.st_uid != os.getuid():
            return None
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if not separator:
            continue
        try:
            name = key.decode("ascii", "strict")
            decoded = value.decode("utf-8", "strict")
        except UnicodeError:
            continue
        if _ENV.fullmatch(name) and name in _AGENT_ENV_NAMES + (_OWNER_PID_ENV, _OWNER_START_ENV):
            result[name] = decoded
    return result


def ancestor_chain(pid: int | None = None) -> dict[int, dict[str, int]]:
    current = os.getpid() if pid is None else pid
    result: dict[int, dict[str, int]] = {}
    for _ in range(128):
        if current <= 1 or current in result:
            break
        info = process_info(current)
        if info is None:
            break
        result[current] = info
        current = info["ppid"]
    return result


def discover_agent_id(pid: int) -> str | None:
    environment = process_environment(pid)
    if environment is None:
        return None
    found = [environment[name] for name in _AGENT_ENV_NAMES if environment.get(name)]
    if not found:
        return None
    if len(set(found)) != 1:
        raise ShellCommandError("owner_identity_ambiguous", "The native agent identity is ambiguous.")
    value = found[0]
    if len(value) > 256 or any(ord(char) < 0x20 for char in value):
        raise ShellCommandError("owner_identity_invalid", "The native agent identity is malformed.")
    return value


def make_owner_binding(
    *,
    owner_pid: int | None = None,
    owner_starttime: int | None = None,
    agent_id: str | None = None,
    caller_pid: int | None = None,
) -> dict[str, Any]:
    """Qualify an explicit live owner as an ancestor of this native caller."""
    caller = os.getpid() if caller_pid is None else caller_pid
    env = os.environ
    if owner_pid is None:
        raw_pid = env.get(_OWNER_PID_ENV)
        raw_start = env.get(_OWNER_START_ENV)
        if raw_pid and raw_start:
            try:
                owner_pid, owner_starttime = int(raw_pid), int(raw_start)
            except ValueError as exc:
                raise ShellCommandError("owner_identity_invalid", "The configured owner process identity is malformed.") from exc
        elif caller_pid is None:
            # Direct Python API calls bind the actual caller process. The CLI
            # can supply a longer-lived native owner through the explicit args.
            owner_pid = caller
    if type(owner_pid) is not int or owner_pid <= 0:
        raise ShellCommandError("owner_identity_required", "Supply the live agent owner PID and start time.")
    chain = ancestor_chain(caller)
    owner = chain.get(owner_pid)
    if owner is None:
        raise ShellCommandError("owner_not_ancestor", "The explicit shell owner is not in this caller's live process ancestry.")
    if owner_starttime is None:
        owner_starttime = owner["starttime"]
    if type(owner_starttime) is not int or owner_starttime != owner["starttime"]:
        raise ShellCommandError("owner_generation_mismatch", "The shell owner process generation does not match.")
    detected_agent_id = discover_agent_id(owner_pid)
    if agent_id is None:
        agent_id = detected_agent_id
    elif detected_agent_id is not None and detected_agent_id != agent_id:
        raise ShellCommandError("owner_agent_mismatch", "The supplied native agent identity does not match the live owner.")
    if agent_id is not None and (not isinstance(agent_id, str) or len(agent_id) > 256
                                 or not agent_id or any(ord(char) < 0x20 for char in agent_id)):
        raise ShellCommandError("owner_identity_invalid", "The native agent identity is malformed.")
    return {
        "pid": owner_pid,
        "starttime": owner_starttime,
        "agent_id": agent_id,
        "machine_generation": machine_generation(),
        "host": platform.node(),
        "uid": os.getuid(),
    }


def owner_fingerprint(owner: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(owner), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def owner_is_current(owner: Mapping[str, Any], *, caller_pid: int | None = None) -> bool:
    if owner.get("machine_generation") != machine_generation() or owner.get("uid") != os.getuid():
        return False
    pid, starttime = owner.get("pid"), owner.get("starttime")
    if type(pid) is not int or type(starttime) is not int:
        return False
    current = process_info(pid)
    if current is None or current["starttime"] != starttime:
        return False
    if process_state(pid) in {"Z", "X"}:
        return False
    return pid in ancestor_chain(caller_pid)


def owner_liveness(owner: Mapping[str, Any]) -> str:
    """Return live, dead, or unknown without treating inaccessible state as exit."""
    if owner.get("machine_generation") != machine_generation() or owner.get("uid") != os.getuid():
        return "unknown"
    pid, starttime = owner.get("pid"), owner.get("starttime")
    if type(pid) is not int or type(starttime) is not int:
        return "unknown"
    try:
        proc_path = Path(f"/proc/{pid}")
        proc_path.stat()
    except FileNotFoundError:
        return "dead"
    except OSError:
        return "unknown"
    current = process_info(pid)
    if current is None:
        return "unknown"
    state = process_state(pid)
    if state in {"Z", "X"}:
        return "dead"
    if state is None:
        return "unknown"
    return "live" if current["starttime"] == starttime else "dead"


def now() -> float:
    return time.time()
