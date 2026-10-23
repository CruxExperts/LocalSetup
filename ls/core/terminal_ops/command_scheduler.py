"""Install and run one owner-authorized, source-bound terminal garbage collector."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from typing import Any, Callable, Iterator, Mapping
import uuid

from .command_state import ShellCommandError, private_root, private_root_path


SCHEDULE_ID = "terminal-shell-gc"
RECIPE_ID = "terminal-session-gc"
CRON_EXPRESSION = "* * * * *"
MAX_CRONTAB_BYTES = 1_048_576
MAX_STATE_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_GRANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_BEGIN_RE = re.compile(
    r"^# BEGIN LOCALSETUP_TERMINAL_GC schedule_id=([a-z][a-z0-9-]{0,63}) binding=([a-f0-9]{64})$"
)
_END_RE = re.compile(
    r"^# END LOCALSETUP_TERMINAL_GC schedule_id=([a-z][a-z0-9-]{0,63}) binding=([a-f0-9]{64})$"
)
_SOURCE_DIRS = ("ls/core/terminal_ops", "ls/core/application_profiles")
_SOURCE_FILES = (
    "ls/__init__.py", "ls/core/__init__.py", "ls/core/paths.py",
    "ls/core/models.py", "ls/core/schema.py", "pyproject.toml", "uv.lock",
)
_SOURCE_CONFIG_GLOBS = (
    "ls/config/application-profile*.schema.json",
    "ls/config/application-profiles/**/*.json",
)


class SchedulerError(RuntimeError):
    """A schedule could not be safely prepared, reconciled, or run."""

    def __init__(self, code: str, message: str, *, effect: str = "none") -> None:
        self.code = code
        self.message = message
        self.effect = effect
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "effect_certainty": self.effect}


@dataclass(frozen=True, slots=True)
class CrontabSnapshot:
    """Current user's crontab, distinguishing no crontab from an empty one."""

    present: bool
    content: str


@dataclass(frozen=True, slots=True)
class CronWriteResult:
    """One crontab write attempt; return code may be unknown after a timeout."""

    returncode: int | None
    detail: str = ""


