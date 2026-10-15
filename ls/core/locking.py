from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import time
from typing import Iterator


DEFAULT_PACKAGE_ROOT_LOCK_TIMEOUT_SECONDS = 30.0


class PackageRootLockTimeout(TimeoutError):
    """Raised when the global package-root advisory lock cannot be acquired."""

    status_code = "package_root_lock_timeout"


def package_root_lock_path(localsetup_home: Path) -> Path:
    return localsetup_home / "state" / "locks" / "package-root.lock"


def _timeout_seconds(timeout: float | None = None) -> float:
    if timeout is not None:
        return float(timeout)
    raw = os.environ.get("LOCALSETUP_PACKAGE_ROOT_LOCK_TIMEOUT")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return DEFAULT_PACKAGE_ROOT_LOCK_TIMEOUT_SECONDS
    return DEFAULT_PACKAGE_ROOT_LOCK_TIMEOUT_SECONDS


@contextmanager
def package_root_lock(localsetup_home: Path, *, timeout: float | None = None) -> Iterator[dict]:
    path = package_root_lock_path(localsetup_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _timeout_seconds(timeout)
    with path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise PackageRootLockTimeout(
                        f"package_root_lock_timeout: timed out waiting for package root lock: {path}"
                    ) from exc
                time.sleep(0.05)
                continue
            break

        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} acquired_at={int(time.time())}\n")
            handle.flush()
            yield {"path": str(path), "acquired": True}
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
