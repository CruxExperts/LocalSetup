"""Bounded local source discovery; filenames and public hints confer no trust."""
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Callable

from .container_sources import LocalFileRangeSource, open_source_directory

_MAGIC = b"format: localsetup.secure-container\n"


class ScanLimitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalCandidate:
    source_id: str
    locator: str
    path: Path
    generation: str


@dataclass(frozen=True, slots=True)
class LocalInventory:
    candidates: tuple[LocalCandidate, ...]
    coverage: tuple[tuple[str, str], ...]


def scan_directories(sources: tuple[tuple[str, Path], ...], *, maximum: int,
                     check: Callable[[], None]) -> LocalInventory:
    """Visit bounded entries without following links; retain partial coverage.

    The caller supplies approved source identities and absolute directory roots.
    A candidate is only a carrier to authenticate, never accepted content.
    """
    if type(maximum) is not int or not 1 <= maximum <= 1_000_000:
        raise ValueError("Invalid source scan bound")
    count, candidates, coverage = 0, [], []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def walk(source_id, root, directory, relative, depth):
        nonlocal count
        check()
        if depth > 64:
            raise ScanLimitError("Source scan depth exceeded")
        before = os.fstat(directory)
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                check()
                count += 1
                if count > maximum:
                    raise ScanLimitError("Source scan entry limit exceeded")
                names.append(entry.name)
        for name in sorted(names):
            check()
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            parts = (*relative, name)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags, dir_fd=directory)
                try:
                    actual = os.fstat(child)
                    if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("Source directory changed")
                    walk(source_id, root, child, parts, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                path = root.joinpath(*parts)
                with LocalFileRangeSource(path, expected_stat=info) as source:
                    if source.size >= len(_MAGIC) and source.read_range(0, len(_MAGIC)) == _MAGIC:
                        candidates.append(LocalCandidate(source_id, "/".join(parts), path, source.generation))
        after = os.fstat(directory)
        if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
            raise OSError("Source directory changed")

    for source_id, root in sorted(sources):
        check()
        start = len(candidates)
        try:
            directory = open_source_directory(root)
            try:
                before = os.fstat(directory)
                walk(source_id, root, directory, (), 0)
                after = os.fstat(directory)
                named = open_source_directory(root)
                try:
                    current = os.fstat(named)
                    if ((before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)
                            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                        raise OSError("Source directory changed")
                finally:
                    os.close(named)
            finally:
                os.close(directory)
            coverage.append((source_id, "complete"))
        except PermissionError:
            raise
        except OSError:
            del candidates[start:]
            coverage.append((source_id, "unavailable"))
    check()
    return LocalInventory(tuple(candidates), tuple(coverage))
