"""CI partitions preserve every collected case exactly once."""
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess

import pytest
import yaml

import ls.core.test_plugin as shard_hooks
from ls.core.test_plugin import pytest_collection_modifyitems


@pytest.mark.parametrize("version,accepted", [("tmux 3.4", True), ("tmux 3.6a", False), ("", False)])
def test_native_shards_reject_unqualified_tmux_before_pytest(tmp_path, version, accepted):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/pr-validation.yml").read_text())
    job = workflow["jobs"]["framework-validation"]
    assert job["runs-on"] == "ubuntu-24.04"
    names = [step["name"] for step in job["steps"]]
    guard = names.index("Verify qualified tmux runtime")
    assert guard < names.index("Run pytest")
    command = tmp_path / "tmux"
    command.write_text('#!/bin/sh\nprintf "%s\\n" "$FIXTURE_TMUX_VERSION"\n')
    command.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", job["steps"][guard]["run"]], capture_output=True, timeout=5,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
             "FIXTURE_TMUX_VERSION": version},
    )
    assert (result.returncode == 0) is accepted


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


class Node:
    def __init__(self, nodeid):
        self.nodeid = nodeid
        self.marks = []

    def add_marker(self, mark):
        self.marks.append(mark)


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
    nodes = [Node(f"ls/tests/test_example.py::test_case[{i}]") for i in range(4010)]
    seen = []
    for shard in range(12):
        selected, excluded = partition(nodes, shard, 12)
        assert selected
        assert len(selected) + len(excluded) == len(nodes)
        assert {n.nodeid for n in selected}.isdisjoint(n.nodeid for n in excluded)
        assert all(not node.marks for node in selected)
        reversed_selection, _ = partition(list(reversed(nodes)), shard, 12)
        assert {n.nodeid for n in selected} == {n.nodeid for n in reversed_selection}
        seen.extend(n.nodeid for n in selected)
    assert len(seen) == len(set(seen)) == len(nodes)
    assert set(seen) == {n.nodeid for n in nodes}


def test_default_collection_is_unchanged():
    nodes = [Node("test.py::test_example")]
    assert partition(nodes, 0, 1) == (nodes, [])


def test_configured_local_pytest_uses_shared_budget_and_group_queue(monkeypatch):
    seen = []
    monkeypatch.setattr(shard_hooks, "resolved_test_workers", lambda override, project_root: (
        seen.append((override, project_root)) or (4 if override is None else override)))
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=None, dist="load"),
                             getoption=lambda name: {"ci_shards": 1}[name])
    shard_hooks._configure_test_workers(config)
    assert (config.option.numprocesses, config.option.dist) == (4, "load")
    assert seen == [(None, Path.cwd())]
    config.option.numprocesses = 2
    shard_hooks._configure_test_workers(config)
    assert config.option.numprocesses == 2
    config.getoption = lambda name: {"ci_shards": 12}[name]
    shard_hooks._configure_test_workers(config)
    assert config.option.numprocesses == 1
    monkeypatch.setattr(shard_hooks, "testing_configuration",
                        lambda _root: {"workers": 7, "shard_count": 1})
    config.getoption = lambda name: {"ci_shards": 1}[name]
    config.option.numprocesses = None
    shard_hooks._configure_test_workers(config)
    assert config.option.numprocesses == 4


def test_xdist_children_cannot_start_nested_worker_pools(monkeypatch):
    config = SimpleNamespace(workerinput={"workerid": "gw0"},
                             option=SimpleNamespace(numprocesses=None, dist="no"))
    monkeypatch.setattr(shard_hooks, "resolved_test_workers",
                        lambda *_args, **_kwargs: pytest.fail("worker tried to resolve a new pool"))
    shard_hooks._configure_test_workers(config)
    assert config.option.numprocesses is None
    assert config.option.dist == "no"


@pytest.mark.parametrize("requested", [None, "auto", "logical"])
def test_automatic_workers_preserve_interactive_debugging(requested):
    config = SimpleNamespace(option=SimpleNamespace(
        numprocesses=requested, dist="load", usepdb=True))
    shard_hooks._configure_test_workers(config)
    assert (config.option.numprocesses, config.option.dist) == (0, "no")


def test_ci_workflow_uses_project_policy_for_shards_and_aggregate_parallelism():
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" /
                "pr-validation.yml").read_text(encoding="utf-8")
    assert "test_policy: ${{ steps.test-policy.outputs.json }}" in workflow
    assert "max-parallel: ${{ fromJSON(needs.framework-prerequisites.outputs.test_policy).max_parallel }}" in workflow
    assert "shard: ${{ fromJSON(needs.framework-prerequisites.outputs.test_policy).shard_indices }}" in workflow
    assert "pytest -n 1 ls/tests" in workflow


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


@pytest.mark.parametrize("shard,count", [(-1, 12), (12, 12), (0, 0), (0, 8), (0, 33)])
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
