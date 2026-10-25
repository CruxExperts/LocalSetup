from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

import ls.core.test_workers as test_workers_mod
from ls.core.path_contract import paths_manifest_issues, paths_manifest_path
from ls.core.path_reprocessor import reprocess_localsetup_paths
from ls.core.repair import run_repair
from ls.core.test_workers import (
    default_test_workers,
    effective_max_test_workers,
    resolved_test_workers,
    test_workers_payload as workers_payload,
)
from ls.tests.test_install_flow import make_temp_repo

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def test_context_skill_source_commands_resolve_checkout_root() -> None:
    text = (SOURCE_ROOT / "ls" / "skills" / "ls-localsetup-project-context" / "SKILL.md").read_text(encoding="utf-8")
    generated_section = text.split("## Generated Docs And Volatile Facts", 1)[1].split(
        "## Validation Expectations", 1
    )[0]
    validation_section = text.split("## Validation Expectations", 1)[1].split("## Invariants", 1)[0]
    source_root_assignment = 'source_root="$(localsetup path source-root)"'
    enter_source_root = 'cd -- "$source_root"'

    for section in (generated_section, validation_section):
        assert section.index("set -euo pipefail") < section.index(source_root_assignment)
        assert section.index(source_root_assignment) < section.index("uv run --locked")
        assert section.index(enter_source_root) < section.index("uv run --locked")

    assert text.count("(\nset -euo pipefail\n" + source_root_assignment) == 2
    assert validation_section.index("localsetup context --markdown") < validation_section.index(source_root_assignment)
    assert text.count(source_root_assignment) == 2
    assert text.count(enter_source_root) == 2
    assert "follows ls/docs/PYTHON_ARCHITECTURE_STANDARD.md" not in text
    assert "follows `localsetup://doc/PYTHON_ARCHITECTURE_STANDARD.md`" in text


def test_localsetup_path_cli_writes_json_manifest_and_resolves_paths(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home with space"
    tool = root / "ls" / "tools" / "localsetup.py"

    completed = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "path", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    manifest = paths_manifest_path(home)
    assert payload["source_root"] == str(root.resolve())
    assert payload["paths"]["docs-root"] == str(root / "ls" / "docs")
    assert payload["paths"]["tools-root"] == str(root / "ls" / "tools")
    assert payload["paths"]["package-root"] == str(home / ".local" / "share" / "localsetup" / "packages")
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["paths"] == payload["paths"]

    doc = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "path", "doc", "ops/tmux-ops-managed.md"],
        text=True,
        capture_output=True,
        check=False,
    )
    package = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "path", "package", "ls-localsetup-project-context", "SKILL.md"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert doc.returncode == 0, doc.stderr
    assert doc.stdout.strip() == str(root / "ls" / "docs" / "ops" / "tmux-ops-managed.md")
    assert package.returncode == 0, package.stderr
    assert package.stdout.strip() == str(home / ".local" / "share" / "localsetup" / "packages" / "ls-localsetup-project-context" / "SKILL.md")


def test_doctor_reports_and_repair_refreshes_stale_paths_manifest(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    manifest = paths_manifest_path(home)
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"schema_version": 1, "source_root": "/old"}\n', encoding="utf-8")

    report = run_repair(root, home=home, target_root=target, repair_mode="report-only")

    assert report["applied"] is False
    assert report["resolver"]["ok"] is False
    assert any(action["kind"] == "refresh_paths_manifest" for action in report["actions"])
    assert json.loads(manifest.read_text(encoding="utf-8"))["source_root"] == "/old"

    applied = run_repair(root, home=home, target_root=target, repair_mode="safe-repair", apply=True)

    assert applied["ok"] is True
    assert applied["applied"] is True
    assert applied["resolver"]["ok"] is True
    assert applied["resolver"]["issues"] == []
    assert paths_manifest_issues(root, home) == []


def test_test_worker_defaults_and_overrides_are_clamped() -> None:
    assert default_test_workers(1) == 1
    assert default_test_workers(3) == 1
    assert default_test_workers(5) == 1
    assert default_test_workers(6) == 2
    assert default_test_workers(512) == 170
    assert effective_max_test_workers(512) == 512
    assert resolved_test_workers("-10") == 1
    assert resolved_test_workers("999", cpu_count=512) == 512
    assert resolved_test_workers("999", cpu_count=512, env={"LOCALSETUP_TEST_WORKERS": "5"}) == 512
    assert resolved_test_workers(None, cpu_count=512, env={}) == 170
    assert resolved_test_workers(None, cpu_count=8, env={"LOCALSETUP_TEST_WORKERS": "5"}) == 5
    assert resolved_test_workers("8", cpu_count=2) == 2
    assert resolved_test_workers("8", cpu_count=3) == 3
    assert resolved_test_workers(None, cpu_count=2, env={"LOCALSETUP_TEST_WORKERS": "8"}) == 2


def test_test_workers_uses_process_cpu_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_workers_mod.os, "process_cpu_count", lambda: None, raising=False)
    monkeypatch.setattr(test_workers_mod.os, "sched_getaffinity", lambda _pid: {2, 3, 4, 5, 6}, raising=False)

    assert test_workers_mod.available_cpu_count() == 5
    assert default_test_workers() == 1


