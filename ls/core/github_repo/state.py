from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator


class TargetOperationBusy(TimeoutError):
    """Another checkout is already operating on this host/repository pair."""


class JournalError(RuntimeError):
    pass


_OPERATION_ID = re.compile(r"^[0-9a-f]{24}$")
_PLAN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_MAX_JOURNAL_ROWS = 20_000
_MAX_JOURNAL_LINE_BYTES = 512 * 1024
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def operation_state_directory(state_root: Path, hostname: str, repository_id: int) -> Path:
    """Return the one cross-checkout target directory for a host and immutable ID."""
    if not hostname or len(hostname) > 253 or hostname != hostname.lower() or "/" in hostname or "\\" in hostname or any(
        not label or len(label) > 63 or label[0] == "-" or label[-1] == "-" or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in label)
        for label in hostname.split(".")
    ):
        raise JournalError("repository state requires a normalized bare hostname")
    if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0:
        raise JournalError("repository state requires a positive immutable repository ID")
    try:
        state_fd = _open_directory(state_root, create=True, private_tail=1)
    except OSError as exc:
        raise JournalError("LocalSetup state root could not be secured; verify that it is user-owned and has private parents") from exc
    os.close(state_fd)
    key = hashlib.sha256(f"{hostname}\0{repository_id}".encode("utf-8")).hexdigest()
    return state_root / "github-repository-operations" / key


