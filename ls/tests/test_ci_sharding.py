"""CI partitions preserve every collected case exactly once."""
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from ls.tests.conftest import pytest_collection_modifyitems


def test_dispatch_version_check_preserves_release_anchor_history(tmp_path):
    """A dispatch without event base fields must retain the older release anchor."""
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("commit", "--allow-empty", "-m", "release anchor")
    anchor = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "published main")
    main = git("rev-parse", "HEAD")
    # A file transport keeps fetch semantics identical to a hosted remote.
    git("remote", "add", "origin", tmp_path.as_uri())
    git("update-ref", "refs/remotes/origin/main", main)
    git("checkout", "-b", "candidate")
    git("commit", "--allow-empty", "-m", "maintenance candidate")
    head = git("rev-parse", "HEAD")
    commands = tmp_path / "commands"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text(
        '#!/bin/sh\n'
        'git merge-base --is-ancestor "$RELEASE_ANCHOR" HEAD || exit 1\n'
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
    )
    uv.chmod(0o755)
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/pr-validation.yml").read_text())
    step = next(step for step in workflow["jobs"]["generated-docs-and-version"]["steps"]
                if step["name"] == "Check version and generated docs")
    log = tmp_path / "uv-commands.log"
    result = subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, text=True,
        capture_output=True, timeout=15, env={**os.environ,
            "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
            "PR_BASE_SHA": "", "MERGE_GROUP_BASE_SHA": "", "BEFORE_SHA": "",
            "HEAD_SHA": head, "RELEASE_ANCHOR": anchor, "COMMAND_LOG": str(log)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert git("rev-parse", "--is-shallow-repository") == "false"
    assert f"Checking version plan from {main} to {head}" in result.stdout
    assert len(log.read_text().splitlines()) == 2
    assert all(f"--base {main} --head {head}" in line for line in log.read_text().splitlines())


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


@pytest.mark.parametrize("cancelled,prerequisites,reused,run", [
    (False, "success", "false", True),
    (True, "success", "false", False),
    (False, "failure", "false", False),
    (False, "skipped", "false", False),
    (False, "cancelled", "false", False),
    (False, "", "false", False),
    (False, "success", "true", False),
])
def test_shards_override_implicit_status_after_optional_jobs_are_skipped(
    cancelled, prerequisites, reused, run
):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/pr-validation.yml").read_text())
    condition = workflow["jobs"]["framework-validation"]["if"]
    # An explicit status function prevents skipped optional ancestors from
    # suppressing the matrix; prerequisite failure and prior proof still gate it.
    assert "!cancelled()" in condition
    assert "needs.framework-prerequisites.result == 'success'" in condition
    assert "needs.framework-prerequisites.outputs.reuse != 'true'" in condition
    expression = condition.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("!cancelled()", repr(not cancelled))
    expression = expression.replace("needs.framework-prerequisites.result", repr(prerequisites))
    expression = expression.replace("needs.framework-prerequisites.outputs.reuse", repr(reused))
    assert eval("(" + expression.replace("&&", "and") + ")", {"__builtins__": {}}) is run


@pytest.mark.parametrize("shard,count", [(-1, 8), (8, 8), (0, 0), (0, 33)])
def test_invalid_partition_fails(shard, count):
    with pytest.raises(pytest.UsageError):
        partition([], shard, count)


@pytest.mark.parametrize("event,dependency", [
    ("pull_request", "success"), ("merge_group", "success"),
    ("push", "skipped"), ("workflow_dispatch", "skipped"),
])
@pytest.mark.parametrize("prerequisites,reused,shards,accepted", [
    ("success", "false", "success", True),
    ("success", "true", "skipped", True),
    ("success", "false", "skipped", False),
    ("success", "false", "failure", False),
    ("success", "false", "cancelled", False),
    ("failure", "true", "skipped", False),
    ("success", "false", "", False),
    ("success", "true", "", False),
    ("", "true", "skipped", False),
    ("cancelled", "false", "success", False),
])
def test_aggregate_cannot_accept_missing_or_failed_shards(event, dependency, prerequisites, reused, shards, accepted):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/pr-validation.yml").read_text())
    jobs = workflow["jobs"]
    script = jobs["framework-result"]["steps"][0]["run"]
    result = subprocess.run(["bash", "-c", script], env={**os.environ,
        "EVENT_NAME": event, "DEPENDENCY_REVIEW": dependency,
        "PREREQUISITES": prerequisites, "REUSED": reused, "SHARDS": shards}, timeout=5)
    assert (result.returncode == 0) is accepted


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "push", "workflow_dispatch", "unknown", ""])
@pytest.mark.parametrize("dependency", ["success", "failure", "cancelled", "skipped", "", "unknown"])
@pytest.mark.parametrize("reused,shards", [("false", "success"), ("true", "skipped")])
def test_aggregate_dependency_review_is_required_even_with_reused_suite(event, dependency, reused, shards):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/pr-validation.yml").read_text())
    script = workflow["jobs"]["framework-result"]["steps"][0]["run"]
    accepted = (
        event in {"pull_request", "merge_group"} and dependency == "success"
        or event in {"push", "workflow_dispatch"} and dependency == "skipped"
    )
    result = subprocess.run(["bash", "-c", script], env={**os.environ,
        "EVENT_NAME": event, "DEPENDENCY_REVIEW": dependency,
        "PREREQUISITES": "success", "REUSED": reused, "SHARDS": shards}, timeout=5)
    assert (result.returncode == 0) is accepted
