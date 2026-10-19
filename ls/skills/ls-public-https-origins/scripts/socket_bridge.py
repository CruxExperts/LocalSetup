#!/usr/bin/env python3
"""Publish a service's local socket into the existing proxy data mount."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import time


def link_socket(source: Path, target: Path, source_uid: int) -> None:
    """Atomically refresh a protected hard link; never copy or proxy HTTP."""
    before = source.lstat()
    parent = target.parent.lstat()
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != source_uid:
        raise ValueError("source is not the expected owner's Unix socket")
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError("target directory must be root-owned with mode 0700")
    if before.st_dev != parent.st_dev:
        raise ValueError("socket sharing requires one filesystem")
    try:
        old = target.lstat()
    except FileNotFoundError:
        old = None
    if old is not None and (not stat.S_ISSOCK(old.st_mode) or old.st_uid != source_uid):
        raise ValueError("refusing to replace an unexpected destination")
    if before.st_gid != 0:
        os.chown(source, source_uid, 0, follow_symlinks=False)
    if stat.S_IMODE(before.st_mode) != 0o600:
        os.chmod(source, 0o600, follow_symlinks=False)
    if old is not None and (old.st_dev, old.st_ino) == (before.st_dev, before.st_ino):
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    try:
        os.link(source, temporary, follow_symlinks=False)
        linked = temporary.lstat()
        after = source.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or (
                linked.st_dev, linked.st_ino) != (before.st_dev, before.st_ino):
            raise FileNotFoundError("socket changed during publication")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    path = Path(args.config)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("configuration must be root-owned with mode 0600")
    config = json.loads(path.read_text())
    source, target = Path(config["source"]), Path(config["target"])
    if not source.is_absolute() or not target.is_absolute():
        raise ValueError("socket paths must be absolute")
    deadline = time.monotonic() + max(0, min(args.timeout, 120))
    while True:
        try:
            link_socket(source, target, int(config["source_uid"]))
            break
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Origin socket did not become available") from None
            time.sleep(0.2)
    print("Local origin socket published; no TCP origin configured")


if __name__ == "__main__":
    main()
