"""Exercise the managed Bash gate against a real controlling pseudo-terminal."""

from __future__ import annotations

import os
from pathlib import Path
import pty
import select
import signal
import sys
import time

import pytest

from ls.core.terminal_ops.models import PaneError, PaneTransition
from ls.core.terminal_ops.shell import (
    read_prompt_state,
    stage_transition,
    write_bash_bootstrap,
)


def _until(predicate, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail("managed Bash did not reach the expected observable state")


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux process identity required")
def test_real_bash_dispatch_and_continuation_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding_key = "a" * 64
    bootstrap = write_bash_bootstrap(tmp_path, binding_key, pane_id="test-native-pane")
    master, slave = pty.openpty()
    child = os.fork()
    if child == 0:
        os.close(master)
        os.setsid()
        import fcntl
        import termios

        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        for stream in (0, 1, 2):
            os.dup2(slave, stream)
        if slave > 2:
            os.close(slave)
        os.environ["INPUTRC"] = "/dev/null"
        os.execv("/bin/bash", ["bash", "--noprofile", "--rcfile", str(bootstrap), "-i"])
    os.close(slave)
    try:
        def primary():
            try:
                state = read_prompt_state(tmp_path, binding_key)
            except Exception:
                return None
            return state if state.get("phase") == "primary" else None

        prompt = _until(primary)
        assert prompt["shell_pid"] == child
        binding = {
            "binding_key": binding_key,
            "pane_id": "test-native-pane",
            "pane_tty": prompt["pane_tty"],
            "shell_pid": child,
            "shell_path": prompt["shell_path"],
            "machine_generation": prompt["machine_generation"],
        }
        from ls.core.terminal_ops.shell import reconcile_transition

        first = PaneTransition("first", (
            sys.executable, "-c",
            "import sys; assert sys.argv[1:] == ['', 'line\\n']; print('managed-pty-success')",
            "", "line\n",
        ))
        stage_transition(tmp_path, binding, first, prompt_nonce=prompt["prompt_nonce"])
        os.write(master, b"\x1b[25~")
        receipt = _until(lambda: _completed(tmp_path, binding_key, "first"))
        assert receipt["exit_code"] == 0
        output = bytearray()
        while select.select([master], [], [], 0.05)[0]:
            output.extend(os.read(master, 65536))
        assert b"managed-pty-success" in output

        prompt = _until(lambda: (state := primary()) and state["prompt_nonce"] != receipt["prompt_nonce"] and state)
        with pytest.raises(PaneError, match="operation ID already has a record"):
            stage_transition(tmp_path, binding, first, prompt_nonce=prompt["prompt_nonce"])
        assert not (tmp_path / "terminal_ops" / binding_key / "writer-lease").exists()
        from ls.core.terminal_ops import shell as shell_module
        real_atomic = shell_module._atomic_json
        def fail_before_staging(path, payload, **kwargs):
            if path.name == "lease.json" and payload.get("operation_id") == "preflight-failure":
                raise OSError("synthetic private-state failure before dispatch")
            return real_atomic(path, payload, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(shell_module, "_atomic_json", fail_before_staging)
            with pytest.raises(OSError, match="before dispatch"):
                stage_transition(tmp_path, binding, PaneTransition("preflight-failure", ("/usr/bin/true",)),
                                 prompt_nonce=prompt["prompt_nonce"])
        assert not (tmp_path / "terminal_ops" / binding_key / "writer-lease").exists()
        next_operation = PaneTransition("fresh-after-repeated-id", ("/usr/bin/true",))
        stage_transition(tmp_path, binding, next_operation, prompt_nonce=prompt["prompt_nonce"])
        os.write(master, b"\x1b[25~")
        next_receipt = _until(lambda: _completed(tmp_path, binding_key, "fresh-after-repeated-id"))
        assert next_receipt["exit_code"] == 0
        prompt = _until(lambda: (state := primary()) and state["prompt_nonce"] != next_receipt["prompt_nonce"] and state)
        absent = tmp_path / "does-not-exist"
        must_not_run = tmp_path / "failed-cd-workload"
        failed_cd = PaneTransition("failed-cd", (
            sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
            str(must_not_run),
        ), cwd=str(absent))
        stage_transition(tmp_path, binding, failed_cd, prompt_nonce=prompt["prompt_nonce"])
        os.write(master, b"\x1b[25~")
        failed_receipt = _until(lambda: _completed(tmp_path, binding_key, "failed-cd"))
        assert failed_receipt["exit_code"] != 0
        assert not must_not_run.exists()
        prompt = _until(lambda: (state := primary()) and state["prompt_nonce"] != failed_receipt["prompt_nonce"] and state)
        os.write(master, b"if true; then\n")
        _until(lambda: select.select([master], [], [], 0.05)[0] and b">" in os.read(master, 65536))
        second = PaneTransition("continuation", ("/usr/bin/printf", "must-not-run"))
        with pytest.raises(PaneError, match="not at a verified primary prompt"):
            stage_transition(tmp_path, binding, second, prompt_nonce=prompt["prompt_nonce"])
        os.write(master, b"\x1b[25~")
        assert not (tmp_path / "terminal_ops" / binding_key / "continuation.json").exists()
        os.write(master, b"fi\n")
        prompt = _until(lambda: (state := primary()) and state["prompt_nonce"] != prompt["prompt_nonce"] and state)
        third = PaneTransition("typed-input", ("/usr/bin/printf", "must-not-run"))
        stage_transition(tmp_path, binding, third, prompt_nonce=prompt["prompt_nonce"])
        os.write(master, b"echo existing input")
        os.write(master, b"\x1b[25~")
        time.sleep(0.15)
        assert reconcile_transition(tmp_path, binding_key=binding_key, operation_id="typed-input")["state"] == "unknown"
    finally:
        os.killpg(child, signal.SIGKILL)
        os.waitpid(child, 0)
        os.close(master)


def _completed(root: Path, key: str, operation_id: str):
    path = root / "terminal_ops" / key / f"{operation_id}.json"
    try:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if value.get("state") == "completed" else None
