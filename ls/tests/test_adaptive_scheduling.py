"""Pressure debounce and queue ownership must not depend on timing luck."""
from types import SimpleNamespace

import pytest

from ls.core.test_pressure import AdaptivePolicy, PressureController
from ls.core.test_scheduling import AdaptiveScheduling


def test_pressure_requires_continuous_dwell_and_cooldown():
    controller = PressureController(4, AdaptivePolicy())
    for now, pressure in [(0, 81), (4, 79), (5, 81), (9, 81)]:
        assert controller.observe(pressure, now).workers == 4
    assert controller.observe(81, 10).workers == 3
    assert controller.observe(81, 24).workers == 3
    assert controller.observe(81, 25).workers == 2
    assert controller.observe(60, 26).workers == 2
    assert controller.observe(71, 55).workers == 2
    assert controller.observe(60, 56).workers == 2
    assert controller.observe(60, 85).workers == 2
    assert controller.observe(60, 86).workers == 3
    assert controller.observe(60, 115).workers == 3
    assert controller.observe(60, 116).workers == 4


def test_critical_pressure_latches_failure_but_single_worker_bypasses():
    controller = PressureController(4, AdaptivePolicy())
    assert controller.observe(96, 0).workers == 1
    assert not controller.observe(96, 4).emergency
    assert controller.observe(96, 5).emergency
    assert controller.observe(0, 10).emergency
    serial = PressureController(1, AdaptivePolicy())
    assert not serial.observe(100, 0).emergency
    assert not serial.observe(100, 100).emergency
    assert serial.last_sample is None


def test_dynamically_reduced_single_worker_still_recovers():
    controller = PressureController(2, AdaptivePolicy())
    controller.observe(81, 0)
    assert controller.observe(81, 5).workers == 1
    controller.observe(60, 6)
    assert controller.observe(60, 36).workers == 2


class Worker:
    def __init__(self):
        self.gateway = SimpleNamespace(id="fixture")
        self.shutting_down = False
        self.sent = []
        self.workeroutput = {"exitstatus": 0}

    def send_runtest_some(self, indices):
        self.sent.extend(indices)

    def shutdown(self):
        self.shutting_down = True


def scheduler(count=3, cases=31):
    config = SimpleNamespace(getvalue=lambda name: [f"{count}*popen"],
                             getoption=lambda name: 2)
    result = AdaptiveScheduling(config)
    nodes = [Worker() for _ in range(count)]
    collection = [f"test_{i}" for i in range(cases)]
    for node in nodes:
        result.add_node(node)
        result.add_node_collection(node, collection)
    result.schedule()
    return result, nodes, collection


def assert_conserved(result):
    locations = [*result.completed, *result.unresolved, *result.unassigned,
                 *(i for queue in result.queues.values() for i in queue),
                 *(i for queue in result.node2pending.values() for i in queue)]
    assert sorted(locations) == list(range(len(result.collection)))
    assert len(locations) == len(set(locations))
    assert all(len(queue) <= 2 for queue in result.node2pending.values())


def test_shrink_drain_growth_balances_only_unstarted_work():
    result, nodes, collection = scheduler()
    assigned = {n: list(result.node2pending[n]) for n in nodes}
    result.set_target(2)
    assert nodes[2].shutting_down
    assert not result.queues[nodes[2]]
    assert result.node2pending == assigned
    assert abs(len(result.queues[nodes[0]]) - len(result.queues[nodes[1]])) <= 1
    assert_conserved(result)
    for index in assigned[nodes[2]]:
        result.mark_test_complete(nodes[2], index)
    assert result.remove_node(nodes[2]) is None
    assert_conserved(result)
    result.set_target(3)
    new = Worker()
    result.add_node(new)
    result.add_node_collection(new, collection)
    result.schedule()
    assert new.sent
    sizes = [len(result.queues[n]) for n in result.eligible()]
    assert max(sizes) - min(sizes) <= 1
    assert_conserved(result)
    while any(result.node2pending.values()):
        for node in list(result.nodes):
            if result.node2pending[node]:
                result.mark_test_complete(node, result.node2pending[node][0])
                assert_conserved(result)
    assert result.tests_finished
    assert result.completed == set(range(len(collection)))


