"""Anchored bounded reads for explicitly selected private secret files."""

from __future__ import annotations

from enum import StrEnum
import os
from pathlib import Path
import re
import secrets as random_secrets
import stat
from typing import Final


_MAX_SECRET_BYTES: Final = 4096
_GENERATED_SECRET_BYTES: Final = 43
_SECRET_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_GENERATED_SECRET_PATTERN: Final = re.compile(rb"[A-Za-z0-9_-]{43}\Z")
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class _SecretFileErrorCode(StrEnum):
    """Redacted outcomes for a selected protected secret file."""

    INVALID_REQUEST = "INVALID_REQUEST"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    PARTIAL = "PARTIAL"


class _SecretFileError(RuntimeError):
    """A protected-file error that never includes paths or file contents."""

    def __init__(self, code: _SecretFileErrorCode) -> None:
        self.code = _SecretFileErrorCode(code)
        super().__init__(self.code.value)


def _read_protected_secret_file(root: Path, name: str) -> bytes:
    """Read one owner-private leaf under ``root`` without following links."""
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_NONBLOCK")
    ):
        raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE)
    if not isinstance(root, Path) or not isinstance(name, str):
        raise _SecretFileError(_SecretFileErrorCode.INVALID_REQUEST)
    selected = root
    if (
        not selected.is_absolute()
        or not selected.name
        or ".." in selected.parts
        or len(selected.parts) > 128
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise _SecretFileError(_SecretFileErrorCode.INVALID_REQUEST)

    directory = _open_private_root(selected)
    payload: bytearray | None = None
    try:
        _verify_private_root(selected, directory)
        payload = bytearray(_read_protected_secret_at(directory, name))
        _verify_private_root(selected, directory)
        return bytes(payload)
    finally:
        if payload is not None:
            payload[:] = b"\x00" * len(payload)
        _close_quietly(directory)


def _create_protected_secret_file(root: Path, name: str) -> bool:
    """Create one random passphrase, or validate and reuse its existing leaf.

    The return value reports only whether this call created the file. It never
    returns secret material. A partial or malformed existing leaf is retained
    and reported so a caller cannot silently replace a value that may have
    been used by an interrupted attempt.
    """
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_NONBLOCK")
    ):
        raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE)
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or not root.name
        or ".." in root.parts
        or len(root.parts) > 128
        or not isinstance(name, str)
        or _SECRET_NAME_PATTERN.fullmatch(name) is None
    ):
        raise _SecretFileError(_SecretFileErrorCode.INVALID_REQUEST)

    directory = _open_private_root(root)
    descriptor = -1
    payload: bytearray | None = None
    created = False
    try:
        _verify_private_root(root, directory)
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory,
            )
            created = True
        except FileExistsError:
            try:
                existing = bytearray(
                    _read_protected_secret_at(directory, name, sync=True)
                )
                _require_generated_secret(existing)
                os.fsync(directory)
                _verify_private_root(root, directory)
            except OSError:
                raise _SecretFileError(_SecretFileErrorCode.PARTIAL) from None
            except _SecretFileError as exc:
                if exc.code is _SecretFileErrorCode.INVALID_REQUEST:
                    raise
                raise _SecretFileError(_SecretFileErrorCode.PARTIAL) from None
            finally:
                if "existing" in locals():
                    existing[:] = b"\x00" * len(existing)
            return False
        except OSError:
            raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None

        before = os.fstat(descriptor)
        _require_new_private_file(before)
        payload = bytearray(random_secrets.token_urlsafe(32).encode("ascii"))
        _require_generated_secret(payload)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        try:
            named_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError:
            raise _SecretFileError(_SecretFileErrorCode.PARTIAL) from None
        if (
            _file_identity(after) != _file_identity(named_after)
            or _file_identity(after)[:2] != _file_identity(before)[:2]
        ):
            raise _SecretFileError(_SecretFileErrorCode.PARTIAL)
        _require_private_file(after)
        os.fsync(directory)
        _verify_private_root(root, directory)
        return created
    except _SecretFileError as exc:
        if created and exc.code is not _SecretFileErrorCode.PARTIAL:
            raise _SecretFileError(_SecretFileErrorCode.PARTIAL) from None
        raise
    except OSError:
        # Once O_EXCL succeeds, never remove or replace the leaf: GnuPG or a
        # later setup owner might already have used this exact passphrase.
        if created:
            raise _SecretFileError(_SecretFileErrorCode.PARTIAL) from None
        raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None
    finally:
        if payload is not None:
            payload[:] = b"\x00" * len(payload)
        if descriptor >= 0:
            _close_quietly(descriptor)
        _close_quietly(directory)