class SubprocessCrontabBackend:
    """Small current-user crontab adapter. It never selects another account."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def read_crontab(self) -> CrontabSnapshot:
        try:
            result = subprocess.run(
                ["crontab", "-l"], capture_output=True, timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SchedulerError("crontab_read_failed", "The current user's crontab could not be read.") from exc
        try:
            output = result.stdout.decode("utf-8", errors="strict")
            error = result.stderr.decode("utf-8", errors="replace")
        except UnicodeError as exc:
            raise SchedulerError("crontab_encoding_invalid", "The current user's crontab is not valid UTF-8.") from exc
        if len(result.stdout) > MAX_CRONTAB_BYTES:
            raise SchedulerError("crontab_too_large", "The current user's crontab exceeds the supported size.")
        if result.returncode == 0:
            return CrontabSnapshot(True, output)
        if result.returncode == 1 and not output and "no crontab for" in error.lower():
            return CrontabSnapshot(False, "")
        raise SchedulerError("crontab_read_failed", "The current user's crontab could not be read.")

    def write_crontab(self, content: str) -> CronWriteResult:
        try:
            result = subprocess.run(
                ["crontab", "-"], input=content.encode("utf-8"),
                capture_output=True, timeout=self.timeout, check=False,
            )
            detail = result.stderr.decode("utf-8", errors="replace")[:2048]
            return CronWriteResult(result.returncode, detail)
        except subprocess.TimeoutExpired as exc:
            # The utility may have installed the file before the timeout.
            return CronWriteResult(None, f"write timed out: {exc.timeout}")
        except OSError as exc:
            # Reconciliation still runs because dispatch may have reached a wrapper.
            return CronWriteResult(None, f"write could not be confirmed: {type(exc).__name__}")

    def validate_crontab(self, content: str, directory: Path) -> None:
        """Ask the installed cron parser to check a private fragment without installing it."""
        # Some cron implementations use a short internal filename buffer for -n.
        descriptor, name = tempfile.mkstemp(prefix="ls-cron-", dir=tempfile.gettempdir())
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content.encode("utf-8"))
            result = subprocess.run(
                ["crontab", "-n", str(path)], capture_output=True,
                timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SchedulerError("crontab_validation_unavailable", "The installed crontab could not validate the proposed entry.") from exc
        finally:
            path.unlink(missing_ok=True)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:256]
            raise SchedulerError("crontab_validation_failed", f"The installed crontab rejected the proposed entry: {detail}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SchedulerError("invalid_schedule_data", "Schedule data must be bounded finite JSON.") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_identifier(value: str, *, label: str = "schedule id") -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SchedulerError("invalid_schedule_id", f"{label} must be a lowercase letter followed by lowercase letters, digits, or hyphens.")
    if value != SCHEDULE_ID:
        raise SchedulerError("unsupported_schedule_id", f"Only the registered {SCHEDULE_ID!r} schedule is supported.")
    return value


def _source_root(value: str | os.PathLike[str]) -> Path:
    requested = Path(value).expanduser()
    raw = str(requested)
    if not requested.is_absolute() or requested.is_symlink() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise SchedulerError("invalid_source_root", "The profile source root must be an absolute real directory.")
    try:
        root = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SchedulerError("invalid_source_root", "The profile source root must be an existing real directory.") from exc
    if not root.is_dir() or not (root / "ls" / "core" / "terminal_ops" / "command_service.py").is_file():
        raise SchedulerError("invalid_source_root", "The source root does not contain the terminal operation service.")
    return root


def _state_root(value: str | os.PathLike[str] | None) -> Path:
    root = private_root_path(value)
    raw = str(root)
    if not root.is_absolute() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise SchedulerError("invalid_state_root", "The terminal operation state root must be an absolute path.")
    if root.is_symlink():
        raise SchedulerError("unsafe_state_root", "The terminal operation state root cannot be a symbolic link.")
    return Path(os.path.normpath(str(root)))


def _reject_overlapping_roots(source_root: Path, state_root: Path) -> None:
    source = source_root.resolve()
    state = state_root.resolve(strict=False)
    if source == state or source in state.parents or state in source.parents:
        raise SchedulerError("overlapping_roots", "Source and private state roots must be separate trees.")


def _source_paths(source_root: Path) -> list[Path]:
    paths: set[Path] = set()
    for relative in _SOURCE_FILES:
        paths.add(source_root / relative)
    for directory in _SOURCE_DIRS:
        base = source_root / directory
        if not base.is_dir() or base.is_symlink():
            raise SchedulerError("source_incomplete", f"Required source directory is missing: {directory}.")
        if any(path.is_symlink() for path in base.rglob("*")):
            raise SchedulerError("source_unsafe", f"Required source directory contains a symbolic link: {directory}.")
        paths.update(path for path in base.rglob("*.py") if path.is_file())
    for pattern in _SOURCE_CONFIG_GLOBS:
        paths.update(path for path in source_root.glob(pattern) if path.is_file())
    result: list[Path] = []
    total = 0
    for path in sorted(paths, key=lambda item: item.relative_to(source_root).as_posix()):
        try:
            relative = path.relative_to(source_root)
            info = path.lstat()
        except (OSError, ValueError) as exc:
            raise SchedulerError("source_incomplete", "A required source file cannot be inspected.") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_SOURCE_BYTES
        ):
            raise SchedulerError("source_unsafe", f"A required source file is not a bounded regular file: {relative}.")
        total += info.st_size
        if total > MAX_SOURCE_BYTES:
            raise SchedulerError("source_too_large", "The source fingerprint input exceeds its size limit.")
        result.append(path)
    if not result:
        raise SchedulerError("source_incomplete", "No scheduler source files were found.")
    return result


def source_fingerprint(source_root: str | os.PathLike[str]) -> str:
    """Hash the scheduler and its terminal/profile implementation inputs."""
    root = _source_root(source_root)
    digest = hashlib.sha256()
    for path in _source_paths(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SchedulerError("source_incomplete", "A source file changed while it was fingerprinted.") from exc
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _runtime_binding(source_root: Path) -> dict[str, Any]:
    """Bind the active interpreter and declared project dependency versions."""
    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise SchedulerError("runtime_unqualified", "The active Python executable must have an absolute path.")
    try:
        project = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = project["project"]["dependencies"]
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise ValueError("project.dependencies must be a list of strings")
        names = sorted({
            re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", item).group(1).lower().replace("_", "-")
            for item in dependencies
            if re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", item) is not None
        })
        if len(names) != len(dependencies):
            raise ValueError("a dependency name is malformed")
        versions: list[list[str | None]] = []
        for name in names:
            try:
                installed: str | None = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            versions.append([name, installed])
        target = executable.resolve(strict=True)
    except (OSError, RuntimeError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SchedulerError("runtime_unavailable", "The scheduled Python environment could not be bound safely.") from exc
    return {
        "schema_version": 1,
        "executable": str(executable),
        "executable_target": str(target),
        "implementation": sys.implementation.name,
        "python_version": list(sys.version_info[:3]),
        "prefix": str(Path(sys.prefix).absolute()),
        "base_prefix": str(Path(sys.base_prefix).absolute()),
        "dependencies": versions,
    }


def _schedule_state_dir(state_root: Path, *, create: bool) -> Path | None:
    terminal_root = private_root_path(state_root)
    schedule_dir = terminal_root / "schedules"
    if create:
        try:
            private_root(terminal_root)
            schedule_dir.mkdir(mode=0o700, exist_ok=True)
            info = schedule_dir.lstat()
        except (OSError, ShellCommandError) as exc:
            raise SchedulerError("state_unavailable", "Private scheduler state could not be prepared.") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SchedulerError("unsafe_state_dir", "Scheduler state must be an owned real directory.")
        os.chmod(schedule_dir, 0o700)
        return schedule_dir
    if not schedule_dir.exists():
        return None
    try:
        info = schedule_dir.lstat()
    except OSError as exc:
        raise SchedulerError("state_unavailable", "Private scheduler state could not be read.") from exc
    if (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid() or info.st_mode & 0o077
    ):
        raise SchedulerError("unsafe_state_dir", "Scheduler state must be owner-private.")
    return schedule_dir


def _record_path(directory: Path, schedule_id: str) -> Path:
    return directory / f"{schedule_id}.json"


def _read_record(directory: Path | None, schedule_id: str) -> dict[str, Any] | None:
    if directory is None:
        return None
    path = _record_path(directory, schedule_id)
    if not path.exists():
        return None
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_STATE_BYTES
        ):
            raise SchedulerError("unsafe_state_file", "The scheduler binding is not a bounded private file.")
        raw = path.read_bytes()
    except SchedulerError:
        raise
    except OSError as exc:
        raise SchedulerError("state_unavailable", "The scheduler binding could not be read safely.") from exc

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SchedulerError("invalid_state", "The scheduler binding contains duplicate JSON properties.")
            result[key] = value
        return result

    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SchedulerError("invalid_state", "The scheduler binding is not valid UTF-8 JSON.") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("schedule_id") != schedule_id
    ):
        raise SchedulerError("invalid_state", "The scheduler binding has an unsupported or mismatched identity.")
    if (
        payload.get("status") not in {"installing", "enabled", "install_uncertain", "revoked"}
        or not isinstance(payload.get("binding"), dict)
        or not isinstance(payload.get("binding_sha256"), str)
        or not _HEX64.fullmatch(payload["binding_sha256"])
    ):
        raise SchedulerError("invalid_state", "The scheduler binding is missing required state or digest fields.")
    return payload


def _atomic_write(path: Path, content: bytes) -> None:
    directory = path.parent
    try:
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SchedulerError("unsafe_state_dir", "Private scheduler state must be owner-private.")
        if path.is_symlink():
            raise SchedulerError("unsafe_state_file", "Scheduler state cannot be a symbolic link.")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    except SchedulerError:
        raise
    except OSError as exc:
        raise SchedulerError("state_unavailable", "Scheduler state could not be written atomically.") from exc


def _write_record(directory: Path, record: Mapping[str, Any]) -> None:
    raw = _canonical(dict(record)) + b"\n"
    if len(raw) > MAX_STATE_BYTES:
        raise SchedulerError("state_too_large", "The scheduler binding exceeds its size limit.")
    _atomic_write(_record_path(directory, str(record["schedule_id"])), raw)


def _delete_record(directory: Path, schedule_id: str) -> None:
    path = _record_path(directory, schedule_id)
    if path.is_symlink():
        raise SchedulerError("unsafe_state_file", "Scheduler state cannot be a symbolic link.")
    try:
        path.unlink(missing_ok=True)
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SchedulerError("state_unavailable", "A failed scheduler binding could not be removed.") from exc


@contextmanager
def _file_lock(directory: Path, name: str, *, blocking: bool) -> Iterator[None]:
    path = directory / name
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise SchedulerError("lock_unavailable", "The scheduler lock could not be opened safely.") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SchedulerError("unsafe_lock", "Scheduler locks must be owner-private regular files.")
        try:
            operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise SchedulerError("busy", "Another scheduler action is already in progress.") from exc
        yield
    finally:
        os.close(descriptor)


def _snapshot(backend: Any) -> CrontabSnapshot:
    try:
        current = backend.read_crontab()
    except SchedulerError:
        raise
    except Exception as exc:
        raise SchedulerError("crontab_read_failed", "The current user's crontab could not be read.") from exc
    try:
        encoded_size = len(current.content.encode("utf-8", errors="strict")) if isinstance(current, CrontabSnapshot) and isinstance(current.content, str) else MAX_CRONTAB_BYTES + 1
    except UnicodeError as exc:
        raise SchedulerError("crontab_encoding_invalid", "The current user's crontab is not valid UTF-8.") from exc
    if (
        not isinstance(current, CrontabSnapshot)
        or type(current.present) is not bool
        or not isinstance(current.content, str)
        or encoded_size > MAX_CRONTAB_BYTES
        or "\x00" in current.content
        or (not current.present and current.content)
    ):
        raise SchedulerError("crontab_invalid", "The crontab backend returned invalid or oversized content.")
    return current


def _snapshot_equal(left: CrontabSnapshot, right: CrontabSnapshot) -> bool:
    if left.content != right.content:
        return False
    # Cron implementations may represent an empty crontab as absent or present.
    return left.present == right.present or left.content == ""


def _backup_crontab(directory: Path, schedule_id: str, snapshot: CrontabSnapshot) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = directory / f"crontab-{schedule_id}-{stamp}-{uuid.uuid4().hex}.tar.gz"
    meta = _canonical({"schema_version": 1, "present": snapshot.present}) + b"\n"
    payload = snapshot.content.encode("utf-8")
    buffer = io.BytesIO()
    try:
        with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9) as archive:
            for name, data in (("metadata.json", meta), ("crontab.txt", payload)):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o600
                member.mtime = int(datetime.now(timezone.utc).timestamp())
                archive.addfile(member, io.BytesIO(data))
    except (OSError, tarfile.TarError) as exc:
        raise SchedulerError("backup_failed", "The prior crontab could not be archived safely.") from exc
    _atomic_write(target, buffer.getvalue())
    return target


def _binding_digest(binding: Mapping[str, Any]) -> str:
    return _sha256(_canonical(dict(binding)))


def _recipe() -> dict[str, Any]:
    return {
        "id": RECIPE_ID,
        "version": 1,
        "schedule": CRON_EXPRESSION,
        "action": "ls.core.terminal_ops.command_service.ShellCommandService.gc",
        "overlap": "nonblocking-owner-private-lock",
    }


def _normalize_authority(
    grant_id: str,
    authority_owner: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id):
        raise SchedulerError("invalid_grant", "An explicit bounded recurring-grant ID is required.")
    from .command_state import make_owner_binding

    if authority_owner is None:
        owner = make_owner_binding()
    else:
        if set(authority_owner) - {"pid", "starttime", "agent_id"}:
            raise SchedulerError("invalid_grant", "Authority owner data contains unsupported fields.")
        owner = make_owner_binding(
            owner_pid=authority_owner.get("pid"),
            owner_starttime=authority_owner.get("starttime"),
            agent_id=authority_owner.get("agent_id"),
        )
    return {
        "grant_id": grant_id,
        "scope": RECIPE_ID,
        "durability": "durable-user-crontab-gc-v1",
        "owner": owner,
    }


def _validate_authority(authority: Any) -> bool:
    if not isinstance(authority, dict) or set(authority) != {"grant_id", "scope", "durability", "owner"}:
        return False
    if not isinstance(authority.get("grant_id"), str) or not _GRANT_ID.fullmatch(authority["grant_id"]):
        return False
    owner = authority.get("owner")
    return (
        authority.get("scope") == RECIPE_ID
        and authority.get("durability") == "durable-user-crontab-gc-v1"
        and isinstance(owner, dict)
        and set(owner) == {"pid", "starttime", "agent_id", "machine_generation", "host", "uid"}
        and type(owner.get("pid")) is int and owner["pid"] > 0
        and type(owner.get("starttime")) is int and owner["starttime"] > 0
        and (owner.get("agent_id") is None or isinstance(owner.get("agent_id"), str))
        and isinstance(owner.get("machine_generation"), str)
        and isinstance(owner.get("host"), str)
        and type(owner.get("uid")) is int
    )


def _authority_runtime_state(authority: Mapping[str, Any]) -> str:
    """Durable jobs survive owner exit, but stay on the enrolled host/user/boot."""
    owner = authority.get("owner")
    if not isinstance(owner, Mapping):
        return "authority_machine_mismatch"
    import platform
    from .command_state import machine_generation

    if owner.get("host") != platform.node() or owner.get("uid") != os.getuid():
        return "authority_machine_mismatch"
    if owner.get("machine_generation") != machine_generation():
        return "reboot_rebind_required"
    return "bound"


def _enrollment_liveness(owner: Mapping[str, Any]) -> str:
    """Expose installer process liveness as provenance, never as durable authority."""
    try:
        from .command_state import owner_liveness

        return owner_liveness(owner)
    except (OSError, ShellCommandError):
        return "unknown"


def _same_host_reboot_rebind(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """Permit explicit post-reboot re-enrollment without broadening the grant."""
    old_authority = old.get("authority")
    new_authority = new.get("authority")
    if not isinstance(old_authority, dict) or not isinstance(new_authority, dict):
        return False
    old_owner = old_authority.get("owner")
    new_owner = new_authority.get("owner")
    if not isinstance(old_owner, dict) or not isinstance(new_owner, dict):
        return False
    old_base = {key: value for key, value in old.items() if key != "authority"}
    new_base = {key: value for key, value in new.items() if key != "authority"}
    old_grant = {key: value for key, value in old_authority.items() if key != "owner"}
    new_grant = {key: value for key, value in new_authority.items() if key != "owner"}
    return (
        old_base == new_base
        and old_grant == new_grant
        and old_owner.get("host") == new_owner.get("host")
        and old_owner.get("uid") == new_owner.get("uid")
        and old_owner.get("agent_id") == new_owner.get("agent_id")
        and old_owner.get("machine_generation") != new_owner.get("machine_generation")
        and _authority_runtime_state(new_authority) == "bound"
    )


def _binding(
    *, schedule_id: str, source_root: Path, state_root: Path,
    source_sha256: str, authority: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "schedule_id": schedule_id,
        "recipe": _recipe(),
        "source": {"root": str(source_root), "sha256": source_sha256},
        "runtime": _runtime_binding(source_root),
        "state_root": str(state_root),
        "authority": dict(authority),
    }


def _line_command(
    source_root: Path,
    state_root: Path,
    schedule_id: str,
    binding_sha256: str,
    source_sha256: str,
) -> str:
    python_binary = Path(sys.executable)
    if not python_binary.is_absolute():
        raise SchedulerError("scheduler_runtime_missing", "An absolute Python executable is required.")
    bootstrap = _bootstrap_path(state_root, schedule_id, binding_sha256)
    argv = [
        str(python_binary), "-I", "-B", "-c", _bootstrap_loader(),
        str(bootstrap), _sha256(_bootstrap_code().encode("utf-8")),
        str(source_root), source_sha256, binding_sha256,
    ]
    # Cron treats percent specially before shell parsing, even inside quotes.
    return shlex.join(argv).replace("%", r"\%")


def _bootstrap_path(state_root: Path, schedule_id: str, binding_sha256: str) -> Path:
    return state_root / "schedules" / f"{schedule_id}-{binding_sha256}.py"


def _bootstrap_loader() -> str:
    """Check private bootstrap bytes before executing any project code."""
    return (
        "import hashlib,os,stat,sys;"
        "p=sys.argv[1];d=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0));"
        "s=os.fstat(d);b=os.read(d,16385);os.close(d);"
        "(stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and not s.st_mode&0o077 and len(b)<16385 and hashlib.sha256(b).hexdigest()==sys.argv[2]) or sys.exit(78);"
        "sys.argv=[p]+sys.argv[3:];exec(compile(b,p,'exec'))"
    )


def _ensure_bootstrap(directory: Path, state_root: Path, schedule_id: str, binding_sha256: str) -> None:
    path = _bootstrap_path(state_root, schedule_id, binding_sha256)
    expected = _bootstrap_code().encode("utf-8")
    if path.parent != directory:
        raise SchedulerError("unsafe_bootstrap", "The bootstrap path is outside the private schedule directory.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        _atomic_write(path, expected)
        return
    except OSError as exc:
        raise SchedulerError("bootstrap_unavailable", "The private bootstrap cannot be inspected.") from exc
    try:
        matches = (
            stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077 and info.st_size == len(expected)
            and path.read_bytes() == expected
        )
    except OSError as exc:
        raise SchedulerError("bootstrap_unavailable", "The private bootstrap cannot be read.") from exc
    if not matches:
        raise SchedulerError("bootstrap_drift", "The private bootstrap changed; scheduled dispatch is refused.")


def _bootstrap_matches(state_root: Path, schedule_id: str, binding_sha256: str) -> bool:
    path = _bootstrap_path(state_root, schedule_id, binding_sha256)
    try:
        info = path.lstat()
        return (
            stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077
            and path.read_bytes() == _bootstrap_code().encode("utf-8")
        )
    except OSError:
        return False


def _block_markers(schedule_id: str, binding_sha256: str) -> tuple[str, str]:
    return (
        f"# BEGIN LOCALSETUP_TERMINAL_GC schedule_id={schedule_id} binding={binding_sha256}",
        f"# END LOCALSETUP_TERMINAL_GC schedule_id={schedule_id} binding={binding_sha256}",
    )


def _generated_block(
    source_root: Path, state_root: Path, schedule_id: str, binding_sha256: str,
    source_sha256: str,
) -> str:
    begin, end = _block_markers(schedule_id, binding_sha256)
    command = _line_command(
        source_root, state_root, schedule_id, binding_sha256, source_sha256,
    )
    return f"{begin}\n{CRON_EXPRESSION} {command}\n{end}\n"


def _bootstrap_code() -> str:
    """Return a stdlib-only pre-import verifier for the installed source digest."""
    code = (
        "import hashlib, importlib.util, pathlib, stat, sys\n"
        f"SOURCE_DIRS={_SOURCE_DIRS!r}\n"
        f"SOURCE_FILES={_SOURCE_FILES!r}\n"
        f"SOURCE_CONFIG_GLOBS={_SOURCE_CONFIG_GLOBS!r}\n"
        f"MAX_SOURCE_BYTES={MAX_SOURCE_BYTES}\n"
        "def refuse():\n"
        " sys.stderr.write('LocalSetup scheduled GC refused: source binding mismatch or unsafe source.\\n')\n"
        " raise SystemExit(78)\n"
        "try:\n"
        " root=pathlib.Path(sys.argv[1]).resolve(strict=True)\n"
        " expected=sys.argv[2]\n"
        " if not expected or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected): refuse()\n"
        " paths={root/p for p in SOURCE_FILES}\n"
        " for rel in SOURCE_DIRS:\n"
        "  base=root/rel\n"
        "  if not base.is_dir() or base.is_symlink(): refuse()\n"
        "  if any(path.is_symlink() for path in base.rglob('*')): refuse()\n"
        "  paths.update(path for path in base.rglob('*.py') if path.is_file())\n"
        " for pattern in SOURCE_CONFIG_GLOBS: paths.update(path for path in root.glob(pattern) if path.is_file())\n"
        " digest=hashlib.sha256(); total=0; verified={}\n"
        " for path in sorted(paths,key=lambda item:item.relative_to(root).as_posix()):\n"
        "  info=path.lstat(); rel=path.relative_to(root).as_posix().encode('utf-8')\n"
        "  if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size>MAX_SOURCE_BYTES: refuse()\n"
        "  total+=info.st_size\n"
        "  if total>MAX_SOURCE_BYTES: refuse()\n"
        "  data=path.read_bytes(); digest.update(len(rel).to_bytes(4,'big')); digest.update(rel)\n"
        "  digest.update(len(data).to_bytes(8,'big')); digest.update(data)\n"
        "  if rel.startswith(b'ls/') and rel.endswith(b'.py'): verified[rel.decode('utf-8')]=data\n"
        " if digest.hexdigest()!=expected: refuse()\n"
        "except (OSError,RuntimeError,ValueError,IndexError): refuse()\n"
        f"if pathlib.Path(sys.argv[0]).name!={SCHEDULE_ID!r}+'-'+sys.argv[3]+'.py': refuse()\n"
        "if any(name=='ls' or name.startswith('ls.') for name in sys.modules): refuse()\n"
        "class VerifiedLocalSetup:\n"
        " def find_spec(self,fullname,path=None,target=None):\n"
        "  if fullname!='ls' and not fullname.startswith('ls.'): return None\n"
        "  stem=fullname.replace('.','/')\n"
        "  package=stem+'/__init__.py'\n"
        "  rel=package if package in verified else stem+'.py'\n"
        "  if rel not in verified: raise ImportError('unbound LocalSetup module: '+fullname)\n"
        "  return importlib.util.spec_from_loader(fullname,self,origin=str(root/rel),is_package=rel==package)\n"
        " def create_module(self,spec): return None\n"
        " def exec_module(self,module):\n"
        "  stem=module.__name__.replace('.','/')\n"
        "  rel=stem+'/__init__.py' if module.__spec__.submodule_search_locations is not None else stem+'.py'\n"
        "  module.__file__=str(root/rel)\n"
        "  exec(compile(verified[rel],str(root/rel),'exec'),module.__dict__)\n"
        "sys.meta_path.insert(0,VerifiedLocalSetup())\n"
        "from ls.core.terminal_ops.command_scheduler import main\n"
        f"raise SystemExit(main(['run-bound','--source-root',str(root),'--state-root',str(pathlib.Path(sys.argv[0]).parent.parent),'--schedule-id',{SCHEDULE_ID!r},'--binding-sha256',sys.argv[3]]))"
    )
    return code


def _remove_block(content: str, schedule_id: str) -> tuple[str, str | None, str | None]:
    lines = content.splitlines(keepends=True)
    begins: list[int] = []
    ends: list[int] = []
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if text.startswith(f"# BEGIN LOCALSETUP_TERMINAL_GC schedule_id={schedule_id}"):
            if _BEGIN_RE.fullmatch(text) is None:
                raise SchedulerError("marker_corrupt", "A managed scheduler start marker is malformed.")
            begins.append(index)
        if text.startswith(f"# END LOCALSETUP_TERMINAL_GC schedule_id={schedule_id}"):
            if _END_RE.fullmatch(text) is None:
                raise SchedulerError("marker_corrupt", "A managed scheduler end marker is malformed.")
            ends.append(index)
    if not begins and not ends:
        return content, None, None
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise SchedulerError("marker_ambiguous", "The managed scheduler block is duplicated or incomplete.")
    begin_text = lines[begins[0]].rstrip("\r\n")
    end_text = lines[ends[0]].rstrip("\r\n")
    begin_match = _BEGIN_RE.fullmatch(begin_text)
    end_match = _END_RE.fullmatch(end_text)
    assert begin_match is not None and end_match is not None
    if begin_match.group(2) != end_match.group(2):
        raise SchedulerError("marker_mismatch", "The managed scheduler markers do not share one binding.")
    block = "".join(lines[begins[0]:ends[0] + 1])
    remaining = "".join(lines[:begins[0]] + lines[ends[0] + 1:])
    return remaining, block, begin_match.group(2)


def _append_block(content: str, block: str) -> str:
    prefix = content
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    return prefix + block


def _replace_block(content: str, block: str, schedule_id: str) -> tuple[str, str | None, str | None]:
    remaining, old_block, old_digest = _remove_block(content, schedule_id)
    return _append_block(remaining, block), old_block, old_digest


def _write_and_reconcile(
    backend: Any,
    previous: CrontabSnapshot,
    desired_content: str,
) -> tuple[str, CrontabSnapshot | None, str]:
    try:
        write_result = backend.write_crontab(desired_content)
        if not isinstance(write_result, CronWriteResult):
            raise SchedulerError("crontab_backend_invalid", "The crontab backend returned an invalid write receipt.")
        detail = write_result.detail[:2048]
    except Exception as exc:  # Readback determines whether the request took effect.
        detail = f"write result unavailable: {type(exc).__name__}"
    try:
        observed = _snapshot(backend)
    except SchedulerError:
        return "uncertain", None, detail
    if observed.content == desired_content and (observed.present or desired_content == ""):
        return "applied", observed, detail
    if _snapshot_equal(observed, previous):
        return "not_applied", observed, detail
    return "uncertain", observed, detail


def _state_record(
    *, schedule_id: str, status: str, binding: Mapping[str, Any], binding_sha256: str,
    prior: Mapping[str, Any] | None = None, **updates: Any,
) -> dict[str, Any]:
    record = {
        "schema_version": 1,
        "schedule_id": schedule_id,
        "status": status,
        "binding": dict(binding),
        "binding_sha256": binding_sha256,
        "created_at": prior.get("created_at", _utc_now()) if prior else _utc_now(),
        "updated_at": _utc_now(),
    }
    if prior and prior.get("binding_sha256") == binding_sha256:
        for key in ("last_run", "last_result"):
            if key in prior:
                record[key] = prior[key]
    record.update(updates)
    return record


def install_schedule(
    schedule_id: str = SCHEDULE_ID,
    *,
    source_root: str | os.PathLike[str],
    state_root: str | os.PathLike[str] | None = None,
    grant_id: str,
    authority_owner: Mapping[str, Any] | None = None,
    backend: Any | None = None,
) -> dict[str, Any]:
    """Install the fixed one-minute GC recipe in the current user's crontab."""
    schedule_id = _safe_identifier(schedule_id)
    source = _source_root(source_root)
    state = _state_root(state_root)
    _reject_overlapping_roots(source, state)
    source_sha256 = source_fingerprint(source)
    authority = _normalize_authority(grant_id, authority_owner)
    binding = _binding(
        schedule_id=schedule_id, source_root=source, state_root=state,
        source_sha256=source_sha256, authority=authority,
    )
    binding_sha256 = _binding_digest(binding)
    block = _generated_block(
        source, state, schedule_id, binding_sha256, source_sha256,
    )
    directory = _schedule_state_dir(state, create=True)
    assert directory is not None
    crontab = backend or SubprocessCrontabBackend()
    with _file_lock(directory, "crontab.lock", blocking=False):
        previous = _snapshot(crontab)
        desired, old_block, old_digest = _replace_block(previous.content, block, schedule_id)
        if len(desired.encode("utf-8")) > MAX_CRONTAB_BYTES:
            raise SchedulerError("crontab_too_large", "The resulting crontab exceeds the supported size.")
        prior = _read_record(directory, schedule_id)
        if prior is not None:
            prior_binding = prior.get("binding", {})
            prior_digest = prior.get("binding_sha256")
            if (
                not isinstance(prior_binding, dict)
                or not isinstance(prior_digest, str)
                or not _HEX64.fullmatch(prior_digest)
                or _binding_digest(prior_binding) != prior_digest
                or prior_binding.get("schema_version") != 1
                or prior_binding.get("schedule_id") != schedule_id
                or prior_binding.get("recipe") != _recipe()
                or prior_binding.get("state_root") != str(state)
                or not isinstance(prior_binding.get("runtime"), dict)
                or not _validate_authority(prior_binding.get("authority"))
            ):
                raise SchedulerError("binding_corrupt", "The prior private schedule binding is invalid.")
            prior_authority = prior_binding.get("authority", {}) if isinstance(prior_binding, dict) else {}
            previous_grant = prior_authority.get("grant_id") if isinstance(prior_authority, dict) else None
            if prior.get("status") == "revoked" and previous_grant == grant_id:
                raise SchedulerError("grant_revoked", "A revoked recurring grant cannot be re-enabled; a new grant ID is required.")
            if (
                previous_grant == grant_id
                and prior_binding != binding
                and not _same_host_reboot_rebind(prior_binding, binding)
            ):
                raise SchedulerError("grant_rebind_refused", "An existing recurring grant cannot be rebound to changed recipe, source, state, or authority.")
        if old_block is not None:
            if prior is None:
                raise SchedulerError("orphan_managed_entry", "A managed crontab entry has no private owner binding.")
            if old_digest != prior.get("binding_sha256"):
                raise SchedulerError("managed_entry_mismatch", "The managed crontab entry does not match its private owner binding.")
        unchanged_crontab = desired == previous.content and previous.present
        if not unchanged_crontab:
            validate = getattr(crontab, "validate_crontab", None)
            if callable(validate):
                validate(desired, directory)
        _ensure_bootstrap(directory, state, schedule_id, binding_sha256)
        if (
            unchanged_crontab
            and prior is not None
            and prior.get("status") == "enabled"
            and prior.get("binding_sha256") == binding_sha256
        ):
            return {
                "ok": True, "state": "enabled", "changed": False,
                "schedule_id": schedule_id, "binding_sha256": binding_sha256,
                "backup": None,
            }
        backup: Path | None = None
        if desired != previous.content or (desired == "" and previous.present):
            backup = _backup_crontab(directory, schedule_id, previous)
        installing = _state_record(
            schedule_id=schedule_id, status="installing", binding=binding,
            binding_sha256=binding_sha256, prior=prior,
        )
        _write_record(directory, installing)
        if unchanged_crontab:
            _write_record(directory, _state_record(
                schedule_id=schedule_id, status="enabled", binding=binding,
                binding_sha256=binding_sha256, prior=prior, last_backup=str(backup) if backup else None,
            ))
            return {
                "ok": True, "state": "enabled", "changed": prior is None or prior.get("status") != "enabled",
                "schedule_id": schedule_id, "binding_sha256": binding_sha256,
                "backup": str(backup) if backup else None,
            }
        outcome, _observed, detail = _write_and_reconcile(crontab, previous, desired)
        if outcome == "applied":
            _write_record(directory, _state_record(
                schedule_id=schedule_id, status="enabled", binding=binding,
                binding_sha256=binding_sha256, prior=prior,
                last_backup=str(backup) if backup else None,
            ))
            return {
                "ok": True, "state": "enabled", "changed": True,
                "schedule_id": schedule_id, "binding_sha256": binding_sha256,
                "backup": str(backup) if backup else None,
                "reconciled": True,
            }
        if outcome == "not_applied":
            if prior is None:
                _delete_record(directory, schedule_id)
            else:
                _write_record(directory, prior)
            return {
                "ok": False, "state": "not_installed", "changed": False,
                "schedule_id": schedule_id, "error": "crontab readback confirms the prior entry remains",
                "binding_sha256": binding_sha256,
                "backup": str(backup) if backup else None,
            }
        _write_record(directory, _state_record(
            schedule_id=schedule_id, status="install_uncertain", binding=binding,
            binding_sha256=binding_sha256, prior=prior, write_detail=detail,
        ))
        return {
            "ok": False, "state": "install_uncertain", "changed": False,
            "schedule_id": schedule_id, "error": "crontab readback did not confirm the requested entry; no retry was attempted",
            "binding_sha256": binding_sha256,
            "backup": str(backup) if backup else None,
        }


