"""Read-only byte-range sources for opaque secure-container data.

Locators identify where bytes were observed. They are not canonical object
keys, enrollment records, or proof of permissions or trust.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable


class ContainerSourceError(OSError):
    """A source could not safely provide the requested bytes."""


class SourceChangedError(ContainerSourceError):
    """The source identity or observed file state changed during access."""


@runtime_checkable
class RangeSource(Protocol):
    """A read-only, random-access source of an opaque byte sequence."""

    @property
    def size(self) -> int: ...

    def read_range(self, offset: int, length: int) -> bytes: ...


class BytesRangeSource:
    """Small in-memory source, useful for callers and deterministic tests."""

    def __init__(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def generation(self) -> str:
        """An observation token, never proof of content or authority."""
        return hashlib.sha256(self._data).hexdigest()

    def read_range(self, offset: int, length: int) -> bytes:
        _validate_range(offset, length)
        if offset + length > len(self._data):
            raise ContainerSourceError("requested range is outside the source")
        return self._data[offset : offset + length]


class LocalFileRangeSource:
    """Anchored read-only source for a regular local or mounted file.

    The descriptor is opened without following a final symlink. Reads use that
    descriptor, while each operation also checks that the original path still
    names the same unchanged regular file. A replacement, truncation, or normal
    in-place update fails closed.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        raw_path = Path(path)
        if not raw_path.is_absolute():
            raw_path = Path.cwd() / raw_path
        if ".." in raw_path.parts:
            raise ContainerSourceError("source path cannot contain parent traversal")
        self._components = tuple(part for part in raw_path.parts if part not in (raw_path.anchor, "."))
        if not self._components:
            raise ContainerSourceError("source path must name a regular file")
        self._path = os.path.join(os.path.sep, *self._components)
        self._leaf = self._components[-1]
        self._parent_components = self._components[:-1]
        required_flags = ("O_NOFOLLOW", "O_DIRECTORY")
        if os.name != "posix" or any(not hasattr(os, name) for name in required_flags):
            raise ContainerSourceError("platform lacks safe anchored nofollow file access")
        flags = (
            os.O_RDONLY
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
            | os.O_NOFOLLOW
        )
        try:
            self._fd = self._open_anchored(flags)
        except OSError as exc:
            raise ContainerSourceError("could not open source without following symlinks") from exc
        try:
            self._initial = self._signature(os.fstat(self._fd))
            if not stat.S_ISREG(self._initial[2]):
                raise ContainerSourceError("source must be a regular file")
            self._check_path_identity()
        except Exception:
            os.close(self._fd)
            self._fd = -1
            raise

    @staticmethod
    def _signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _check_open(self) -> None:
        if self._fd < 0:
            raise ContainerSourceError("source is closed")

    def _open_parent(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(os.path.sep, flags)
        except OSError as exc:
            raise SourceChangedError("filesystem root is unavailable") from exc
        try:
            for component in self._parent_components:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except OSError as exc:
            os.close(descriptor)
            raise SourceChangedError("source parent path changed or contains a symlink") from exc

    def _open_anchored(self, flags: int) -> int:
        parent = self._open_parent()
        try:
            return os.open(self._leaf, flags, dir_fd=parent)
        finally:
            os.close(parent)

    def _check_path_identity(self) -> None:
        parent = self._open_parent()
        try:
            path_signature = self._signature(
                os.stat(self._leaf, dir_fd=parent, follow_symlinks=False)
            )
        except OSError as exc:
            raise SourceChangedError("source path is no longer available") from exc
        finally:
            os.close(parent)
        if not stat.S_ISREG(path_signature[2]) or path_signature != self._initial:
            raise SourceChangedError("source path identity or state changed")

    def _check_unchanged(self) -> None:
        self._check_open()
        try:
            fd_signature = self._signature(os.fstat(self._fd))
        except OSError as exc:
            raise SourceChangedError("source descriptor is no longer available") from exc
        if fd_signature != self._initial:
            raise SourceChangedError("source file changed")
        self._check_path_identity()

    @property
    def size(self) -> int:
        self._check_unchanged()
        return self._initial[3]

    @property
    def generation(self) -> str:
        """Hash a stable opened-file observation without disclosing host paths."""
        self._check_unchanged()
        return hashlib.sha256(repr(self._initial).encode("ascii")).hexdigest()

    def read_range(self, offset: int, length: int) -> bytes:
        _validate_range(offset, length)
        self._check_unchanged()
        if offset + length > self._initial[3]:
            raise ContainerSourceError("requested range is outside the source")
        chunks: list[bytes] = []
        cursor = offset
        remaining = length
        while remaining:
            try:
                block = os.pread(self._fd, min(remaining, 1024 * 1024), cursor)
            except OSError as exc:
                raise ContainerSourceError("source range read failed") from exc
            if not block:
                raise SourceChangedError("source ended during a range read")
            chunks.append(block)
            cursor += len(block)
            remaining -= len(block)
        self._check_unchanged()
        return b"".join(chunks)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> LocalFileRangeSource:
        self._check_open()
        self._check_unchanged()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _validate_range(offset: int, length: int) -> None:
    if (
        isinstance(offset, bool)
        or isinstance(length, bool)
        or not isinstance(offset, int)
        or not isinstance(length, int)
        or offset < 0
        or length < 0
    ):
        raise ValueError("offset and length must be non-negative integers")
