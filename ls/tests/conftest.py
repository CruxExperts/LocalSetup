import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_addoption(parser):
    group = parser.getgroup("LocalSetup CI")
    group.addoption("--ci-shard", type=int, default=0, help="Zero-based CI shard index")
    group.addoption("--ci-shards", type=int, default=1, help="Number of isolated CI shards")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Partition individual cases, including parameters, without dropping coverage."""
    shard, count = config.getoption("ci_shard"), config.getoption("ci_shards")
    if not 1 <= count <= 32 or not 0 <= shard < count:
        raise pytest.UsageError("require 1 <= --ci-shards <= 32 and 0 <= --ci-shard < --ci-shards")
    if count == 1:
        return
    selected, excluded = [], []
    for item in items:
        bucket = int.from_bytes(hashlib.sha256(item.nodeid.encode()).digest(), "big") % count
        (selected if bucket == shard else excluded).append(item)
    items[:] = selected
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