def _marker_block_status(content: str, schedule_id: str) -> tuple[str | None, str | None]:
    _remaining, block, binding = _remove_block(content, schedule_id)
    return block, binding


def status_schedule(
    schedule_id: str = SCHEDULE_ID,
    *,
    state_root: str | os.PathLike[str] | None = None,
    source_root: str | os.PathLike[str] | None = None,
    backend: Any | None = None,
) -> dict[str, Any]:
    """Read schedule state, marker, source fingerprint, and current crontab."""
    schedule_id = _safe_identifier(schedule_id)
    state = _state_root(state_root)
    directory = _schedule_state_dir(state, create=False)
    record = _read_record(directory, schedule_id)
    crontab = backend or SubprocessCrontabBackend()
    snapshot = _snapshot(crontab)
    block, marker_digest = _marker_block_status(snapshot.content, schedule_id)
    if record is None:
        state_name = "unbound_crontab_entry" if block is not None else "not_installed"
        return {"ok": True, "state": state_name, "enabled": False, "schedule_id": schedule_id, "managed_entry_present": block is not None}
    persisted = record.get("status")
    binding = record.get("binding")
    digest = record.get("binding_sha256")
    if not isinstance(binding, dict) or not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        raise SchedulerError("invalid_state", "The scheduler binding is incomplete or invalid.")
    if _binding_digest(binding) != digest:
        raise SchedulerError("binding_corrupt", "The private scheduler binding digest does not match its record.")
    source_entry = binding.get("source")
    if (
        type(binding.get("schema_version")) is not int
        or binding.get("schema_version") != 1
        or binding.get("schedule_id") != schedule_id
        or binding.get("recipe") != _recipe()
        or binding.get("state_root") != str(state)
        or not _validate_authority(binding.get("authority"))
        or not isinstance(source_entry, dict)
        or set(source_entry) != {"root", "sha256"}
        or not isinstance(source_entry.get("root"), str)
        or not isinstance(source_entry.get("sha256"), str)
        or not _HEX64.fullmatch(source_entry["sha256"])
    ):
        raise SchedulerError("binding_corrupt", "The private scheduler binding has invalid task, state, or authority fields.")
    source_matches = False
    runtime_matches = False
    try:
        bound_source = _source_root(source_entry["root"])
        current_digest = source_fingerprint(bound_source)
        source_matches = source_entry == {"root": str(bound_source), "sha256": current_digest}
        runtime_matches = binding.get("runtime") == _runtime_binding(bound_source)
        if source_root is not None:
            source_matches = source_matches and _source_root(source_root) == bound_source
    except SchedulerError:
        source_matches = False
        runtime_matches = False
    authority_state = _authority_runtime_state(binding["authority"])
    entry_matches = block is not None and marker_digest == digest
    if persisted == "revoked":
        state_name = "revoked_pending_removal" if block is not None else "disabled"
    elif persisted != "enabled":
        state_name = str(persisted)
    elif authority_state != "bound":
        state_name = authority_state
    elif not runtime_matches:
        state_name = "runtime_drift"
    elif not source_matches:
        state_name = "source_drift"
    elif not _bootstrap_matches(state, schedule_id, digest):
        state_name = "bootstrap_drift"
    elif not entry_matches:
        state_name = "entry_missing" if block is None else "binding_drift"
    elif block != _generated_block(
        Path(source_entry["root"]), Path(binding["state_root"]), schedule_id, digest,
        source_entry["sha256"],
    ):
        state_name = "entry_drift"
    else:
        state_name = "enabled"
    authority = binding.get("authority")
    return {
        "ok": True,
        "state": state_name,
        "enabled": state_name == "enabled",
        "schedule_id": schedule_id,
        "binding_sha256": digest,
        "managed_entry_present": block is not None,
        "source_matches": source_matches,
        "runtime_matches": runtime_matches,
        "enrollment_owner_liveness": _enrollment_liveness(binding["authority"]["owner"]),
        "grant_id": authority.get("grant_id") if isinstance(authority, dict) else None,
        "scope": authority.get("scope") if isinstance(authority, dict) else None,
        "last_run": record.get("last_run"),
        "last_result": record.get("last_result"),
    }


