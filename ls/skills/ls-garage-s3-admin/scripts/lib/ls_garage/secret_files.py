"""Protected secret export and restore files; no secret crosses ordinary JSON."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from .reporting import ToolError


def _safe(path: Path) -> None:
    if not path.name or any(item.is_symlink() for item in (path, *path.parents)):
        raise ToolError("secret_file_unsafe", "secret output path must not contain symlinks", "policy")


def write(path_value: str, value: dict[str, Any]) -> None:
    reserved = reserve(path_value)
    try:
        deliver_reserved(reserved, value)
    finally:
        os.close(reserved[1])


def reserve(path_value: str) -> tuple[Path, int]:
    path = Path(path_value)
    _safe(path)
    fd = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return path, fd
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        if isinstance(exc, FileExistsError):
            raise ToolError("secret_file_exists", "secret output path already exists", "policy") from exc
        raise ToolError("secret_file_unavailable", "secret output cannot be reserved", "policy") from exc


def deliver_reserved(reserved: tuple[Path, int], value: dict[str, Any]) -> None:
    path, fd = reserved
    try:
        info, linked = os.fstat(fd), path.lstat()
        if (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino) or not stat.S_ISREG(linked.st_mode):
            raise OSError("reserved path changed")
        if stat.S_IMODE(linked.st_mode) != 0o600 or linked.st_uid != os.geteuid():
            raise OSError("reserved permissions changed")
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        offset = 0
        while offset < len(raw):
            count = os.write(fd, raw[offset:])
            if count <= 0:
                raise OSError("secret short write")
            offset += count
        os.fsync(fd)
        final = path.lstat()
        if (info.st_dev, info.st_ino) != (final.st_dev, final.st_ino):
            raise OSError("reserved path changed")
    except (OSError, ValueError) as exc:
        raise ToolError("secret_file_unavailable", "reserved secret output could not be durably delivered", "uncertain", False,
                        "preserve the output file; reconcile the remote identifier before any new creation") from exc


def read_restore(value: dict[str, Any]) -> dict[str, str]:
    path_value = value.get("file") if isinstance(value, dict) else None
    if not isinstance(path_value, str) or not path_value:
        raise ToolError("restore_file_invalid", "restore_file must select a protected JSON file")
    path = Path(path_value)
    _safe(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ToolError("restore_file_unsafe", "restore file must be an owned regular mode0600 file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if opened.st_ino != info.st_ino or opened.st_dev != info.st_dev:
                raise ToolError("restore_file_unsafe", "restore file changed while opening")
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ToolError("restore_file_unsafe", "restore file exceeds64KiB")
        data = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ToolError("restore_file_invalid", "protected restore file cannot be read") from exc
    fields = {"accessKeyId", "secretAccessKey", "name"}
    if not isinstance(data, dict) or set(data) != fields or any(not isinstance(data[key], str) or not data[key] for key in fields):
        raise ToolError("restore_file_invalid", "restore file must contain exactly accessKeyId, secretAccessKey, and name")
    return data
