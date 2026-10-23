"""Reconcile one exact completed family pane from tmux's pane-died hook."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import time

from .command_service import ShellCommandService
from .command_state import ShellCommandError, read_json, session_lock


def finalize(session_record: Path, pane_id: str) -> int:
    if not session_record.is_absolute() or session_record.name != "session.json":
        return 2
    if not re.fullmatch(r"%[0-9]+", pane_id):
        return 2
    if session_record.is_symlink():
        return 2
    try:
        record = read_json(session_record, limit=2_000_000)
        service = ShellCommandService(session_record.parents[4])
        directory, session, op_id = service._load(str(record["handle"]))
        if op_id is not None or directory / "session.json" != session_record:
            return 2
        deadline = time.monotonic() + 2
        while True:
            with session_lock(directory, blocking=True):
                session = read_json(session_record, limit=2_000_000)
                if (session.get("layout_mode") != "family-v1"
                        or session.get("session_record_path") != str(session_record)
                        or session.get("session_key") != record.get("session_key")):
                    return 2
                matches = [item for item in service._strict_ops(directory, session)
                           if item.get("pane_id") == pane_id]
                if len(matches) != 1:
                    return 2
                operation = matches[0]
                if (operation.get("state") != "completed"
                        or operation.get("process_scope") != "collected"
                        or operation.get("session_key") != session.get("session_key")):
                    return 2
                updated = service._reconcile(directory, session, operation)
                if updated.get("cleanup_state") == "removed":
                    return 0
                if updated.get("cleanup_state") != "pending" or time.monotonic() >= deadline:
                    return 1
            time.sleep(0.05)
    except (ShellCommandError, OSError, KeyError, TypeError):
        return 2


def finalize_detached(session_record: Path) -> int:
    if not session_record.is_absolute() or session_record.name != "session.json" or session_record.is_symlink():
        return 2
    try:
        record = read_json(session_record, limit=2_000_000)
        service = ShellCommandService(session_record.parents[4])
        directory, session, op_id = service._load(str(record["handle"]))
        if op_id is not None or directory / "session.json" != session_record:
            return 2
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with session_lock(directory, blocking=True):
                session = read_json(session_record, limit=2_000_000)
                if (session.get("session_key") != record.get("session_key")
                        or session.get("session_record_path") != str(session_record)):
                    return 2
                if session.get("state") == "removed":
                    return 0
                if session.get("state") != "retiring":
                    return 0
                if service._finish_retirement_locked(directory, session):
                    return 0
            time.sleep(0.05)
    except (ShellCommandError, OSError, KeyError, TypeError):
        return 2
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-record", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pane")
    target.add_argument("--detached", action="store_true")
    args = parser.parse_args()
    return finalize_detached(args.session_record) if args.detached else finalize(args.session_record, args.pane)


if __name__ == "__main__":
    raise SystemExit(main())