def disable_schedule(
    schedule_id: str = SCHEDULE_ID,
    *,
    state_root: str | os.PathLike[str] | None = None,
    backend: Any | None = None,
) -> dict[str, Any]:
    """Revoke this recurring grant before removing its exact crontab block."""
    schedule_id = _safe_identifier(schedule_id)
    state = _state_root(state_root)
    directory = _schedule_state_dir(state, create=True)
    assert directory is not None
    crontab = backend or SubprocessCrontabBackend()
    with _file_lock(directory, "crontab.lock", blocking=False):
        prior = _read_record(directory, schedule_id)
        previous = _snapshot(crontab)
        desired, block, marker_digest = _remove_block(previous.content, schedule_id)
        if prior is None and block is not None:
            raise SchedulerError("orphan_managed_entry", "A managed crontab entry has no private owner binding.")
        if prior is not None:
            prior_binding = prior.get("binding")
            prior_digest = prior.get("binding_sha256")
            if (
                not isinstance(prior_binding, dict)
                or not isinstance(prior_digest, str)
                or not _HEX64.fullmatch(prior_digest)
                or _binding_digest(prior_binding) != prior_digest
            ):
                raise SchedulerError("binding_corrupt", "The private schedule binding is invalid; the crontab was left unchanged.")
            if block is not None and marker_digest != prior_digest:
                raise SchedulerError("managed_entry_mismatch", "The managed crontab entry does not match its private owner binding.")
        revoked_record: dict[str, Any] | None = None
        if prior is not None:
            revoked_record = dict(prior)
            revoked_record.update(status="revoked", revoked_at=_utc_now(), updated_at=_utc_now())
            _write_record(directory, revoked_record)
        if desired == previous.content and (desired or not previous.present):
            return {
                "ok": True, "state": "disabled", "changed": prior is not None and prior.get("status") != "revoked",
                "schedule_id": schedule_id, "backup": None,
            }
        try:
            backup = _backup_crontab(directory, schedule_id, previous)
        except SchedulerError as exc:
            return {
                "ok": False, "state": "revoked_pending_removal", "changed": False,
                "schedule_id": schedule_id, "error": exc.message,
                "future_dispatch": "blocked by private revoked state",
            }
        outcome, _observed, _detail = _write_and_reconcile(crontab, previous, desired)
        if outcome == "applied":
            return {"ok": True, "state": "disabled", "changed": True, "schedule_id": schedule_id, "backup": str(backup)}
        if outcome == "not_applied":
            return {
                "ok": False, "state": "revoked_pending_removal", "changed": False,
                "schedule_id": schedule_id, "error": "crontab readback confirms the entry remains",
                "future_dispatch": "blocked by private revoked state", "backup": str(backup),
            }
        return {
            "ok": False, "state": "revoked_pending_removal", "changed": False,
            "schedule_id": schedule_id,
            "error": "crontab readback is uncertain; no retry was attempted",
            "future_dispatch": "blocked by private revoked state", "backup": str(backup),
        }


