#!/usr/bin/env python3
# Purpose: Scan opaque file drops, claim ready pairs, and move processed objects.
# Created: 2026-03-09
# Last updated: 2026-03-09

from __future__ import annotations

import fnmatch
import hashlib
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterator


def default_ignore_globs() -> list[str]:
    return ["*conflicted copy*", "*.tmp", "~*", "*.part"]


def ignored_path(path: Path, globs: list[str]) -> bool:
    name = path.name
    for g in globs:
        if fnmatch.fnmatch(name, g):
            return True
    return False


_READY_SHA256 = re.compile(r"^\s*sha256\s+([a-fA-F0-9]{64})\s*$")


def read_ready_sha256(ready_path: Path) -> str | None:
    """
    If ready file first non-empty line matches 'sha256 <64hex>', return lowercase hex.
    Otherwise None (ready file may be empty, which is valid).
    """
    try:
        text = ready_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _READY_SHA256.match(line)
        if m:
            return m.group(1).lower()
        break
    return None


def sealed_file_sha256(sealed_path: Path) -> str:
    """SHA256 of file bytes (for ready marker compare)."""
    h = hashlib.sha256()
    with open(sealed_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_ready_marker_sha256(sealed_path: Path, ready_path: Path) -> bool:
    """
    If ready contains sha256 line, it must match sealed file. Returns True if no line or match.
    Raises ValueError with message if mismatch (caller should quarantine / ingest_verify_fail).
    """
    expected = read_ready_sha256(ready_path)
    if not expected:
        return True
    actual = sealed_file_sha256(sealed_path)
    if actual != expected:
        raise ValueError(
            f"ready marker sha256 mismatch: sealed={actual[:16]}... expected={expected[:16]}..."
        )
    return True


def ready_marker_path(sealed_path: Path, sealed_extension: str) -> Path:
    # Opaque x.agentq.lspgp -> x.ready.
    if sealed_path.name.endswith(sealed_extension):
        return sealed_path.with_name(
            sealed_path.name[: -len(sealed_extension)] + ".ready"
        )
    return sealed_path.with_suffix(".ready")


def iter_candidates(
    roots: list[Path],
    sealed_extension: str,
    ignore_globs: list[str] | None = None,
) -> Iterator[tuple[Path, Path]]:
    """Yield (sealed_path, ready_path) for each complete pair."""
    globs = ignore_globs or default_ignore_globs()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for sealed in sorted(root.glob(f"*{sealed_extension}")):
            if sealed.is_symlink() or not sealed.is_file():
                continue
            if ignored_path(sealed, globs):
                continue
            ready = ready_marker_path(sealed, sealed_extension)
            if ready.is_file() and not ready.is_symlink():
                yield sealed, ready


def claim_with_lockfile(sealed: Path) -> Any:
    """
    Optional exclusive lock next to sealed file (Part 6 lock before verify).
    Returns context manager or None if fcntl unavailable. Caller must release.
    """
    try:
        import fcntl

        lock_path = sealed.parent / (sealed.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (OSError, BlockingIOError, ImportError):
        return None


def claim_to_processing(
    sealed: Path,
    processing_dir: Path,
    sealed_extension: str,
    use_lockfile: bool = False,
) -> Path | None:
    """Move sealed+ready into processing/<uuid>/; return dir or None if lost race."""
    lock_fh = None
    if use_lockfile:
        lock_fh = claim_with_lockfile(sealed)
        if lock_fh is None and use_lockfile:
            # Could not lock; another worker may be ingesting
            return None
    processing_dir.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex[:12]
    dest = processing_dir / uid
    try:
        dest.mkdir(exist=False)
    except FileExistsError:
        return None
    ready = ready_marker_path(sealed, sealed_extension)
    if not ready.is_file():
        shutil.rmtree(dest, ignore_errors=True)
        return None
    try:
        shutil.move(str(sealed), str(dest / sealed.name))
        shutil.move(str(ready), str(dest / ready.name))
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)
        if lock_fh:
            try:
                lock_fh.close()
            except OSError:
                pass
        return None
    if lock_fh:
        try:
            lock_fh.close()
        except OSError:
            pass
    return dest


def processed_subdir_name(short_id: str) -> str:
    """processed/<iso8601_utc>_<shortid>/ per spec Part 6."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{short_id}"


def move_to_processed(claimed_dir: Path, processed_root: Path) -> Path:
    """Move claimed_dir under processed/<iso>_<shortid>/."""
    from datetime import datetime, timezone

    short = claimed_dir.name
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_parent = processed_root / f"{stamp}_{short}"
    dest_parent.mkdir(parents=True, exist_ok=True)
    for child in claimed_dir.iterdir():
        shutil.move(str(child), str(dest_parent / child.name))
    shutil.rmtree(claimed_dir, ignore_errors=True)
    return dest_parent
