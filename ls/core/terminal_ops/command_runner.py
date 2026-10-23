"""Execute one operation in a fresh tmux pane and record its process scope."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from typing import Any

from .command_state import ShellCommandError, atomic_json, process_info, read_json, session_lock
from .credentials import child_environment


_TERMINATE = False
_CHILD_BOOTSTRAP = """import os,signal,sys
os.kill(os.getpid(), signal.SIGSTOP)
try:
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
except OSError:
    os._exit(127)
"""


def _signal_handler(_signum: int, _frame: Any) -> None:
    global _TERMINATE
    _TERMINATE = True


def _become_subreaper() -> bool:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        result = library.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
        if result != 0:
            raise OSError(ctypes.get_errno(), "prctl failed")
        return True
    except (AttributeError, OSError):
        # Process-scope tracking remains conservative if this kernel does not
        # expose subreaper support: collection will stay pending when unknown.
        return False


def _children(parent_pid: int) -> list[dict[str, int]]:
    rows: list[dict[str, int]] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return rows
    for entry in entries:
        if not entry.name.isdigit():
            continue
        info = process_info(int(entry.name))
        if info is not None and info["ppid"] == parent_pid:
            rows.append(info)
    return rows


def _signal_exact(pid: int, starttime: int | None, signum: int) -> bool:
    """Signal only a process whose kernel identity remains the recorded one."""
    if starttime is None or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    current = process_info(pid)
    if current is None or current["starttime"] != starttime:
        return False
    try:
        descriptor = os.pidfd_open(pid)
        try:
            current = process_info(pid)
            if current is None or current["starttime"] != starttime:
                return False
            signal.pidfd_send_signal(descriptor, signum)
            return True
        finally:
            os.close(descriptor)
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _signal_direct_child(process: subprocess.Popen[bytes], signum: int) -> None:
    # An unreaped direct child cannot have its PID reused. Only this runner
    # polls or waits for it, so no external process can enter this identity.
    if process.returncode is not None:
        return
    try:
        os.kill(process.pid, signum)
    except ProcessLookupError:
        pass


def _update(path: Path, record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    with session_lock(path.parent.parent, blocking=True):
        latest = read_json(path)
        if (latest.get("operation_id") != record.get("operation_id")
                or latest.get("session_key") != record.get("session_key")):
            raise ShellCommandError("operation_identity_mismatch", "The operation record changed identity.", effect="unknown")
        latest.update(changes)
        record.clear()
        record.update(latest)
        atomic_json(path, latest)
    return record


def _await_finalization(
    operation_path: Path, record: dict[str, Any], drained_path: Path,
    finalize_path: Path, exit_code: int,
) -> int:
    _update(operation_path, record, state="draining", effect_certainty="known",
            process_scope="collected", exit_code=exit_code,
            command_finished_at=time.time())
    while not finalize_path.exists():
        time.sleep(0.05)
    try:
        final = read_json(finalize_path, limit=4096)
    except ShellCommandError:
        final = {}
    if (final.get("state") != "finalize"
            or final.get("operation_id") != record.get("operation_id")
            or final.get("session_key") != record.get("session_key")):
        _update(operation_path, record, state="unknown", effect_certainty="unknown",
                process_scope="unknown", detail="collector finalization identity did not match")
        return exit_code
    if final.get("output_drain") == "available":
        try:
            drained = read_json(drained_path, limit=4096)
        except ShellCommandError:
            drained = {}
        if drained.get("state") != "drained" or drained.get("storage") != "available":
            _update(operation_path, record, state="unknown", effect_certainty="unknown",
                    process_scope="unknown", detail="collector drain receipt did not match finalization")
            return exit_code
    with session_lock(operation_path.parent.parent, blocking=True):
        try:
            latest = read_json(operation_path)
        except ShellCommandError:
            latest = {}
        if (latest.get("operation_id") != record.get("operation_id")
                or latest.get("session_key") != record.get("session_key")):
            latest = {**record, "state": "unknown", "effect_certainty": "unknown",
                      "process_scope": "unknown",
                      "detail": "operation identity changed during finalization"}
        else:
            latest.update(state="completed", effect_certainty="known",
                          process_scope="collected", finished_at=time.time(),
                          output_drain=final.get("output_drain", "available"))
        record.update(latest)
        atomic_json(operation_path, record)
    return exit_code


def run_operation(
    operation_path: Path, payload_path: Path, release_path: Path,
    drained_path: Path, finalize_path: Path,
) -> int:
    global _TERMINATE
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    if not _become_subreaper():
        return 125

    deadline = time.monotonic() + 120.0
    while not release_path.exists() and time.monotonic() < deadline and not _TERMINATE:
        time.sleep(0.05)
    if _TERMINATE or not release_path.exists():
        return 125
    try:
        release = read_json(release_path, limit=4096)
        if release.get("state") != "release":
            return 125
        record = read_json(operation_path)
        if record.get("state") != "running":
            return 125
        payload = read_json(payload_path, limit=16 * 1024 * 1024)
    except ShellCommandError:
        return 125

    runner = process_info(os.getpid())
    if runner is None:
        return 125
    try:
        tty = os.ttyname(0) if os.isatty(0) else None
    except OSError:
        tty = None
    try:
        if os.getpgrp() != os.getpid():
            os.setpgid(0, 0)
    except OSError:
        # Keep the observed process group; the service verifies it before any
        # cancellation signal is sent.
        pass
    runner = process_info(os.getpid()) or runner
    try:
        _update(
            operation_path,
            record,
            runner_pid=os.getpid(),
            runner_starttime=runner["starttime"],
            runner_pgrp=runner["pgrp"],
            runner_session=runner["session"],
            pane_tty=tty,
            process_scope="tracking",
            runner_started_at=time.time(),
        )
    except ShellCommandError:
        return 125

    cwd = payload.get("cwd")
    environment = payload.get("env")
    inherited_pythonpath = payload.get("inherited_pythonpath")
    argv = payload.get("argv")
    script = payload.get("script")
    stdin_value = payload.get("stdin_b64")
    if (not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir()
            or not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items())
            or (inherited_pythonpath is not None and
                (not isinstance(inherited_pythonpath, str) or "\0" in inherited_pythonpath))
            or (argv is None) == (script is None)):
        _update(operation_path, record, state="unknown", effect_certainty="unknown",
                detail="The durable command payload is malformed.", finished_at=time.time())
        return 125
    if argv is not None:
        if (not isinstance(argv, list) or not argv or any(not isinstance(item, str) or "\0" in item for item in argv)):
            _update(operation_path, record, state="unknown", effect_certainty="unknown",
                    detail="The durable argv request is malformed.", finished_at=time.time())
            return 125
        command = argv
    else:
        if not isinstance(script, str) or "\0" in script:
            _update(operation_path, record, state="unknown", effect_certainty="unknown",
                    detail="The explicit Bash script is malformed.", finished_at=time.time())
            return 125
        command = ["/bin/bash", "--noprofile", "--norc", "-c", script]

    safe_env = child_environment(os.environ)
    # The helper's import path is transport state, not the user's environment.
    # Preserve absence and empty values separately, then allow explicit overrides.
    safe_env.pop("PYTHONPATH", None)
    if inherited_pythonpath is not None:
        safe_env["PYTHONPATH"] = inherited_pythonpath
    if script is not None:
        for name in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
            safe_env.pop(name, None)
    for name, value in environment.items():
        if name.endswith("_SUDO_KEY"):
            continue
        if not name or "=" in name or "\0" in name or "\0" in value:
            _update(operation_path, record, state="unknown", effect_certainty="unknown",
                    detail="The explicit command environment is malformed.", finished_at=time.time())
            return 125
        safe_env[name] = value
    if any(name.endswith("_SUDO_KEY") for name in safe_env):
        _update(operation_path, record, state="unknown", effect_certainty="unknown",
                detail="The command environment could not be proven credential-free.", finished_at=time.time())
        return 125

    stdin_bytes: bytes | None = None
    if stdin_value is not None:
        if not isinstance(stdin_value, str):
            _update(operation_path, record, state="unknown", effect_certainty="unknown",
                    detail="The explicit stdin payload is malformed.", finished_at=time.time())
            return 125
        try:
            stdin_bytes = base64.b64decode(stdin_value, validate=True)
        except (ValueError, base64.binascii.Error):
            _update(operation_path, record, state="unknown", effect_certainty="unknown",
                    detail="The explicit stdin payload is malformed.", finished_at=time.time())
            return 125

    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _CHILD_BOOTSTRAP, *command],
            cwd=cwd,
            env=safe_env,
            stdin=subprocess.PIPE if stdin_bytes is not None else None,
            stdout=None,
            stderr=None,
            shell=False,
            close_fds=True,
            preexec_fn=os.setpgrp,
        )
    except OSError as exc:
        _update(operation_path, record, state="draining", effect_certainty="known",
                exit_code=127, command_spawn_error=exc.__class__.__name__,
                process_scope="collected", started_at=time.time(),
                command_finished_at=time.time())
        payload_path.unlink(missing_ok=True)
        return _await_finalization(operation_path, record, drained_path, finalize_path, 127)

    # The bootstrap stops itself before executing user code. Popen has already
    # returned, so foreground ownership can be established without an exec race.
    stopped_pid, stopped_status = os.waitpid(process.pid, os.WUNTRACED)
    if stopped_pid != process.pid or not os.WIFSTOPPED(stopped_status):
        _update(operation_path, record, state="unknown", effect_certainty="unknown",
                process_scope="unknown", detail="command bootstrap did not stop before exec")
        return 125
    child = process_info(process.pid)
    if child is None:
        _signal_direct_child(process, signal.SIGKILL)
        process.wait()
        _update(operation_path, record, state="unknown", effect_certainty="unknown",
                process_scope="unknown", detail="stopped command identity is unavailable")
        return 125
    child_fields = {
        "child_pid": process.pid,
        "child_starttime": child["starttime"] if child else None,
        "child_pgrp": child["pgrp"] if child else process.pid,
        "child_session": child["session"] if child else None,
        "started_at": time.time(),
        "process_scope": "tracking",
    }
    try:
        with session_lock(operation_path.parent.parent, blocking=True):
            latest = read_json(operation_path)
            if (latest.get("operation_id") != record.get("operation_id")
                    or latest.get("session_key") != record.get("session_key")):
                raise ShellCommandError("operation_identity_mismatch", "The operation record changed identity.", effect="unknown")
            latest.update(child_fields)
            record.clear()
            record.update(latest)
            atomic_json(operation_path, latest)
    except ShellCommandError:
        _signal_direct_child(process, signal.SIGKILL)
        process.wait()
        payload_path.unlink(missing_ok=True)
        return 125

    payload_path.unlink(missing_ok=True)
    if tty:
        previous_sigttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(0, process.pid)
        except OSError as exc:
            _update(operation_path, record, state="unknown", effect_certainty="unknown",
                    process_scope="unknown", detail="command foreground ownership could not be established")
            _signal_direct_child(process, signal.SIGKILL)
            process.wait()
            return 125
        finally:
            signal.signal(signal.SIGTTOU, previous_sigttou)
    _signal_direct_child(process, signal.SIGCONT)
    pending_input = memoryview(stdin_bytes) if stdin_bytes is not None else None
    if process.stdin is not None:
        os.set_blocking(process.stdin.fileno(), False)
    terminate_started: float | None = None
    while process.poll() is None:
        if _TERMINATE:
            if terminate_started is None:
                terminate_started = time.monotonic()
                _signal_direct_child(process, signal.SIGTERM)
            elif time.monotonic() - terminate_started >= 1.5:
                _signal_direct_child(process, signal.SIGKILL)
        if pending_input is not None and process.stdin is not None:
            if len(pending_input) == 0:
                process.stdin.close()
                pending_input = None
            else:
                _, writable, _ = select.select([], [process.stdin.fileno()], [], 0)
                if writable:
                    try:
                        written = os.write(process.stdin.fileno(), pending_input[:65536])
                        pending_input = pending_input[written:]
                    except (BrokenPipeError, OSError):
                        process.stdin.close()
                        pending_input = None
        time.sleep(0.05)
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    exit_code = process.returncode
    if tty:
        previous_sigttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(0, os.getpgrp())
        except OSError:
            pass
        finally:
            signal.signal(signal.SIGTTOU, previous_sigttou)
    if exit_code is None:
        exit_code = 125
    if exit_code < 0:
        exit_code = 128 + abs(exit_code)

    _update(operation_path, record, state="draining", effect_certainty="known",
            exit_code=exit_code, process_scope="draining", command_finished_at=time.time())
    descendant_termination_started: float | None = None
    while True:
        try:
            while True:
                reaped, _ = os.waitpid(-1, os.WNOHANG)
                if reaped == 0:
                    break
        except ChildProcessError:
            break
        except OSError as exc:
            if exc.errno == errno.ECHILD:
                break
            _update(operation_path, record, state="unknown", effect_certainty="unknown",
                    process_scope="unknown", detail="The command process scope could not be reconciled.")
            return exit_code
        remaining = _children(os.getpid())
        if not remaining:
            break
        if _TERMINATE:
            if descendant_termination_started is None:
                descendant_termination_started = time.monotonic()
            descendant_signal = (signal.SIGKILL if time.monotonic() - descendant_termination_started >= 1.5
                                 else signal.SIGTERM)
            for child_info in remaining:
                _signal_exact(child_info["pid"], child_info["starttime"], descendant_signal)
        time.sleep(0.05)
    # Keep the exact pane alive until the service closes the collector pipe and
    # observes its durable drain receipt. This separates process completion,
    # output drain and pane cleanup without losing the final bytes.
    return _await_finalization(operation_path, record, drained_path, finalize_path, exit_code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", required=True)
    parser.add_argument("--payload", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--drained", required=True)
    parser.add_argument("--finalize", required=True)
    args = parser.parse_args(argv)
    return run_operation(Path(args.operation), Path(args.payload), Path(args.release),
                         Path(args.drained), Path(args.finalize))


if __name__ == "__main__":
    raise SystemExit(main())