def _validate_record_binding(
    record: Mapping[str, Any], *, schedule_id: str, source_root: Path,
    state_root: Path, expected_digest: str,
) -> tuple[dict[str, Any], str | None]:
    binding = record.get("binding")
    digest = record.get("binding_sha256")
    if not isinstance(binding, dict) or not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        return {}, "scheduler binding is incomplete"
    if digest != expected_digest or _binding_digest(binding) != digest:
        return {}, "scheduler binding does not match the crontab marker"
    if (
        binding.get("schema_version") != 1
        or binding.get("schedule_id") != schedule_id
        or binding.get("recipe") != _recipe()
        or binding.get("state_root") != str(state_root)
        or binding.get("source") != {"root": str(source_root), "sha256": source_fingerprint(source_root)}
        or not _validate_authority(binding.get("authority"))
    ):
        return {}, "recipe, source, state, or authority binding changed"
    authority_state = _authority_runtime_state(binding["authority"])
    if authority_state != "bound":
        return {}, authority_state
    if binding.get("runtime") != _runtime_binding(source_root):
        return {}, "runtime_drift"
    return binding, None


def _gc_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ok": result.get("ok") is True,
        "state": str(result.get("state", "unknown"))[:64],
    }
    gc_result = result.get("gc")
    if isinstance(gc_result, Mapping):
        for key in ("removed", "pending", "preserved", "expired_logs"):
            values = gc_result.get(key)
            if isinstance(values, list):
                summary[key] = len(values)
    return summary


