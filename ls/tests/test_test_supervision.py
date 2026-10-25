"""Emergency cleanup stays confined to the owned scope and cannot report success."""
from types import SimpleNamespace
import subprocess

import pytest

from ls.core import test_supervision as supervision


@pytest.mark.parametrize("sampling_error", [False, True])
def test_emergency_or_sampler_failure_stops_and_reaps_only_owned_scope(monkeypatch, capsys, sampling_error):
    unit = "localsetup-tests-fixture.scope"
    calls = []
    state = {"stopped": False, "reaped": False, "clock": 0}
    class Process:
        def wait(self, timeout):
            if not state["stopped"]:
                raise subprocess.TimeoutExpired("fixture", timeout)
            state["reaped"] = True
            return -15
        def poll(self):
            return -15 if state["reaped"] else None
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[3] == unit
        if argv[2] == "stop":
            state["stopped"] = True
        return SimpleNamespace(stdout=f"/fixture/{unit}", returncode=0)
    def now():
        state["clock"] += 5
        return state["clock"]
    def pressure(*args):
        if sampling_error:
            raise OSError("unavailable sample")
        return SimpleNamespace(percent=96, working_bytes=96, window_bytes=100, charged_bytes=99)
    monkeypatch.setattr(supervision.subprocess, "Popen", lambda _: Process())
    monkeypatch.setattr(supervision.subprocess, "run", run)
    monkeypatch.setattr(supervision.time, "monotonic", now)
    monkeypatch.setattr(supervision, "sample_pressure", pressure)
    if sampling_error:
        with pytest.raises(OSError):
            supervision.supervise(["fixture"], unit, {}, 4)
    else:
        assert supervision.supervise(["fixture"], unit, {}, 4) == 4
        assert "Coverage is incomplete" in capsys.readouterr().out
    assert state["reaped"]
    assert sum(call[2] == "stop" for call in calls) == 1


def test_fixed_single_worker_never_constructs_adaptive_controller(monkeypatch):
    monkeypatch.setattr(supervision, "PressureController", lambda *_: pytest.fail("adaptive controller created"))
    monkeypatch.setattr(supervision.subprocess, "Popen", lambda _: SimpleNamespace(wait=lambda **_: 7, poll=lambda: 7))
    assert supervision.supervise(["fixture"], "unused.scope", {}, 1) == 7


def test_unrelated_command_flags_do_not_disable_watchdog():
    assert supervision.command_worker_limit(["other-runner", "-n1"], 4) == 4
    assert supervision.command_worker_limit(["python", "-m", "pytest", "-n1"], 4) == 1
