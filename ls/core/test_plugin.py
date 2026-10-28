"""Reusable pytest integration for LocalSetup's guarded adaptive test policy."""
import json
import os
from pathlib import Path
import sys

import pytest

FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]


def project_root(config):
    return Path(getattr(config, "rootpath", Path.cwd())).resolve()


from ls.core.test_workers import resolved_test_workers, testing_configuration


def pytest_addoption(parser):
    group = parser.getgroup("LocalSetup CI")
    group.addoption("--ci-shard", type=int, default=0, help="Zero-based CI shard index")
    group.addoption("--ci-shards", type=int, default=1, help="Number of isolated CI shards")
    group.addoption("--test-seed", type=int, default=None, help="Reproduce a recorded shuffled test order")
    group.addoption("--no-test-shuffle", action="store_true", help="Preserve collection order within each partition")


def ordering(config):
    if hasattr(config, "_localsetup_ordering"):
        return config._localsetup_ordering
    if "localsetup_ordering" in getattr(config, "workerinput", {}):
        result = config.workerinput["localsetup_ordering"]
    else:
        from .test_ordering import select_seed
        settings = testing_configuration(project_root(config)) or {}
        option = getattr(config, "option", None)
        seed, source = select_seed(settings, explicit=getattr(option, "test_seed", None),
                                   sharded=config.getoption("ci_shards") > 1)
        result = {"version": 1, "seed": seed, "source": source,
                  "shuffle": settings.get("shuffle", True) and not getattr(option, "no_test_shuffle", False)}
    config._localsetup_ordering = result
    return result


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    ordering(config)
    from .test_progress import ProgressReporter
    config.pluginmanager.register(ProgressReporter(config), "localsetup_test_progress")


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    node.workerinput["localsetup_ordering"] = ordering(node.config)


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    from .process_identity import set_process_name
    set_process_name()
    if not hasattr(session.config, "workerinput"):
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        message = "LocalSetup test order: " + json.dumps(ordering(session.config), sort_keys=True)
        reporter.write_line(message) if reporter is not None else print(message, flush=True)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtestloop(session):
    from .test_output import TestOutput
    from .test_telemetry import controller_binding
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    display = None
    if reporter is not None and controller_binding() is not None:
        settings = testing_configuration(project_root(session.config)) or {}
        display = TestOutput(reporter, project_root(session.config), settings.get("telemetry_interval_seconds", 5))
        display.start()
    try:
        return (yield)
    finally:
        if display is not None:
            display.close()


def _configure_test_workers(config):
    """Use the project's shared test budget and queue its stable shard groups."""
    if hasattr(config, "workerinput"):
        # xdist invokes pytest_cmdline_main in every child. Re-enabling xdist
        # there recursively multiplies workers until the host is exhausted.
        return
    try:
        configured = testing_configuration(project_root(config))
        if configured is None:
            return
        if not hasattr(config.option, "numprocesses"):
            raise pytest.UsageError("configured test scheduling requires pytest-xdist")
        if getattr(config.option, "collectonly", False):
            return
        requested = config.option.numprocesses
        if getattr(config.option, "usepdb", False) and requested in (None, "auto", "logical"):
            config.option.numprocesses = 0
            config.option.dist = "no"
            return
        if requested == 0:
            return
        override = requested if type(requested) is int else None
        workers = resolved_test_workers(override, project_root=project_root(config))
        config.option.numprocesses = workers
        config.option.dist = "load"
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_cmdline_main(config):
    from ls.core.test_resources import verified_limits
    try:
        verified_limits()
    except (OSError, ValueError, RuntimeError):
        # Re-exec before xdist creates workers. The launcher fails closed if
        # the kernel boundary cannot be established; serial runs are protected too.
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(FRAMEWORK_ROOT), *filter(None, env.get("PYTHONPATH", "").split(os.pathsep))])
        os.execve(sys.executable, [sys.executable, "-m", "ls.core.test_resources",
                                  "--project-root", str(project_root(config)), "--",
                                  sys.executable, "-m", "pytest", *config.invocation_params.args], env)
    # xdist creates its worker transports in this hook, before pytest_configure.
    _configure_test_workers(config)
    configured = testing_configuration(project_root(config)) or {}
    reserve = configured.get("worker_memory_mib", 1024) * 1024 * 1024
    if isinstance(getattr(config.option, "numprocesses", None), int) and config.option.numprocesses > 1:
        from .test_pressure import AdaptivePolicy, sample_pressure
        from .test_resources import cgroup_directory
        sample = sample_pressure(cgroup_directory(), configured)
        policy = AdaptivePolicy.parse(configured.get("adaptive"))
        capacity = max(1, sample.window_bytes * policy.target_percent // 100 // reserve)
        requested = config.option.numprocesses
        config.option.numprocesses = min(requested, capacity)
        if config.option.numprocesses != requested:
            print("LocalSetup memory worker limit: " + json.dumps({
                "requested": requested, "effective": config.option.numprocesses,
                "window_bytes": sample.window_bytes, "worker_reserve_bytes": reserve}), flush=True)
    return (yield)


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_xdist_make_scheduler(config, log):
    from ls.core.test_scheduling_runtime import make_scheduler
    return make_scheduler(config, log, testing_configuration(project_root(config)) or {})


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    bridge = getattr(session.config, "_localsetup_adaptive_bridge", None)
    if bridge is not None:
        bridge.close()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Partition individual cases, including parameters, without dropping coverage."""
    shard, count = config.getoption("ci_shard"), config.getoption("ci_shards")
    if not 1 <= count <= 32 or not 0 <= shard < count:
        raise pytest.UsageError("require 1 <= --ci-shards <= 32 and 0 <= --ci-shard < --ci-shards")
    try:
        configured = testing_configuration(project_root(config))
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc
    groups = configured["shard_count"] if configured is not None else count
    if count > 1 and configured is not None and count != groups:
        raise pytest.UsageError("--ci-shards must match the resolved testing.shard_count")
    from .test_ordering import partition
    order = ordering(config)
    selected, excluded = partition(items, seed=order["seed"], shard=shard, count=count,
                                   shuffle=order["shuffle"])
    items[:] = selected
    if excluded:
        config.hook.pytest_deselected(items=excluded)
    # An empty shard must fail with pytest's normal no-tests exit code.
