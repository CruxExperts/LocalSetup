"""Private backup-file and isolated GnuPG-home filesystem operations."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

from .recovery_models import RecoveryError, RecoveryErrorCode, _MAX_BACKUP_BYTES

def _open_backup(value: str | os.PathLike[str]) -> BinaryIO:
    try:
        path = Path(value).absolute()
        if ".." in path.parts:
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        try:
            _require_private_ancestors(path)
        except RecoveryError:
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID) from None
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_BACKUP_BYTES
        ):
            os.close(descriptor)
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        return os.fdopen(descriptor, "rb", closefd=True)
    except RecoveryError:
        raise
    except (OSError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID) from None


def _create_backup_file(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).absolute()
        if ".." in path.parts:
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        _require_private_ancestors(path)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID)
        return path
    except RecoveryError:
        raise
    except (OSError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.BACKUP_INVALID) from None


def _existing_private_home(value: str | os.PathLike[str]) -> Path:
    try:
        home = Path(value).absolute()
        if ".." in home.parts:
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        _require_private_ancestors(home)
        metadata = home.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or _is_ambient_home(home)
        ):
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        _validate_private_tree(home)
        return home
    except RecoveryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME) from None


def _selected_home_path(value: str | os.PathLike[str]) -> Path:
    try:
        home = Path(value).absolute()
        if not home.name or ".." in home.parts:
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        return home
    except RecoveryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME) from None


def _new_empty_home(value: str | os.PathLike[str]) -> Path:
    try:
        home = Path(value).absolute()
        if ".." in home.parts:
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        _require_private_ancestors(home)
        if _is_ambient_home(home):
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        try:
            metadata = home.lstat()
        except FileNotFoundError:
            home.mkdir(mode=0o700)
            metadata = home.lstat()
        else:
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
            if next(home.iterdir(), None) is not None:
                raise RecoveryError(RecoveryErrorCode.KEYRING_NOT_EMPTY)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        os.chmod(home, 0o700)
        return home
    except RecoveryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME) from None


def _validate_private_home_contents(home: Path) -> None:
    metadata = home.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RecoveryError(RecoveryErrorCode.KEYRING_UNAVAILABLE)
    _validate_private_tree(home)


def _validate_private_tree(root: Path) -> None:
    with os.scandir(root) as entries:
        for entry in entries:
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
            if stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
                _validate_private_tree(Path(entry.path))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
            elif not stat.S_ISSOCK(metadata.st_mode):
                raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)


def _clear_restore_home(home: Path) -> None:
    """Roll back only files created in the verified-empty selected home."""
    try:
        _remove_contents(home)
        os.chmod(home, 0o700)
    except (OSError, RuntimeError):
        raise RecoveryError(RecoveryErrorCode.KEYRING_UNAVAILABLE) from None


def _remove_contents(root: Path) -> None:
    with os.scandir(root) as entries:
        for entry in entries:
            path = Path(entry.path)
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                _remove_contents(path)
                path.rmdir()
            else:
                path.unlink()


def _require_private_ancestors(selected: Path) -> None:
    for parent in selected.parents:
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in (0, os.geteuid()):
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)
        if stat.S_IMODE(metadata.st_mode) & 0o022 and not (
            metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        ):
            raise RecoveryError(RecoveryErrorCode.INVALID_KEYRING_HOME)


def _is_ambient_home(home: Path) -> bool:
    configured = os.environ.get("GNUPGHOME")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.append(Path.home() / ".gnupg")
    for candidate in candidates:
        try:
            ambient = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if _overlaps(home, ambient):
            return True
    return False


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)
