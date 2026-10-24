"""Read-only selection and descriptor-bound preparation for key generation homes."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Callable

from .generation_models import KeyGenerationError, KeyGenerationErrorCode

__all__ = [
    "DirectoryIdentity",
    "GenerationHomeSelection",
    "PreparedGenerationHome",
    "inspect_generation_home",
    "check_generation_selection",
    "check_generation_parent",
    "normalize_generation_home_path",
    "require_private_ancestors",
    "is_ambient_home",
]


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    path: Path
    device: int
    inode: int
    uid: int
    mode: int


@dataclass(frozen=True, slots=True)
class GenerationHomeSelection:
    path: Path
    ancestors: tuple[DirectoryIdentity, ...]
    initial_leaf: DirectoryIdentity | None


def normalize_generation_home_path(value: str | os.PathLike[str]) -> Path:
    """Apply the shared path, ancestor, ambient-home, and symlink rules."""
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError
        initial = Path(raw).expanduser()
        if ".." in initial.parts:
            raise ValueError
        selected = initial.absolute()
        if ".." in selected.parts or not selected.name:
            raise ValueError
        require_private_ancestors(selected)
        if is_ambient_home(selected):
            raise ValueError
        return selected
    except KeyGenerationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise KeyGenerationError(
            KeyGenerationErrorCode.INVALID_KEYRING_HOME
        ) from None


def require_private_ancestors(selected: Path) -> None:
    """Reject ancestors another Unix identity can replace during generation."""
    try:
        for parent in selected.parents:
            metadata = parent.lstat()
            mode = metadata.st_mode
            if not stat.S_ISDIR(mode) or metadata.st_uid not in (0, os.geteuid()):
                raise ValueError
            if stat.S_IMODE(mode) & 0o022 and not (
                metadata.st_uid == 0 and mode & stat.S_ISVTX
            ):
                raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise KeyGenerationError(
            KeyGenerationErrorCode.INVALID_KEYRING_HOME
        ) from None


def is_ambient_home(home: Path) -> bool:
    configured = os.environ.get("GNUPGHOME")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.append(Path.home() / ".gnupg")
    for candidate in candidates:
        try:
            ambient = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if home == ambient or home.is_relative_to(ambient):
            return True
    return False


def inspect_generation_home(value: str | os.PathLike[str]) -> GenerationHomeSelection:
    """Capture a missing or empty caller-owned home without changing it."""
    path = normalize_generation_home_path(value)
    try:
        ancestors = tuple(_directory_identity(parent) for parent in reversed(path.parents))
        leaf: DirectoryIdentity | None
        try:
            info = path.lstat()
        except FileNotFoundError:
            leaf = None
        else:
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError
            descriptor = _open_directory(path)
            try:
                opened = os.fstat(descriptor)
                if not _same_object(info, opened):
                    raise ValueError
                if os.listdir(descriptor):
                    raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_NOT_EMPTY)
                leaf = _identity(path, opened)
            finally:
                os.close(descriptor)
        check_generation_parent(GenerationHomeSelection(path, ancestors, leaf))
        return GenerationHomeSelection(path, ancestors, leaf)
    except KeyGenerationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise KeyGenerationError(
            KeyGenerationErrorCode.INVALID_KEYRING_HOME
        ) from None


def check_generation_selection(selection: GenerationHomeSelection) -> None:
    """Require the originally selected missing/empty leaf and parents unchanged."""
    try:
        _check_ancestors(selection)
        if selection.initial_leaf is None:
            try:
                selection.path.lstat()
            except FileNotFoundError:
                return
            raise ValueError
        current = _directory_identity(selection.path)
        if current != selection.initial_leaf:
            raise ValueError
        descriptor = _open_directory(selection.path)
        try:
            if os.listdir(descriptor):
                raise ValueError
        finally:
            os.close(descriptor)
    except KeyGenerationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        raise KeyGenerationError(
            KeyGenerationErrorCode.KEYRING_UNAVAILABLE
        ) from None


def check_generation_parent(selection: GenerationHomeSelection) -> None:
    """Recheck only the qualified parent chain after a consumer prepares leaf."""
    try:
        _check_ancestors(selection)
    except KeyGenerationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        raise KeyGenerationError(
            KeyGenerationErrorCode.KEYRING_UNAVAILABLE
        ) from None


def _check_ancestors(selection: GenerationHomeSelection) -> None:
    if not isinstance(selection, GenerationHomeSelection):
        raise ValueError
    if (
        not isinstance(selection.path, Path)
        or not selection.path.is_absolute()
        or tuple(item.path for item in selection.ancestors)
        != tuple(reversed(selection.path.parents))
        or (
            selection.initial_leaf is not None
            and selection.initial_leaf.path != selection.path
        )
    ):
        raise ValueError
    if normalize_generation_home_path(selection.path) != selection.path:
        raise ValueError
    require_private_ancestors(selection.path)
    current = tuple(
        _directory_identity(item.path) for item in selection.ancestors
    )
    if current != selection.ancestors:
        raise ValueError


class PreparedGenerationHome:
    """Own an anchored parent and exact prepared leaf for one generation call."""

    def __init__(
        self,
        selection: GenerationHomeSelection,
        parent_fd: int,
        home_fd: int,
        identity: DirectoryIdentity,
        *,
        created: bool,
    ) -> None:
        self.selection = selection
        self.parent_fd = parent_fd
        self.home_fd = home_fd
        self.path = selection.path
        self.identity = identity
        self.created = created

    @classmethod
    def prepare(
        cls,
        selection: GenerationHomeSelection,
        *,
        before_mutation: Callable[[], None] | None = None,
    ) -> PreparedGenerationHome:
        check_generation_selection(selection)
        parent_fd = -1
        home_fd = -1
        created = False
        leaf_name = selection.path.name
        try:
            parent_fd = _open_directory(selection.path.parent)
            expected_parent = next(
                (item for item in selection.ancestors if item.path == selection.path.parent),
                None,
            )
            if (
                expected_parent is None
                or _identity(selection.path.parent, os.fstat(parent_fd)) != expected_parent
            ):
                raise ValueError
            check_generation_selection(selection)

            if selection.initial_leaf is None:
                _before_mutation(before_mutation)
                try:
                    os.mkdir(leaf_name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    raise ValueError from None
                created = True
                check_generation_parent(selection)
                home_fd = _open_directory_at(parent_fd, leaf_name)
                opened = os.fstat(home_fd)
                if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
                    raise ValueError
                if stat.S_IMODE(opened.st_mode) != 0o700:
                    _before_mutation(before_mutation)
                    os.fchmod(home_fd, 0o700)
            else:
                home_fd = _open_directory_at(parent_fd, leaf_name)
                opened = os.fstat(home_fd)
                if _identity(selection.path, opened) != selection.initial_leaf:
                    raise ValueError
                if os.listdir(home_fd):
                    raise ValueError
                _before_mutation(before_mutation)
                os.fchmod(home_fd, 0o700)

            prepared_identity = _identity(selection.path, os.fstat(home_fd))
            if prepared_identity.mode != 0o700:
                raise ValueError
            prepared = cls(
                selection, parent_fd, home_fd, prepared_identity, created=created,
            )
            prepared.check_current()
            parent_fd = home_fd = -1
            return prepared
        except KeyGenerationError:
            if created and parent_fd >= 0:
                _remove_empty_created_leaf(parent_fd, leaf_name, home_fd)
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            if created and parent_fd >= 0:
                _remove_empty_created_leaf(parent_fd, leaf_name, home_fd)
            raise KeyGenerationError(
                KeyGenerationErrorCode.KEYRING_UNAVAILABLE
            ) from None
        finally:
            if home_fd >= 0:
                os.close(home_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    def check_current(self) -> None:
        check_generation_parent(self.selection)
        try:
            descriptor_info = os.fstat(self.home_fd)
            parent_info = os.fstat(self.parent_fd)
            named_info = os.stat(
                self.path.name, dir_fd=self.parent_fd, follow_symlinks=False,
            )
        except OSError:
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
        if (
            _identity(self.path.parent, parent_info)
            != next(item for item in self.selection.ancestors if item.path == self.path.parent)
            or _identity(self.path, descriptor_info) != self.identity
            or _identity(self.path, named_info) != self.identity
            or not stat.S_ISDIR(descriptor_info.st_mode)
        ):
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)

    def check_owned_directory(self) -> None:
        """Prove the open descriptor still names this prepared inode, regardless of path."""
        try:
            info = os.fstat(self.home_fd)
        except OSError:
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino, info.st_uid)
            != (self.identity.device, self.identity.inode, self.identity.uid)
        ):
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)

    def child_identity(self, name: str) -> tuple[int, int, int, int, int] | None:
        try:
            info = os.stat(name, dir_fd=self.home_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None
        return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink)

    def unlink_child_if_same(
        self, name: str, expected: tuple[int, int, int, int, int] | None,
    ) -> None:
        if expected is None:
            return
        try:
            current = self.child_identity(name)
            if current == expected:
                os.unlink(name, dir_fd=self.home_fd)
        except FileNotFoundError:
            pass
        except OSError:
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None

    def harden(self) -> None:
        """Tighten only the directory inode opened by this prepared operation."""
        self.check_owned_directory()
        try:
            os.fchmod(self.home_fd, 0o700)
            _harden_directory_fd(self.home_fd, depth=0, count=[0])
        except KeyGenerationError:
            raise
        except OSError:
            raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE) from None

    def close(self) -> None:
        for attribute in ("home_fd", "parent_fd"):
            descriptor = getattr(self, attribute)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, attribute, -1)

    def __enter__(self) -> PreparedGenerationHome:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _before_mutation(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        callback()


def _directory_identity(path: Path) -> DirectoryIdentity:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError
    return _identity(path, info)


def _identity(path: Path, info: os.stat_result) -> DirectoryIdentity:
    return DirectoryIdentity(
        path=path,
        device=info.st_dev,
        inode=info.st_ino,
        uid=info.st_uid,
        mode=stat.S_IMODE(info.st_mode),
    )


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_uid, left.st_mode) == (
        right.st_dev, right.st_ino, right.st_uid, right.st_mode,
    )


def _open_directory(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )


def _open_directory_at(parent_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )


def _remove_empty_created_leaf(parent_fd: int, name: str, home_fd: int) -> None:
    # Without an open descriptor there is no captured identity for the created
    # leaf. Its name may now refer to an unrelated replacement, so preserve it.
    if home_fd < 0:
        return
    try:
        if os.listdir(home_fd):
            return
        descriptor_info = os.fstat(home_fd)
        named_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (descriptor_info.st_dev, descriptor_info.st_ino) != (
            named_info.st_dev, named_info.st_ino,
        ):
            return
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        pass


def _harden_directory_fd(descriptor: int, *, depth: int, count: list[int]) -> None:
    if depth > 8:
        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
    with os.scandir(descriptor) as entries:
        for entry in entries:
            count[0] += 1
            if count[0] > 4096:
                raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
            info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _open_directory_at(descriptor, entry.name)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
                    os.fchmod(child, 0o700)
                    _harden_directory_fd(child, depth=depth + 1, count=count)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
                child = os.open(
                    entry.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
                        info.st_dev, info.st_ino, info.st_nlink,
                    ):
                        raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
                    os.fchmod(child, 0o600)
                finally:
                    os.close(child)
            elif stat.S_ISSOCK(info.st_mode):
                continue
            else:
                raise KeyGenerationError(KeyGenerationErrorCode.KEYRING_UNAVAILABLE)
