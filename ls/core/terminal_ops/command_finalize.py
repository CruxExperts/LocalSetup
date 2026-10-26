"""Reconcile one exact completed family pane from tmux's pane-died hook."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import time

from .command_service import ShellCommandService
from .command_state import ShellCommandError, atomic_json, read_json, session_lock


_RETRYABLE_READ_CODES = frozenset({
    "native_unavailable", "native_command_failed", "native_ack_unknown",
})
_CLEANUP_STATES = frozenset({"pending", "dispatching", "unknown", "retained", "removed"})
_REMOVAL_INTENTS = frozenset({"none", "dispatching", "removed"})


def _hook_diagnostic(
    service: ShellCommandService, directory: Path, session: dict,
    operation: dict, *, phase: str, attempts: int, elapsed_ms: int,
    error: ShellCommandError | None = None, returncode: int | None = None,
) -> dict:
    """Merge one bounded hook observation into the exact operation under its lock."""
    path = service._paths(directory, str(operation["operation_id"]))["record"]
    current = read_json(path, limit=2_000_000)
    if any(current.get(key) != operation.get(key) for key in (
        "operation_id", "operation_handle", "session_handle", "session_key",
        "request_digest", "pane_id",
    )) or current.get("session_key") != session.get("session_key"):
        raise ShellCommandError("operation_identity_mismatch", "The hook operation changed identity.", effect="unknown")
    code = error.code if error is not None else None
    if code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        code = "other"
    effect = error.effect if error is not None and error.effect in {"none", "unknown", "known"} else None
    current["family_hook_diagnostic"] = {
        "schema_version": 1,
        "phase": phase,
        "attempts": min(max(attempts, 0), 255),
        "elapsed_ms": min(max(elapsed_ms, 0), 60_000),
        "error_code": code,
        "effect": effect,
        "returncode": returncode,
        "observed_cleanup": (current.get("cleanup_state")
                             if current.get("cleanup_state") in _CLEANUP_STATES else "unknown"),
        "removal_intent": (current.get("family_removal_intent")
                           if current.get("family_removal_intent") in _REMOVAL_INTENTS else "unknown"),
    }
    atomic_json(path, current)
    return current


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
        started = time.monotonic()
        deadline = started + 2
        attempts = 0
        last_error: ShellCommandError | None = None
        last_attempt_read_error = False
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
                now = time.monotonic()
                if attempts > 0 and now >= deadline:
                    result = 2 if last_attempt_read_error else 1
                    _hook_diagnostic(
                        service, directory, session, operation,
                        phase="read_error" if last_attempt_read_error else "pending",
                        attempts=attempts, elapsed_ms=int((now - started) * 1000),
                        error=last_error, returncode=result,
                    )
                    return result
                attempts += 1
                operation = _hook_diagnostic(
                    service, directory, session, operation, phase="reconciling",
                    attempts=attempts, elapsed_ms=int((now - started) * 1000),
                    error=last_error,
                )
                finished = operation.get("finished_at")
                finished_key = (float(finished) if type(finished) in {int, float} else 0.0,
                                str(operation["operation_id"]))
                try:
                    for sibling in service._strict_ops(directory, session):
                        sibling_finished = sibling.get("finished_at")
                        sibling_key = (float(sibling_finished)
                                       if type(sibling_finished) in {int, float} else 0.0,
                                       str(sibling.get("operation_id")))
                        if (sibling_key < finished_key
                                and sibling.get("state") == "completed"
                                and sibling.get("process_scope") == "collected"
                                and sibling.get("output_drain") in {"available", "unavailable"}
                                and sibling.get("cleanup_state") in {"pending", "dispatching", "unknown"}):
                            service._reconcile(directory, session, sibling, prune_older=False)
                    updated = service._reconcile(directory, session, operation)
                    # A removed trigger can be superseded by a still-visible
                    # newer result while an older sibling needs another pass.
                    siblings_pending = any(
                            item.get("operation_id") != operation.get("operation_id")
                            and (float(item["finished_at"])
                                 if type(item.get("finished_at")) in {int, float} else 0.0,
                                 str(item.get("operation_id"))) < finished_key
                            and item.get("state") == "completed"
                            and item.get("process_scope") == "collected"
                            and item.get("output_drain") in {"available", "unavailable"}
                            and item.get("cleanup_state") in {"pending", "dispatching", "unknown"}
                            for item in service._strict_ops(directory, session)
                    )
                except ShellCommandError as exc:
                    now = time.monotonic()
                    last_error = exc
                    retry_read = (exc.code in _RETRYABLE_READ_CODES
                                  and exc.effect == "none" and now < deadline)
                    last_attempt_read_error = retry_read
                    _hook_diagnostic(
                        service, directory, session, operation,
                        phase="retrying_read" if retry_read else "read_error",
                        attempts=attempts, elapsed_ms=int((now - started) * 1000),
                        error=exc, returncode=None if retry_read else 2,
                    )
                    if not retry_read:
                        return 2
                except (OSError, KeyError, TypeError):
                    _hook_diagnostic(
                        service, directory, session, operation, phase="error",
                        attempts=attempts,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        returncode=2,
                    )
                    return 2
                else:
                    now = time.monotonic()
                    last_attempt_read_error = False
                    retry_pending = updated.get("cleanup_state") == "pending" or siblings_pending
                    if not retry_pending or now >= deadline:
                        result = 0 if updated.get("cleanup_state") == "removed" and not siblings_pending else 1
                        _hook_diagnostic(
                            service, directory, session, operation,
                            phase="settled" if not retry_pending else "pending",
                            attempts=attempts, elapsed_ms=int((now - started) * 1000),
                            error=last_error, returncode=result,
                        )
                        return result
                    _hook_diagnostic(
                        service, directory, session, operation, phase="pending",
                        attempts=attempts, elapsed_ms=int((now - started) * 1000),
                        error=last_error,
                    )
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
