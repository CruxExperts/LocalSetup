"""Private local container state I/O using the shared anchored file checks.

Runtime leases serialize cooperating callers. Filesystem permissions and the
sandbox remain the boundary against other processes running as the same user.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat

from .envelope_io import AnchoredFile, _open_parent, _parent_chain_matches


def ensure_private_directory(path: Path, *, create: bool) -> None:
    """Create missing components only through verified, non-symlink parents."""
    if not path.is_absolute() or not path.name or ".." in path.parts:
        raise ValueError("invalid private directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for index, component in enumerate(path.parts[1:], 1):
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass  # A concurrent creator still has to pass validation.
                child = os.open(component, flags, dir_fd=descriptor)
            try:
                info = os.fstat(child)
                final = index == len(path.parts) - 1
                if (not stat.S_ISDIR(info.st_mode)
                        or info.st_uid not in {0, os.geteuid()}
                        or (info.st_mode & 0o022 and not (
                            info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
                        or (final and (info.st_uid != os.geteuid()
                                       or stat.S_IMODE(info.st_mode) != 0o700))):
                    raise ValueError("unsafe private directory")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        # Reopening through the existing file owner checks the complete named
        # chain, including replacement during creation of a component.
        current, _, selected, chain = _open_parent(path / ".probe", private=True)
        try:
            held, named = os.fstat(descriptor), os.fstat(current)
            if ((held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
                    or not _parent_chain_matches(selected, current, chain, private=True)):
                raise OSError("private directory changed")
        finally:
            os.close(current)
    finally:
        os.close(descriptor)


def read_private_file(path: Path, *, maximum: int, minimum: int = 1) -> bytes:
    """Read a bounded single-link file and recheck its complete named ancestry."""
    with AnchoredFile(path, private=True) as source:
        info = os.fstat(source.file)
        if info.st_nlink != 1 or not minimum <= source.size <= maximum:
            raise ValueError("invalid private file")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(source.file, min(64 * 1024, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != source.size or not source.unchanged():
            raise OSError("private file changed")
        return bytes(data)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def write_private_file(path: Path, data: bytes, *, replace: bool) -> None:
    """Publish fsynced bytes through the held parent without following links.

    A failed publication keeps any destination already created. Callers reconcile
    exact state after uncertain durability; they must not replay it automatically.
    Only an unchanged temporary file owned by this call is removed.
    """
    parent, name, selected, chain = _open_parent(path, private=True)
    temporary = f".state-{secrets.token_hex(16)}"
    descriptor = -1
    owned = None
    try:
        def check_parent() -> None:
            if not _parent_chain_matches(selected, parent, chain, private=True):
                raise OSError("private parent changed")

        def destination() -> tuple[int, ...] | None:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise OSError("unsafe private destination")
            return _identity(info)

        check_parent()
        previous = destination()
        if previous is not None and not replace:
            raise FileExistsError
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        info = os.fstat(descriptor)
        owned = (info.st_dev, info.st_ino)
        view = memoryview(data)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short private write")
            view = view[count:]
        os.fsync(descriptor)
        written = os.fstat(descriptor)
        if _identity(os.stat(temporary, dir_fd=parent, follow_symlinks=False)) != _identity(written):
            raise OSError("private stage changed")
        check_parent()
        if destination() != previous:
            raise OSError("private destination changed")
        if replace:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
        check_parent()
        if destination() != _identity(os.fstat(descriptor)):
            raise OSError("published private file changed")
    finally:
        try:
            if owned is not None:
                try:
                    info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == owned:
                        os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)