def test_singleton_and_replacement_collection_mismatch():
    result, nodes, collection = scheduler(cases=1)
    assert nodes[0].shutting_down
    assert nodes[0].sent == [0]
    result.mark_test_complete(nodes[0], 0)
    assert result.tests_finished
    new = Worker()
    result.add_node(new)
    with pytest.raises(RuntimeError, match="collection differs"):
        result.add_node_collection(new, ["changed"])


def test_crashed_assignment_remains_accounted_until_an_explicit_retry():
    result, nodes, collection = scheduler()
    crashed = nodes[0]
    failed_index = result.node2pending[crashed][0]
    crashed.workeroutput = {"exitstatus": 1}
    failed = result.remove_node(crashed)
    assert failed == collection[failed_index]
    assert result.unresolved == {failed_index}
    assert_conserved(result)
    result.mark_test_pending(failed)
    assert not result.unresolved
    assert_conserved(result)


@pytest.mark.parametrize("replace_initial", [False, True])
def test_initial_collection_defers_pressure_retirement(replace_initial):
    config = SimpleNamespace(getvalue=lambda name: ["2*popen"],
                             getoption=lambda name: 2)
    result = AdaptiveScheduling(config)
    first, second = Worker(), Worker()
    result.add_node(first)
    result.add_node(second)
    collection = [f"test_{i}" for i in range(12)]
    result.add_node_collection(first, collection)
    result.set_target(1)
    if replace_initial:
        assert result.remove_node(second) is None
        second = Worker()
        result.add_node(second)
    assert not first.shutting_down
    assert not first.sent
    result.add_node_collection(second, collection)
    result.schedule()
    assert first.sent
    assert second.shutting_down
    assert_conserved(result)


def test_native_retirement_and_recovery_preserve_exact_coverage(tmp_path, request):
    """Run serially so the nested three-worker probe shares the aggregate budget."""
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from ls.core.test_workers import default_test_workers
    if hasattr(request.config, "workerinput") or default_test_workers() < 4:
        pytest.skip("run this bounded native probe with -n 0 and at least four worker slots")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.localsetup.testing]\nworkers=3\n'
        '[tool.localsetup.testing.adaptive]\ndown_dwell_seconds=1\n'
        'recovery_dwell_seconds=2\ncooldown_seconds=1\n'
        '[tool.pytest.ini_options]\naddopts=""\n')
    (tmp_path / "conftest.py").write_text('''
import time
from ls.core.test_pressure import PressureSample
def pytest_configure(config):
    if hasattr(config, "workerinput"):
        return
    import ls.core.test_scheduling_runtime as runtime
    started = time.monotonic()
    def pressure(*args):
        age = time.monotonic() - started
        used = 81 if 1 <= age < 4 else 10
        return PressureSample(used, used, 100, 100, 100, 0)
    runtime.sample_pressure = pressure
''')
    (tmp_path / "test_probe.py").write_text('''
import json, os, time
from pathlib import Path
import pytest
@pytest.mark.parametrize("case", range(180))
def test_case(case):
    time.sleep(.1)
    record = json.dumps([case, os.getpid(), Path("/proc/self/stat").read_text().split()[21]]) + "\\n"
    fd = os.open("coverage.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, record.encode())
    finally:
        os.close(fd)
''')
    env = dict(os.environ)
    env.pop("LOCALSETUP_TEST_WORKERS", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    completed = subprocess.run([sys.executable, "-m", "pytest", "-p", "ls.core.test_plugin", "-n", "3", "-q"],
                               cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"event": "memory_pressure"' in completed.stdout
    assert '"event": "sustained_memory_recovery"' in completed.stdout
    records = [json.loads(line) for line in (tmp_path / "coverage.jsonl").read_text().splitlines()]
    assert sorted(row[0] for row in records) == list(range(180))
    assert len({row[1] for row in records}) > 3
    for _, pid, start in records:
        stat = Path(f"/proc/{pid}/stat")
        assert not stat.exists() or stat.read_text().split()[21] != start
