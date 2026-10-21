#!/usr/bin/env python3
# Purpose: Run all tasks for a named trigger in sequence (used by generated cron).
# Created: 2026-02-24
# Last updated: 2026-02-24

from __future__ import annotations
import argparse
import codecs
import os
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, TextIO

from _cron_manifest import (
    MAX_DELAY_SECONDS,
    MAX_TRIGGER_LEN,
    ManifestError,
    clean_display,
    load_manifest,
    normalize_delay_seconds,
    validate_identifier,
    validate_manifest,
)

LOG_TAIL_CHARS = 4000
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class RunnerLogError(ValueError):
    """Raised when durable runner logging cannot be initialized or written."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log_file(log_dir: Path | None, trigger_name: str) -> Path | None:
    if log_dir is None:
        return None
    existed = log_dir.exists()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RunnerLogError(f"Invalid log directory {log_dir}: {exc}") from exc
    if not log_dir.is_dir():
        raise RunnerLogError(f"Invalid log directory {log_dir}: not a directory")
    if os.name == "posix":
        try:
            mode = stat.S_IMODE(log_dir.stat().st_mode)
            if existed and mode & 0o077:
                raise RunnerLogError(f"Invalid log directory {log_dir}: permissions must not allow group/other access")
            os.chmod(log_dir, PRIVATE_DIR_MODE)
        except RunnerLogError:
            raise
        except OSError as exc:
            raise RunnerLogError(f"Invalid log directory {log_dir}: failed to set private permissions: {exc}") from exc
    safe_trigger = "".join(ch if ch.isalnum() or ch in "._@+-" else "_" for ch in trigger_name)[:MAX_TRIGGER_LEN]
    return log_dir / f"{safe_trigger}.log"


def _append_tail(tail: bytearray, chunk: bytes) -> None:
    tail.extend(chunk)
    excess = len(tail) - LOG_TAIL_CHARS
    if excess > 0:
        del tail[:excess]


def _stream_output(
    source: BinaryIO,
    destination: TextIO,
    tail: bytearray,
    stream_name: str,
    relay_errors: list[str],
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    relay_error: str | None = None
    try:
        while chunk := source.read(8192):
            _append_tail(tail, chunk)
            if relay_error is not None:
                continue
            text = decoder.decode(chunk)
            if not text:
                continue
            try:
                destination.write(text)
                destination.flush()
            except (OSError, UnicodeError, ValueError) as exc:
                relay_error = f"{stream_name} relay failed: {type(exc).__name__}: {exc}"
        if relay_error is None:
            remaining = decoder.decode(b"", final=True)
            if remaining:
                try:
                    destination.write(remaining)
                    destination.flush()
                except (OSError, UnicodeError, ValueError) as exc:
                    relay_error = f"{stream_name} relay failed: {type(exc).__name__}: {exc}"
    except OSError as exc:
        relay_error = f"{stream_name} pipe read failed: {type(exc).__name__}: {exc}"
    if relay_error is not None:
        relay_errors.append(relay_error)


def _safe_stderr(message: str) -> None:
    try:
        print(message, file=sys.stderr)
    except (OSError, UnicodeError, ValueError):
        pass


def _record_relay_failures(log_path: Path | None, task_id: str, relay_errors: list[str]) -> bool:
    details = "; ".join(relay_errors)
    _safe_stderr(f"[run_trigger] Task {task_id} output relay failed: {details}")
    try:
        _append_log(log_path, f"task_output_error id={task_id} detail={details}")
    except RunnerLogError as exc:
        _safe_stderr(f"[run_trigger] {exc}")
        return False
    return True


def _append_log(path: Path | None, message: str) -> None:
    if path is None:
        return
    try:
        if os.name == "posix" and path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RunnerLogError(f"Invalid log file {path}: permissions must not allow group/other access")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE)
        if os.name == "posix":
            os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"{_utc_now()} {message}\n")
    except RunnerLogError:
        raise
    except OSError as exc:
        raise RunnerLogError(f"Failed to write log file {path}: {exc}") from exc


def _load_validated_manifest(manifest_path: Path) -> dict:
    return validate_manifest(load_manifest(manifest_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run tasks for a trigger in sequence.")
    parser.add_argument("--manifest", required=True, help="Path to manifest.yaml")
    parser.add_argument("--repo-root", help="Working directory for commands (default: manifest parent)")
    parser.add_argument("--log-dir", help="Append durable runner/task logs under this directory")
    parser.add_argument(
        "--delay-seconds",
        default=0,
        type=int,
        help=f"Delay execution before running tasks, for @reboot cron entries (0..{MAX_DELAY_SECONDS})",
    )
    parser.add_argument("trigger", help="Trigger name")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.is_file():
        print(f"[run_trigger] Not a file: {manifest_path}", file=sys.stderr)
        return 1
    repo_root = Path(args.repo_root).resolve() if args.repo_root else manifest_path.parent
    if not repo_root.is_dir():
        print(f"[run_trigger] Not a directory: {repo_root}", file=sys.stderr)
        return 1

    try:
        trigger_name = validate_identifier(args.trigger, "trigger", MAX_TRIGGER_LEN)
        delay_seconds = normalize_delay_seconds(args.delay_seconds, "delay_seconds")
    except ManifestError as exc:
        print(f"[run_trigger] {exc}", file=sys.stderr)
        return 1
    try:
        log_path = _log_file(Path(args.log_dir).resolve() if args.log_dir else None, trigger_name)
        _append_log(
            log_path,
            f"runner_start trigger={trigger_name} manifest={manifest_path} repo_root={repo_root} delay_seconds={delay_seconds}",
        )
    except RunnerLogError as exc:
        print(f"[run_trigger] {exc}", file=sys.stderr)
        return 1
    if delay_seconds:
        time.sleep(delay_seconds)

    try:
        data = _load_validated_manifest(manifest_path)
    except ManifestError as exc:
        print(f"[run_trigger] {exc}", file=sys.stderr)
        try:
            _append_log(log_path, f"runner_exit trigger={trigger_name} exit_code=1 reason=manifest_validation_failed")
        except RunnerLogError:
            pass
        return 1

    triggers = data.get("triggers") or {}
    if trigger_name not in triggers:
        print(f"[run_trigger] Unknown trigger: {trigger_name}", file=sys.stderr)
        try:
            _append_log(log_path, f"runner_exit trigger={trigger_name} exit_code=1 reason=unknown_trigger")
        except RunnerLogError as exc:
            print(f"[run_trigger] {exc}", file=sys.stderr)
        return 1

    tasks = data.get("tasks") or []
    tasks_for_trigger = [task for task in tasks if task["trigger"] == trigger_name and task["enabled"]]
    tasks_for_trigger.sort(key=lambda task: task["sequence_order"])

    for task in tasks_for_trigger:
        task_id = clean_display(task["id"], 128)
        argv = task["argv"]
        timeout = task["timeout_seconds"]
        try:
            _append_log(log_path, f"task_start id={task_id} timeout_seconds={timeout}")
        except RunnerLogError as exc:
            print(f"[run_trigger] {exc}", file=sys.stderr)
            return 1
        try:
            process = subprocess.Popen(
                argv,
                shell=False,
                cwd=repo_root,
                env={**os.environ, "LANG": "C"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_tail = bytearray()
            stderr_tail = bytearray()
            relay_errors: list[str] = []
            stdout_reader = threading.Thread(
                target=_stream_output,
                args=(process.stdout, sys.stdout, stdout_tail, "stdout", relay_errors),
            )
            stderr_reader = threading.Thread(
                target=_stream_output,
                args=(process.stderr, sys.stderr, stderr_tail, "stderr", relay_errors),
            )
            stdout_reader.start()
            stderr_reader.start()
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                stdout_reader.join()
                stderr_reader.join()
                if relay_errors:
                    if not _record_relay_failures(log_path, task_id, relay_errors):
                        return 1
                    try:
                        _append_log(log_path, f"runner_exit trigger={trigger_name} exit_code=1 reason=output_relay_failed")
                    except RunnerLogError as exc:
                        _safe_stderr(f"[run_trigger] {exc}")
                    return 1
                print(f"[run_trigger] Task {task_id} timed out after {timeout}s", file=sys.stderr)
                try:
                    _append_log(log_path, f"task_timeout id={task_id} timeout_seconds={timeout}")
                    _append_log(log_path, f"runner_exit trigger={trigger_name} exit_code=124")
                except RunnerLogError as exc:
                    print(f"[run_trigger] {exc}", file=sys.stderr)
                    return 1
                return 124
            stdout_reader.join()
            stderr_reader.join()
            stdout_text = stdout_tail.decode("utf-8", errors="replace")
            stderr_text = stderr_tail.decode("utf-8", errors="replace")
            try:
                _append_log(
                    log_path,
                    f"task_exit id={task_id} exit_code={returncode} stdout_tail={stdout_text!r} stderr_tail={stderr_text!r}",
                )
            except RunnerLogError as exc:
                print(f"[run_trigger] {exc}", file=sys.stderr)
                return 1
            if relay_errors:
                if not _record_relay_failures(log_path, task_id, relay_errors):
                    return 1
                try:
                    _append_log(log_path, f"runner_exit trigger={trigger_name} exit_code=1 reason=output_relay_failed")
                except RunnerLogError as exc:
                    _safe_stderr(f"[run_trigger] {exc}")
                return 1
            if returncode != 0:
                print(f"[run_trigger] Task {task_id} exited {returncode}", file=sys.stderr)
                try:
                    _append_log(log_path, f"runner_exit trigger={trigger_name} exit_code={returncode}")
                except RunnerLogError as exc:
                    print(f"[run_trigger] {exc}", file=sys.stderr)
                    return 1
                return returncode
        except Exception as exc:
            print(f"[run_trigger] Task {task_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                _append_log(log_path, f"task_error id={task_id} error={type(exc).__name__}: {exc}")
                _append_log(log_path, f"runner_exit trigger={trigger_name} exit_code=1")
            except RunnerLogError as log_exc:
                print(f"[run_trigger] {log_exc}", file=sys.stderr)
                return 1
            return 1
    try:
        _append_log(log_path, f"runner_exit trigger={trigger_name} exit_code=0")
    except RunnerLogError as exc:
        print(f"[run_trigger] {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
