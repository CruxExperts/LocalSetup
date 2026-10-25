"""Resource failures must stop test execution rather than exhaust the host."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ls.core import test_resources as safety
from ls.core.test_workers import testing_configuration as read_testing_configuration


def test_kernel_limits_require_all_controllers_and_group_oom(tmp_path, monkeypatch):
    directory = tmp_path / "localsetup-tests-fixture.scope"
    directory.mkdir()
    monkeypatch.setattr(safety, "cgroup_directory", lambda: directory)
    values = {"memory.max": "67108864", "memory.swap.max": "0", "pids.max": "32",
              "cpu.max": "50000 100000", "memory.oom.group": "1"}
    for name, value in values.items():
        (directory / name).write_text(value)
    assert safety.verified_limits() == {"memory_bytes": 67108864, "swap_bytes": 0,
                                       "tasks": 32, "cpu_quota_percent": 50}
    for name, bad in (("memory.max", "max"), ("cpu.max", "max 100000"),
                      ("memory.swap.max", "1"), ("memory.oom.group", "0"),
                      ("pids.max", "max")):
        (directory / name).write_text(bad)
        with pytest.raises((ValueError, RuntimeError)):
            safety.verified_limits()
        (directory / name).write_text(values[name])


def test_no_environment_marker_can_replace_kernel_evidence(monkeypatch):
    monkeypatch.setenv("LOCALSETUP_TEST_PROTECTED", "1")
    monkeypatch.setattr(safety, "cgroup_directory", lambda: Path("/unprotected"))
    with pytest.raises(RuntimeError, match="outside"):
        safety.verified_limits()


@pytest.mark.parametrize("available,cap,reserve,expected", [
    (16, 95, 512, 16320875724), (16, 75, 512, 12 * 1024**3), (8, 95, 2048, 6 * 1024**3),
])
def test_physical_capacity_and_other_workloads_override_requested_limit(
        tmp_path, monkeypatch, available, cap, reserve, expected):
    gib = 1024**3
    monkeypatch.setattr(safety, "testing_configuration", lambda _: {
        "memory_limit_mib": 32 * 1024, "physical_memory_percent": cap,
        "host_reserve_percent": 1, "host_reserve_mib": reserve})
    monkeypatch.setattr(safety, "host_memory", lambda: {"MemTotal": 16 * gib, "MemAvailable": available * gib})
    monkeypatch.setattr(safety, "cgroup_directory", lambda: tmp_path)
    assert safety.resource_budget(tmp_path)["memory_bytes"] == expected


@pytest.mark.parametrize("setting", ["memory_percent = 51", "memory_percent = true",
                                      "tasks_max = 0", "worker_memory_mib = 1"])
def test_unsafe_resource_settings_are_rejected(tmp_path, setting):
    (tmp_path / "pyproject.toml").write_text("[tool.localsetup.testing]\n" + setting)
    with pytest.raises(ValueError):
        read_testing_configuration(tmp_path)


def test_inside_failure_never_executes_workload(monkeypatch):
    monkeypatch.setattr(safety.sys, "argv", ["runner", "--inside", "unsafe-command"])
    def missing():
        raise RuntimeError("missing kernel controls")
    monkeypatch.setattr(safety, "verified_limits", missing)
    monkeypatch.setattr(safety.os, "execvp", lambda *_: pytest.fail("workload started"))
    assert safety.main() == 4


def test_budget_binds_selected_project_and_respects_parent_headroom(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[tool.localsetup.testing]\nmemory_percent=25\ntasks_max=64\n")
    group = tmp_path / "group"
    group.mkdir()
    (group / "memory.max").write_text(str(4 * 1024**3))
    (group / "memory.current").write_text(str(1024**3))
    (group / "cpu.max").write_text("200000 100000")
    monkeypatch.setattr(safety, "cgroup_directory", lambda: group)
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        "MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n"
                        if p == Path("/proc/meminfo") else original(p, *a, **k))
    budget = safety.resource_budget(project)
    assert budget == {"memory_bytes": 768 * 1024**2, "cpu_quota_percent": 66, "tasks": 64}
    (group / "memory.current").write_text(str(3 * 1024**3))
    with pytest.raises(RuntimeError, match="insufficient"):
        safety.resource_budget(project)


@pytest.mark.parametrize("active", [True, False])
def test_leftover_scope_refuses_dispatch_and_interrupt_stops_owned_scope(tmp_path, monkeypatch, active):
    monkeypatch.setattr(safety, "testing_configuration", lambda _: {})
    monkeypatch.setattr(safety, "resolved_test_workers", lambda **_: 1)
    class FakeTelemetry:
        def __init__(self, *args, **kwargs):
            self.data = {"state": "starting"}
        def finish(self, **kwargs):
            self.data["state"] = "interrupted"
        def cleaned(self):
            pass
    monkeypatch.setattr(safety, "Telemetry", FakeTelemetry)
    def unprotected():
        raise RuntimeError("unprotected")
    monkeypatch.setattr(safety, "verified_limits", unprotected)
    original_open = safety.os.open
    monkeypatch.setattr(safety.os, "open", lambda _path, flags, mode:
                        original_open(tmp_path / "isolated-lock", flags, mode))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout="stopping.scope" if active else "")
    monkeypatch.setattr(safety.subprocess, "run", run)
    monkeypatch.setattr(safety, "resource_budget", lambda _root:
                        {"memory_bytes": 67108864, "cpu_quota_percent": 50, "tasks": 32})
    def interrupt(argv):
        calls.append(argv)
        raise KeyboardInterrupt
    monkeypatch.setattr(safety.subprocess, "Popen", interrupt)
    if active:
        with pytest.raises(RuntimeError, match="prior test scope"):
            safety.run_protected(["workload"])
        assert len(calls) == 1
    else:
        with pytest.raises(KeyboardInterrupt):
            safety.run_protected(["workload"])
        assert calls[-1][:3] == ["systemctl", "--user", "stop"]
        unit = calls[-1][3]
        assert f"--unit={unit}" in calls[1]
    assert "--state=active,activating,deactivating,reloading" in calls[0]
