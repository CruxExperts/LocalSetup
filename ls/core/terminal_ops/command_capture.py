"""Drain a tmux pane stream into bounded rolling operation logs."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import BinaryIO

from .command_state import ShellCommandError, atomic_json, log_lock, read_json


ROLL_TARGET_FRACTION = 2


def _private_log(path: Path) -> BinaryIO:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("unsafe operation log directory")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise OSError("unsafe operation log file")
    return os.fdopen(fd, "ab", buffering=0)


def _roll(path: Path, *, keep_bytes: int) -> int:
    """Drop the oldest excess bytes atomically and return the logical gap."""
    size = path.stat().st_size
    if size <= keep_bytes:
        return 0
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output, path.open("rb") as source:
            source.seek(size - keep_bytes)
            while block := source.read(64 * 1024):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return size - keep_bytes


def drain_stream(
    log_path: Path, cursor_path: Path, drained_path: Path, *, max_bytes: int,
    operation_path: Path | None = None,
) -> int:
    """Always drain stdin; disk failures disable logging but never backpressure tmux."""
    if type(max_bytes) is not int or max_bytes < 4096:
        max_bytes = 4096
    storage_ok = True
    writer: BinaryIO | None = None
    base_offset = 0
    try:
        writer = _private_log(log_path)
        if log_path.exists():
            old = {}
            if cursor_path.exists() and not cursor_path.is_symlink():
                import json
                try:
                    old = json.loads(cursor_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    old = {}
            base_offset = old.get("base_offset", 0) if isinstance(old, dict) else 0
            if type(base_offset) is not int or base_offset < 0:
                base_offset = 0
    except OSError:
        storage_ok = False
    byte_count = 0
    last_activity_monotonic: float | None = None
    handoff_started: float | None = None
    try:
        while True:
            # os.read returns currently available bytes for a live tmux pipe.
            # BufferedReader.read(N) may wait for N bytes or EOF, which would
            # back up quiet commands whose pane stays open for observation.
            block = os.read(sys.stdin.fileno(), 64 * 1024)
            if not block:
                break
            byte_count += len(block)
            if not storage_ok or writer is None:
                continue
            try:
                with log_lock(cursor_path):
                    current_size = log_path.stat().st_size
                    if current_size + len(block) > max_bytes:
                        # Keep about half the cap after each rollover, reducing the
                        # cost of sustained high-volume output while retaining exact
                        # logical cursor offsets.
                        if len(block) > max_bytes:
                            dropped_from_block = len(block) - max_bytes
                            block = block[-max_bytes:]
                            base_offset += current_size + dropped_from_block
                            writer.close()
                            writer = None
                            fd, name = tempfile.mkstemp(prefix=f".{log_path.name}.", dir=log_path.parent)
                            temporary = Path(name)
                            try:
                                os.fchmod(fd, 0o600)
                                with os.fdopen(fd, "wb") as reset:
                                    reset.write(block)
                                    reset.flush()
                                    os.fsync(reset.fileno())
                                os.replace(temporary, log_path)
                            finally:
                                temporary.unlink(missing_ok=True)
                            writer = _private_log(log_path)
                            block = b""
                        else:
                            keep = min(max_bytes - len(block), max_bytes // ROLL_TARGET_FRACTION)
                            writer.flush()
                            writer.close()
                            dropped = _roll(log_path, keep_bytes=keep)
                            base_offset += dropped
                            writer = _private_log(log_path)
                    if writer is not None:
                        writer.write(block)
                    last_activity_monotonic = time.monotonic()
                    atomic_json(cursor_path, {
                        "base_offset": base_offset,
                        "end_offset": base_offset + log_path.stat().st_size,
                        "available": True,
                        "updated_at": time.time(),
                        "activity_monotonic": last_activity_monotonic,
                    })
            except (OSError, ShellCommandError):
                storage_ok = False
                if writer is not None:
                    try:
                        writer.close()
                    except OSError:
                        pass
                    writer = None
    finally:
        if writer is not None:
            try:
                writer.flush()
                writer.close()
            except OSError:
                storage_ok = False
        try:
            with log_lock(cursor_path):
                atomic_json(cursor_path, {
                    "base_offset": base_offset,
                    "end_offset": base_offset + (log_path.stat().st_size if storage_ok else 0),
                    "available": storage_ok,
                    "updated_at": time.time(),
                    **({"activity_monotonic": last_activity_monotonic}
                       if last_activity_monotonic is not None else {}),
                })
        except (OSError, ShellCommandError):
            storage_ok = False
        try:
            atomic_json(drained_path, {
                "state": "drained",
                "storage": "available" if storage_ok else "unavailable",
                "bytes_seen": byte_count,
                "drained_at": time.time(),
            })
            handoff_started = time.monotonic()
        except (OSError, ShellCommandError):
            pass
    if operation_path is not None and handoff_started is not None:
        # The receipt is durable before any cleanup admission. The runner
        # waits only for this receipt and its finalize acknowledgement, never
        # for the collector process to exit.
        try:
            operation = read_json(operation_path, limit=2_000_000)
            if operation.get("layout_mode") == "family-v1":
                from .command_finalize import finalize_collected

                finalize_collected(operation_path.parent.parent / "session.json",
                                   str(operation["pane_id"]), {
                             "operation_path": operation_path,
                             "drained_path": drained_path,
                             "log_path": log_path,
                             "cursor_path": cursor_path,
                             "session_key": operation.get("session_key"),
                             "request_digest": operation.get("request_digest"),
                             "operation_id": operation.get("operation_id"),
                             "pane_id": operation.get("pane_id"),
                         }, started=handoff_started)
        except (ShellCommandError, OSError, KeyError, TypeError, ValueError):
            # The exact native hook or a later read may still reconcile a
            # pending operation. Capture's successful drain keeps return 0.
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--cursor", required=True)
    parser.add_argument("--drained", required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--operation")
    args = parser.parse_args(argv)
    return drain_stream(Path(args.log), Path(args.cursor), Path(args.drained),
                        max_bytes=args.max_bytes,
                        operation_path=Path(args.operation) if args.operation else None)


if __name__ == "__main__":
    raise SystemExit(main())