def test_test_worker_project_policy_and_override_precedence(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.localsetup.testing]\nworkers = 7\nshard_count = 12\n', encoding="utf-8")
    assert resolved_test_workers(cpu_count=9, env={}, project_root=tmp_path) == 7
    assert resolved_test_workers(cpu_count=5, env={}, project_root=tmp_path) == 5
    assert resolved_test_workers(cpu_count=9, env={"LOCALSETUP_TEST_WORKERS": "4"}, project_root=tmp_path) == 4
    assert resolved_test_workers("8", cpu_count=9, env={"LOCALSETUP_TEST_WORKERS": "4"},
                                 project_root=tmp_path) == 8
    payload = workers_payload(cpu_count=9, env={}, project_root=tmp_path)
    assert (payload["workers_per_shard"], payload["max_parallel"], payload["shard_count"]) == (1, 7, 12)
    assert payload["worker_source"] == "project_configuration"
    assert workers_payload(cpu_count=9, env={"LOCALSETUP_TEST_WORKERS": "3"},
                           project_root=tmp_path)["worker_source"] == "environment"
    for document in ('[tool.localsetup.testing]\nworkers = 0\n',
                     '[tool.localsetup.testing]\nworkers = "fast"\n',
                     '[tool.localsetup.testing]\nshard_count = 0\n',
                     '[tool]\nlocalsetup = "invalid"\n'):
        (tmp_path / "pyproject.toml").write_text(document, encoding="utf-8")
        with pytest.raises(ValueError):
            workers_payload(cpu_count=9, env={}, project_root=tmp_path)


def test_test_workers_cli_outputs_json_and_rejects_bad_override(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    tool = root / "ls" / "tools" / "localsetup.py"

    completed = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "test-workers", "--workers", "999", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    bad = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "test-workers", "--workers", "many"],
        text=True,
        capture_output=True,
        check=False,
    )

    env_completed = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "test-workers", "--json"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "LOCALSETUP_TEST_WORKERS": "999"},
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["workers"] == payload["max_workers"]
    assert env_completed.returncode == 0, env_completed.stderr
    env_payload = json.loads(env_completed.stdout)
    assert env_payload["workers"] == env_payload["max_workers"]
    assert bad.returncode == 2
    assert "must be an integer" in bad.stderr

    assert workers_payload(cpu_count=4)["max_workers"] == 4
    assert workers_payload(cpu_count=6)["max_workers"] == 6
    assert workers_payload(cpu_count=6, env={})["workers"] == 2
    assert workers_payload(cpu_count=6, env={})["shard_indices"] == list(range(12))
    assert workers_payload(cpu_count=6, env={})["max_parallel"] == 2


def test_test_workers_cli_reads_invocation_project_not_source_root(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.localsetup.testing]\nworkers = 1\nshard_count = 5\n', encoding="utf-8")
    tool = SOURCE_ROOT / "ls" / "tools" / "localsetup.py"
    completed = subprocess.run(
        [sys.executable, str(tool), "--source-root", str(SOURCE_ROOT), "test-workers", "--json"],
        cwd=tmp_path, text=True, capture_output=True, check=False,
        env={key: value for key, value in os.environ.items() if key != "LOCALSETUP_TEST_WORKERS"},
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["configured_workers"] == 1
    assert payload["shard_indices"] == list(range(5))


def test_reprocess_paths_apply_is_not_exposed_until_allowlisted(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    tool = root / "ls" / "tools" / "localsetup.py"
    tracked_file = root / "README.md"
    before = tracked_file.read_text(encoding="utf-8")

    report = reprocess_localsetup_paths(root, apply=False)
    completed = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "reprocess-paths", "--apply"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert report["ok"] is True
    assert completed.returncode == 2
    assert "disabled until allowlisted rewrites" in completed.stderr
    assert tracked_file.read_text(encoding="utf-8") == before


def test_full_pytest_procedure_surfaces_use_hardened_worker_helper() -> None:
    surfaces = [
        SOURCE_ROOT / "AGENTS.md",
        SOURCE_ROOT / "CONTRIBUTING.md",
        SOURCE_ROOT / ".github" / "pull_request_template.md",
        SOURCE_ROOT / ".github" / "workflows" / "pr-validation.yml",
        SOURCE_ROOT / ".github" / "workflows" / "publish.yml",
        SOURCE_ROOT / "ls" / "README.md",
        SOURCE_ROOT / "ls" / "core" / "context.py",
        SOURCE_ROOT / "ls" / "docs" / "COMMAND_REFERENCE.md",
        SOURCE_ROOT / "ls" / "docs" / "REPO_MAINTENANCE.md",
        SOURCE_ROOT / "ls" / "skills" / "ls-localsetup-project-context" / "SKILL.md",
        SOURCE_ROOT / "ls" / "skills" / "ls-localsetup-framework-changes" / "SKILL.md",
        SOURCE_ROOT / "ls" / "templates" / "codex" / "AGENTS.md",
    ]

    for surface in surfaces:
        text = surface.read_text(encoding="utf-8")
        assert "pytest -n auto" not in text, surface
        assert "pytest -n 8" not in text, surface
    assert "test-workers" in (SOURCE_ROOT / ".github" / "workflows" / "pr-validation.yml").read_text(encoding="utf-8")
    assert "test-workers" in (SOURCE_ROOT / "ls" / "core" / "context.py").read_text(encoding="utf-8")
