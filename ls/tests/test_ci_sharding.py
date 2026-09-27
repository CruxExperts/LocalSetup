"""CI partitions preserve every collected case exactly once."""
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from ls.tests.conftest import pytest_collection_modifyitems


def partition(nodes, shard, count):
    excluded = []
    config = SimpleNamespace(
        getoption=lambda name: {"ci_shard": shard, "ci_shards": count}[name],
        hook=SimpleNamespace(pytest_deselected=lambda items: excluded.extend(items)),
    )
    selected = list(nodes)
    pytest_collection_modifyitems(config, selected)
    return selected, excluded


def test_shards_cover_each_case_once_independent_of_collection_order():
    nodes = [SimpleNamespace(nodeid=f"ls/tests/test_example.py::test_case[{i}]") for i in range(4010)]
    seen = []
    for shard in range(8):
        selected, excluded = partition(nodes, shard, 8)
        assert selected
        assert len(selected) + len(excluded) == len(nodes)
        assert {n.nodeid for n in selected}.isdisjoint(n.nodeid for n in excluded)
        reversed_selection, _ = partition(list(reversed(nodes)), shard, 8)
        assert {n.nodeid for n in selected} == {n.nodeid for n in reversed_selection}
        seen.extend(n.nodeid for n in selected)
    assert len(seen) == len(set(seen)) == len(nodes)
    assert set(seen) == {n.nodeid for n in nodes}


def test_default_collection_is_unchanged():
    nodes = [SimpleNamespace(nodeid="test.py::test_example")]
    assert partition(nodes, 0, 1) == (nodes, [])


@pytest.mark.parametrize("shard,count", [(-1, 8), (8, 8), (0, 0), (0, 33)])
def test_invalid_partition_fails(shard, count):
    with pytest.raises(pytest.UsageError):
        partition([], shard, count)


@pytest.mark.parametrize("prerequisites,reused,shards,accepted", [
    ("success", "false", "success", True),
    ("success", "true", "skipped", True),
    ("success", "false", "skipped", False),
    ("success", "false", "failure", False),
    ("success", "false", "cancelled", False),
    ("failure", "true", "skipped", False),
])
def test_aggregate_cannot_accept_missing_or_failed_shards(prerequisites, reused, shards, accepted):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/pr-validation.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["framework-validation"]["strategy"]["matrix"]["shard"] == list(range(8))
    assert jobs["framework-validation"]["needs"] == "framework-prerequisites"
    assert jobs["framework-prerequisites"]["needs"] == ["generated-docs-and-version", "shell-smoke-and-audit", "documentation", "quality"]
    script = jobs["framework-result"]["steps"][0]["run"]
    result = subprocess.run(["bash", "-c", script], env={**os.environ,
        "PREREQUISITES": prerequisites, "REUSED": reused, "SHARDS": shards}, timeout=5)
    assert (result.returncode == 0) is accepted
