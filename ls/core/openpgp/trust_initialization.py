"""Descriptor-bound publication of a newly initialized private authority store."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .envelope import EnvelopeError
from .envelope_io import ExclusiveOutput


def _write_database(descriptor: int, serialized: bytes) -> None:
    """Handle short writes without reopening a destination or staging pathname."""
    view = memoryview(serialized)
    position = 0
    while position < len(view):
        written = os.write(descriptor, view[position:])
        if written <= 0:
            raise OSError("database write made no progress")
        position += written
    os.fsync(descriptor)


def publish_initial_database(path: Path, serialized: bytes) -> None:
    """Preserve portable create-once semantics and remove only our own inode.

    This route intentionally does not call the Linux-only staged publisher.
    SQLite never opens the selected pathname during initialization.
    """
    from .trust_state import TrustStateError

    try:
        with ExclusiveOutput(path) as output:
            if not output.unchanged():
                raise OSError("store selection changed")
            descriptor = os.open(
                output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=output.directory,
            )
            try:
                info = os.fstat(descriptor)
                output.published_identity = (info.st_dev, info.st_ino)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    raise OSError("unsafe store")
                if not output.unchanged():
                    raise OSError("store selection changed")
                _write_database(descriptor, serialized)
                os.fsync(output.directory)
                after = os.fstat(descriptor)
                if (after.st_uid != info.st_uid or after.st_mode != info.st_mode
                        or after.st_nlink != 1 or after.st_size != len(serialized)
                        or not output.unchanged()):
                    raise OSError("store selection changed")
            finally:
                os.close(descriptor)
            output.published_identity = None
    except (OSError, EnvelopeError) as exc:
        raise TrustStateError("store already exists or cannot be created") from exc
