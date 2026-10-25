import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ls.core.test_workers import resolved_test_workers, testing_configuration


def pytest_addoption(parser):
    group = parser.getgroup("LocalSetup CI")
    group.addoption("--ci-shard", type=int, default=0, help="Zero-based CI shard index")
    group.addoption("--ci-shards", type=int, default=1, help="Number of isolated CI shards")


def _configure_test_workers(config):
    """Use the project's shared test budget and queue its stable shard groups."""
    if hasattr(config, "workerinput"):
        # xdist invokes pytest_cmdline_main in every child. Re-enabling xdist
        # there recursively multiplies workers until the host is exhausted.
        return
    try:
        configured = testing_configuration(ROOT)
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
        workers = resolved_test_workers(override, project_root=ROOT)
        config.option.numprocesses = min(workers, configured["shard_count"])
        if config.getoption("ci_shards") > 1:
            config.option.numprocesses = 1
        config.option.dist = "loadgroup"
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
            [str(ROOT), *filter(None, env.get("PYTHONPATH", "").split(os.pathsep))])
        os.execve(sys.executable, [sys.executable, "-m", "ls.core.test_resources",
                                  "--project-root", str(ROOT), "--",
                                  sys.executable, "-m", "pytest", *config.invocation_params.args], env)
    # xdist creates its worker transports in this hook, before pytest_configure.
    _configure_test_workers(config)
    configured = testing_configuration(ROOT) or {}
    reserve = configured.get("worker_memory_mib", 1024) * 1024 * 1024
    if hasattr(config.option, "numprocesses") and isinstance(config.option.numprocesses, int):
        config.option.numprocesses = min(config.option.numprocesses,
                                        max(1, verified_limits()["memory_bytes"] // reserve))
    return (yield)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Partition individual cases, including parameters, without dropping coverage."""
    shard, count = config.getoption("ci_shard"), config.getoption("ci_shards")
    if not 1 <= count <= 32 or not 0 <= shard < count:
        raise pytest.UsageError("require 1 <= --ci-shards <= 32 and 0 <= --ci-shard < --ci-shards")
    try:
        configured = testing_configuration(ROOT)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc
    groups = configured["shard_count"] if configured is not None else count
    if count > 1 and configured is not None and count != groups:
        raise pytest.UsageError("--ci-shards must match [tool.localsetup.testing].shard_count")
    selected, excluded = [], []
    for item in items:
        original_nodeid = item.nodeid
        bucket = int.from_bytes(hashlib.sha256(original_nodeid.encode()).digest(), "big") % groups
        if configured is not None and groups > 1:
            item.add_marker(pytest.mark.xdist_group(f"shard-{bucket:02d}"))
        (selected if count == 1 or bucket == shard else excluded).append(item)
    items[:] = selected
    if excluded:
        config.hook.pytest_deselected(items=excluded)
    # An empty shard must fail with pytest's normal no-tests exit code.


@pytest.fixture
def synthetic_runtime_interpreter(tmp_path, monkeypatch):
    """Supply owned bytes for inventory unit tests; never execute this file."""
    from ls.core.agent import runtime_integrity
    interpreter = tmp_path / 'synthetic-python'
    interpreter.write_bytes(b'synthetic interpreter inventory fixture')
    interpreter.chmod(0o700)
    monkeypatch.setattr(runtime_integrity, 'sys', SimpleNamespace(
        executable=str(interpreter), version_info=sys.version_info))
    return interpreter


@pytest.fixture
def default_opencode_environment(monkeypatch):
    """Select default discovery roots only for tests that request this fixture."""
    for name in ('OPENCODE_TEST_HOME', 'OPENCODE_CONFIG_DIR',
                 'OPENCODE_DISABLE_EXTERNAL_SKILLS', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(name, raising=False)