def _read_protected_secret_at(
    directory: int, name: str, *, sync: bool = False
) -> bytes:
    descriptor = -1
    try:
        try:
            named_before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            raise _SecretFileError(_SecretFileErrorCode.MISSING) from None
        except OSError:
            raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None
        _require_private_file(named_before)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory,
            )
        except FileNotFoundError:
            raise _SecretFileError(_SecretFileErrorCode.MISSING) from None
        except OSError:
            raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None
        before = os.fstat(descriptor)
        _require_private_file(before)
        if _file_identity(before) != _file_identity(named_before):
            raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE)
        if before.st_size > _MAX_SECRET_BYTES:
            raise _SecretFileError(_SecretFileErrorCode.OUTPUT_LIMIT)

        contents = bytearray()
        while len(contents) <= _MAX_SECRET_BYTES:
            try:
                chunk = os.read(
                    descriptor,
                    min(8192, _MAX_SECRET_BYTES + 1 - len(contents)),
                )
            except OSError:
                raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None
            if not chunk:
                break
            contents.extend(chunk)
        if len(contents) > _MAX_SECRET_BYTES:
            raise _SecretFileError(_SecretFileErrorCode.OUTPUT_LIMIT)

        after = os.fstat(descriptor)
        try:
            named_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError:
            raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None
        if (
            _file_identity(after) != _file_identity(before)
            or _file_identity(named_after) != _file_identity(before)
        ):
            raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE)
        if sync:
            try:
                os.fsync(descriptor)
            except OSError:
                raise _SecretFileError(_SecretFileErrorCode.PARTIAL) from None
        return bytes(contents)
    except _SecretFileError:
        raise
    except OSError:
        raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None
    finally:
        if "contents" in locals():
            contents[:] = b"\x00" * len(contents)
        if descriptor >= 0:
            _close_quietly(descriptor)


def _require_generated_secret(payload: bytes) -> None:
    if len(payload) != _GENERATED_SECRET_BYTES or _GENERATED_SECRET_PATTERN.fullmatch(payload) is None:
        raise _SecretFileError(_SecretFileErrorCode.PARTIAL)


def _require_new_private_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size != 0
    ):
        raise _SecretFileError(_SecretFileErrorCode.PARTIAL)


def _write_all(descriptor: int, payload: bytearray) -> None:
    view = memoryview(payload)
    written = 0
    try:
        while written < len(view):
            try:
                count = os.write(descriptor, view[written:])
            except OSError:
                raise _SecretFileError(_SecretFileErrorCode.PARTIAL) from None
            if count <= 0:
                raise _SecretFileError(_SecretFileErrorCode.PARTIAL)
            written += count
    finally:
        view.release()


def _open_private_root(selected: Path) -> int:
    descriptor = -1
    try:
        descriptor = os.open(selected.anchor, _DIRECTORY_FLAGS)
        for index, part in enumerate(selected.parts[1:]):
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            final = index == len(selected.parts[1:]) - 1
            _require_safe_directory(info, private=final)
        return descriptor
    except _SecretFileError:
        if descriptor >= 0:
            _close_quietly(descriptor)
        raise
    except (OSError, ValueError):
        if descriptor >= 0:
            _close_quietly(descriptor)
        raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None


def _verify_private_root(selected: Path, descriptor: int) -> None:
    """Requalify every ancestor and require the configured root's held inode."""
    current = _open_private_root(selected)
    try:
        held = os.fstat(descriptor)
        named = os.fstat(current)
        _require_safe_directory(held, private=True)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE)
    except OSError:
        raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE) from None
    finally:
        _close_quietly(current)


def _require_safe_directory(info: os.stat_result, *, private: bool) -> None:
    mode = info.st_mode
    sticky_root = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(mode)
        or info.st_uid not in {0, os.geteuid()}
        or (stat.S_IMODE(mode) & 0o022 and not sticky_root)
        or (
            private
            and (
                info.st_uid != os.geteuid()
                or stat.S_IMODE(mode) != 0o700
            )
        )
    ):
        raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE)


def _require_private_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size <= 0
    ):
        raise _SecretFileError(_SecretFileErrorCode.UNAVAILABLE)


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
    )


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass
