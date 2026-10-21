"""Real child-process checks for bounded heartbeat I/O."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/ls-codex-heartbeat-checks/scripts/heartbeat_process.py"
spec = importlib.util.spec_from_file_location("heartbeat_process_fixture", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def run(tmp_path, code, **kwargs):
    return runner.execute([sys.executable, "-c", code], cwd=tmp_path, **kwargs)


def test_bidirectional_input_and_bounded_tails(tmp_path):
    result = run(tmp_path, "import sys; print(sys.stdin.read()); print('diagnostic',file=sys.stderr)",
                 timeout=3, stdin_text="fixture")
    assert result["returncode"] == result["process_returncode"] == 0
    assert result["stdout_tail"] == "fixture\n"
    assert result["stderr_tail"] == "diagnostic\n"


def test_flood_stops_before_unbounded_capture(tmp_path):
    result = run(tmp_path, "import os\nwhile True: os.write(1,b'x'*8192)", timeout=3,
                 output_limit=16384)
    assert result["termination_reason"] == "output_limit"
    assert result["output_bytes_observed"] == 16385
    assert len(result["stdout_tail"]) <= 12000
    assert result["returncode"] != 0 and result["cleanup_reaped"]


def test_stdin_delivery_shares_deadline(tmp_path):
    started = time.monotonic()
    result = run(tmp_path, "import time; time.sleep(30)", timeout=.2, stdin_text="x"*131072)
    assert result["timed_out"] and result["returncode"] == 124
    assert result["cleanup_reaped"] and time.monotonic() - started < 3


def test_descendant_held_pipes_do_not_wait_forever(tmp_path):
    result = run(tmp_path, "import os,time\nif os.fork()==0: time.sleep(30)",
                 timeout=.2)
    assert result["timed_out"] and result["process_returncode"] == 0
    assert result["returncode"] == 124 and result["cleanup_reaped"]


def test_prompt_limit_checks_encoded_bytes(tmp_path):
    with pytest.raises(ValueError, match="input limit"):
        run(tmp_path, "raise AssertionError('must not launch')", timeout=3,
            stdin_text="é"*70000)


def test_signal_cancels_and_restores_handler(tmp_path):
    ready = tmp_path / "ready"
    child = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(30)"
    code = f"""import sys,signal
sys.path.insert(0,{str(SCRIPT.parent)!r})
from heartbeat_process import execute
from pathlib import Path
before=signal.getsignal(signal.SIGTERM)
result=execute([sys.executable,'-c',{child!r}],
               cwd=Path({str(tmp_path)!r}),timeout=20)
assert signal.getsignal(signal.SIGTERM)==before
import json
print(json.dumps(result))
"""
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "child did not reach readiness"
        os.kill(proc.pid, signal.SIGTERM)
        out, _ = proc.communicate(timeout=4)
        import json
        result = json.loads(out)
        assert result["termination_reason"] == "cancelled"
        assert result["returncode"] == 143 and result["cleanup_reaped"]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_selector_failure_does_not_replace_signal_handlers(tmp_path, monkeypatch):
    before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    def fail():
        raise OSError("descriptor exhaustion fixture")
    monkeypatch.setattr(runner.selectors, "DefaultSelector", fail)
    with pytest.raises(OSError, match="descriptor exhaustion"):
        run(tmp_path, "raise AssertionError('must not launch')", timeout=3)
    assert {number: signal.getsignal(number) for number in before} == before