def _open_directory(path: Path, *, create: bool, private_tail: int) -> int:
    """Open a directory path without following symlinks and validate every parent."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute():
        raise JournalError("repository state path must be absolute")
    parts = absolute.parts[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise JournalError("repository state path contains an unsafe component")
    uid = os.geteuid()
    current = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current,
                )
            except FileNotFoundError:
                if not create:
                    raise JournalError("repository state directory does not exist")
                created = False
                try:
                    os.mkdir(part, _PRIVATE_DIR_MODE, dir_fd=current)
                    created = True
                    os.fsync(current)
                except FileExistsError:
                    # A concurrent creator is acceptable only if the subsequent no-follow open validates it.
                    pass
                if created:
                    os.chmod(part, _PRIVATE_DIR_MODE, dir_fd=current, follow_symlinks=False)
                child = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current,
                )
            except OSError as exc:
                raise JournalError("repository state path contains a symlink or non-directory component") from exc
            info = os.fstat(child)
            mode = stat.S_IMODE(info.st_mode)
            writable_parent = bool(mode & 0o022)
            trusted_root_sticky_parent = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
            if info.st_uid not in {0, uid} or (writable_parent and not trusted_root_sticky_parent):
                os.close(child)
                raise JournalError("LocalSetup state path has an untrusted owner or group/world-writable parent; secure the state root as a user-owned private directory before retrying")
            if len(parts) - index <= private_tail:
                if info.st_uid != uid or mode != _PRIVATE_DIR_MODE:
                    os.close(child)
                    raise JournalError("repository state directory must be user-owned and private")
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def open_private_directory(path: Path, *, create: bool = True) -> int:
    """Open a user-owned private output directory without following path symlinks."""
    try:
        return _open_directory(path, create=create, private_tail=1)
    except OSError as exc:
        raise JournalError("private LocalSetup output directory could not be opened safely") from exc


@dataclass
class _LockGuard:
    directory: Path
    directory_fd: int
    lock_fd: int
    lock_identity: tuple[int, int]
    active: bool = True

    def validate(self, directory: Path) -> None:
        expected = Path(os.path.abspath(os.fspath(directory)))
        if not self.active or expected != self.directory:
            raise JournalError("operation journal access requires its active target lock")
        lock_info = os.fstat(self.lock_fd)
        if (lock_info.st_dev, lock_info.st_ino) != self.lock_identity:
            raise JournalError("target lock identity changed")
        try:
            named = os.stat("operation.lock", dir_fd=self.directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise JournalError("target lock path changed while locked") from exc
        if (named.st_dev, named.st_ino) != self.lock_identity or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1:
            raise JournalError("target lock path no longer names the held private lock")


def _validate_lock_file(descriptor: int) -> tuple[int, int]:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise JournalError("operation lock must be a user-owned, single-link regular file")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE:
        raise JournalError("operation lock file must be private")
    return info.st_dev, info.st_ino


@contextmanager
def target_operation_lock(directory: Path) -> Iterator[_LockGuard]:
    directory = Path(os.path.abspath(os.fspath(directory)))
    try:
        directory_fd = _open_directory(directory, create=True, private_tail=3)
    except OSError as exc:
        raise JournalError("repository operation state path could not be opened safely") from exc
    lock_fd = -1
    try:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open("operation.lock", flags | os.O_CREAT | os.O_EXCL, _PRIVATE_FILE_MODE, dir_fd=directory_fd)
            os.fchmod(lock_fd, _PRIVATE_FILE_MODE)
            os.fsync(directory_fd)
        except FileExistsError:
            lock_fd = os.open("operation.lock", flags, dir_fd=directory_fd)
        identity = _validate_lock_file(lock_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise TargetOperationBusy("another LocalSetup GitHub repository operation holds the target lock") from exc
            raise
        guard = _LockGuard(directory, directory_fd, lock_fd, identity)
        try:
            yield guard
        finally:
            guard.active = False
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except TargetOperationBusy:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise JournalError("operation lock path is a symlink or non-regular entry") from exc
        raise JournalError("operation lock could not be opened safely") from exc
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def _journal_descriptor(guard: _LockGuard, *, writing: bool) -> tuple[int, bool]:
    flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if writing:
        flags |= os.O_WRONLY | os.O_APPEND
        try:
            descriptor = os.open("operations.jsonl", flags | os.O_CREAT | os.O_EXCL, _PRIVATE_FILE_MODE, dir_fd=guard.directory_fd)
            created = True
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        except FileExistsError:
            descriptor = os.open("operations.jsonl", flags, dir_fd=guard.directory_fd)
            created = False
    else:
        descriptor = os.open("operations.jsonl", os.O_RDONLY | flags, dir_fd=guard.directory_fd)
        created = False
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        os.close(descriptor)
        raise JournalError("operation journal must be a user-owned, single-link regular file")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE:
        os.close(descriptor)
        raise JournalError("operation journal file must be private")
    return descriptor, created


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("journal row is not an object")
    allowed = {"operation_id", "plan_digest", "state", "operation", "reconciliation"}
    if set(row) - allowed:
        raise ValueError("journal row has unsupported fields")
    operation_id = row.get("operation_id")
    state = row.get("state")
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError("journal operation ID is malformed")
    if state not in {"pending", "applied", "verification_mismatch", "reconciled_applied", "reconciled_not_applied", "reconciliation_conflict"}:
        raise ValueError("journal state is unsupported")
    plan_digest = row.get("plan_digest")
    if not isinstance(plan_digest, str) or not _PLAN_DIGEST.fullmatch(plan_digest):
        raise ValueError("journal plan digest is malformed")
    if state == "pending":
        operation = row.get("operation")
        if not isinstance(operation, dict) or operation.get("id") != operation_id:
            raise ValueError("pending journal row lacks its matching typed operation")
    elif "operation" in row:
        raise ValueError("completed journal rows cannot retain operation payloads")
    if "reconciliation" in row and row["reconciliation"] != "read_only_current_state":
        raise ValueError("journal reconciliation marker is unsupported")
    return row


def read_journal(directory: Path, guard: _LockGuard) -> list[dict[str, Any]]:
    guard.validate(directory)
    try:
        descriptor, _ = _journal_descriptor(guard, writing=False)
    except FileNotFoundError:
        return []
    try:
        info = os.fstat(descriptor)
        if info.st_size > _MAX_JOURNAL_BYTES:
            raise JournalError("operation journal exceeds the supported size limit")
        chunks = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_JOURNAL_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > _MAX_JOURNAL_BYTES:
                raise JournalError("operation journal exceeds the supported size limit")
        text = bytes(chunks).decode("utf-8")
        lines = text.splitlines()
        if len(lines) > _MAX_JOURNAL_ROWS:
            raise JournalError("operation journal exceeds the supported row limit")
        rows: list[dict[str, Any]] = []
        for line in lines:
            encoded = line.encode("utf-8")
            if not line or len(encoded) > _MAX_JOURNAL_LINE_BYTES:
                raise JournalError("operation journal contains an empty or oversized row")
            value = json.loads(line, object_pairs_hook=_object_without_duplicate_keys)
            rows.append(_validate_row(value))
        return rows
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, JournalError):
            raise
        raise JournalError("GitHub operation journal is unreadable or malformed") from exc
    finally:
        os.close(descriptor)


def latest_operation_records(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest[row["operation_id"]] = row
    return latest


def append_journal(directory: Path, row: dict[str, Any], guard: _LockGuard) -> None:
    guard.validate(directory)
    try:
        normalized = _validate_row(row)
        encoded = (json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JournalError("refusing to append an invalid operation journal row") from exc
    if len(encoded) > _MAX_JOURNAL_LINE_BYTES:
        raise JournalError("operation journal row exceeds the supported size limit")
    try:
        descriptor, created = _journal_descriptor(guard, writing=True)
    except OSError as exc:
        raise JournalError("operation journal path could not be opened safely") from exc
    try:
        info = os.fstat(descriptor)
        if info.st_size + len(encoded) > _MAX_JOURNAL_BYTES:
            raise JournalError("operation journal exceeds the supported size limit")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise JournalError("operation journal append was incomplete")
            view = view[written:]
        os.fsync(descriptor)
        if created:
            os.fsync(guard.directory_fd)
    finally:
        os.close(descriptor)
