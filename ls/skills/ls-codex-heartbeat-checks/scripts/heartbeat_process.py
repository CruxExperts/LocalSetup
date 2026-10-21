"""Bounded POSIX process I/O for heartbeat commands; not a sandbox."""
from __future__ import annotations
import os
import selectors
import signal
import subprocess
import threading
import time
from pathlib import Path

OUTPUT_LIMIT = 4 * 1024 * 1024
INPUT_LIMIT = 128 * 1024
GRACE_SECONDS = 0.5


def _signal_group(pid: int, number: int) -> None:
    try:
        os.killpg(pid, number)
    except ProcessLookupError:
        pass


def _cleanup(proc: subprocess.Popen) -> bool:
    """Signal the original group even after its leader has exited."""
    _signal_group(proc.pid, signal.SIGTERM)
    time.sleep(GRACE_SECONDS)
    _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def execute(argv: list[str], *, cwd: Path, timeout: float,
            stdin_text: str | None = None, output_limit: int = OUTPUT_LIMIT,
            receipt=None, idle_timeout: float | None = None) -> dict:
    """Bound output before accumulation; include pipe delivery in the deadline."""
    if stdin_text is not None and len(stdin_text) > INPUT_LIMIT:
        raise ValueError("heartbeat prompt exceeds input limit")
    prompt = None if stdin_text is None else stdin_text.encode("utf-8")
    if prompt is not None and len(prompt) > INPUT_LIMIT:
        raise ValueError("heartbeat prompt exceeds input limit")
    streams = selectors.DefaultSelector()
    cancelled = []
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda signum, frame: cancelled.append(signum))
    proc = None
    tails = {"stdout": b"", "stderr": b""}
    total = 0
    reason = None
    reaped = False
    deadline = time.monotonic() + timeout
    activity = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, shell=False, stdin=subprocess.PIPE if prompt is not None
                                else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        for name in ("stdout", "stderr"):
            pipe = getattr(proc, name)
            os.set_blocking(pipe.fileno(), False)
            streams.register(pipe, selectors.EVENT_READ, name)
        offset = 0
        if proc.stdin is not None:
            os.set_blocking(proc.stdin.fileno(), False)
            if prompt:
                streams.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                proc.stdin.close()
        while streams.get_map() or proc.poll() is None:
            if cancelled:
                reason = "cancelled"
                break
            if idle_timeout is not None and time.monotonic() - activity >= idle_timeout:
                reason = "no_progress_timeout"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            for key, _ in streams.select(min(remaining, 0.1)):
                pipe, name = key.fileobj, key.data
                if name == "stdin":
                    try:
                        offset += os.write(pipe.fileno(), prompt[offset:offset + 8192])
                    except BrokenPipeError:
                        offset = len(prompt)
                    if offset == len(prompt):
                        streams.unregister(pipe)
                        pipe.close()
                    continue
                data = os.read(pipe.fileno(), min(8192, output_limit - total + 1))
                if not data:
                    streams.unregister(pipe)
                    pipe.close()
                    continue
                total += len(data)
                if total > output_limit:
                    reason = "output_limit"
                    break
                if receipt is not None:
                    if name == "stdout":
                        try:
                            if receipt.feed(data):
                                activity = time.monotonic()
                        except ValueError:
                            reason = "protocol_error"
                            break
                else:
                    tails[name] = (tails[name] + data)[-48000:]
            if reason:
                break
        protocol = None
        if receipt is not None and reason is None:
            try:
                protocol = receipt.finish(proc.returncode)
                if not protocol["completed"]:
                    reason = "protocol_failed"
            except ValueError:
                reason = "protocol_error"
        if reason:
            reaped = _cleanup(proc)
        else:
            reaped = proc.poll() is not None
        actual = proc.returncode
        result = {"pid": proc.pid, "pgid": proc.pid, "sid": proc.pid,
                  "process_returncode": actual, "cleanup_reaped": reaped,
                  "timed_out": reason == "timeout", "termination_reason": reason,
                  "output_bytes_observed": total, "output_limit_bytes": output_limit,
                  "returncode": actual if reason is None else
                  (124 if reason == "timeout" else 128 + cancelled[0] if cancelled else 1)}
        if receipt is not None:
            result["protocol"] = protocol
        result.update({name + "_tail": data.decode("utf-8", errors="replace")[-12000:]
                       for name, data in tails.items()})
        return result
    finally:
        try:
            if proc is not None and not reaped:
                _cleanup(proc)
        finally:
            streams.close()
            if proc is not None:
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
            for number, handler in previous.items():
                signal.signal(number, handler)