def run_bound_schedule(
    schedule_id: str,
    *,
    source_root: str | os.PathLike[str],
    state_root: str | os.PathLike[str] | None,
    binding_sha256: str,
    gc_callback: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run GC only while the installed private grant and source binding still match."""
    try:
        schedule_id = _safe_identifier(schedule_id)
        if not isinstance(binding_sha256, str) or not _HEX64.fullmatch(binding_sha256):
            raise SchedulerError("invalid_binding", "A valid installed binding digest is required.")
        source = _source_root(source_root)
        state = _state_root(state_root)
        _reject_overlapping_roots(source, state)
        directory = _schedule_state_dir(state, create=False)
        if directory is None:
            return {"ok": False, "state": "not_installed", "dispatched": False}
        record = _read_record(directory, schedule_id)
        if record is None:
            return {"ok": False, "state": "not_installed", "dispatched": False}
        if record.get("status") != "enabled":
            return {"ok": True, "state": str(record.get("status")), "dispatched": False}
        _binding_data, error = _validate_record_binding(
            record, schedule_id=schedule_id, source_root=source,
            state_root=state, expected_digest=binding_sha256,
        )
        if error:
            state_name = error if error in {"authority_machine_mismatch", "reboot_rebind_required", "runtime_drift"} else "binding_refused"
            return {"ok": False, "state": state_name, "dispatched": False, "error": error}
        with _file_lock(directory, "gc-overlap.lock", blocking=False):
            # This lock also guards install/revoke. The final admission check and
            # their state writes cannot interleave; GC itself holds only overlap.
            with _file_lock(directory, "crontab.lock", blocking=False):
                current = _read_record(directory, schedule_id)
                if current is None or current.get("status") != "enabled":
                    return {"ok": True, "state": "revoked", "dispatched": False}
                _binding_data, error = _validate_record_binding(
                    current, schedule_id=schedule_id, source_root=source,
                    state_root=state, expected_digest=binding_sha256,
                )
                if error:
                    state_name = error if error in {"authority_machine_mismatch", "reboot_rebind_required", "runtime_drift"} else "binding_refused"
                    return {"ok": False, "state": state_name, "dispatched": False, "error": error}
            try:
                if gc_callback is None:
                    from .command_service import ShellCommandService

                    result = ShellCommandService(state, source_root=source).gc()
                else:
                    result = gc_callback()
            except Exception as exc:
                result = {"ok": False, "state": "failed", "failure_type": type(exc).__name__}
            if not isinstance(result, Mapping):
                result = {"ok": False, "state": "invalid_gc_result"}
            summary = _gc_summary(result)
            try:
                with _file_lock(directory, "crontab.lock", blocking=False):
                    latest = _read_record(directory, schedule_id)
                    if latest is not None and latest.get("status") == "enabled" and latest.get("binding_sha256") == binding_sha256:
                        latest.update(last_run=_utc_now(), last_result=summary, updated_at=_utc_now())
                        _write_record(directory, latest)
            except SchedulerError:
                summary["tracking_error"] = True
            return {"ok": result.get("ok") is True, "state": "completed" if result.get("ok") is True else "failed", "dispatched": True, "result": summary}
    except SchedulerError as exc:
        if exc.code == "busy":
            return {"ok": True, "state": "overlap_skipped", "dispatched": False}
        return {"ok": False, "state": "refused", "dispatched": False, "error": exc.message, "code": exc.code}
    except (OSError, ShellCommandError) as exc:
        return {"ok": False, "state": "failed", "dispatched": False, "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m ls.core.terminal_ops.command_scheduler")
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_parser = subparsers.add_parser("run-bound", help="execute one installed source-bound GC schedule")
    run_parser.add_argument("--source-root", required=True)
    run_parser.add_argument("--state-root", required=True)
    run_parser.add_argument("--schedule-id", required=True)
    run_parser.add_argument("--binding-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        source = _source_root(args.source_root)
        module_root = Path(__file__).resolve().parents[3]
        if module_root != source:
            raise SchedulerError("module_source_mismatch", "The cron wrapper did not load the scheduler from its bound source root.")
        result = run_bound_schedule(
            args.schedule_id,
            source_root=source,
            state_root=args.state_root,
            binding_sha256=args.binding_sha256,
        )
    except SchedulerError as exc:
        result = {"ok": False, "state": "refused", "error": exc.message, "code": exc.code}
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("ok") is True else 2


__all__ = [
    "CRON_EXPRESSION", "RECIPE_ID", "SCHEDULE_ID", "CronWriteResult", "CrontabSnapshot",
    "SchedulerError", "SubprocessCrontabBackend", "disable_schedule", "install_schedule",
    "run_bound_schedule", "source_fingerprint", "status_schedule",
]


if __name__ == "__main__":  # pragma: no cover - exercised through the bound cron entry.
    raise SystemExit(main())
