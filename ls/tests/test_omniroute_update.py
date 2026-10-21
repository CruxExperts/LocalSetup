from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ls" / "skills" / "ls-omniroute-skills-update" / "scripts" / "omniroute_update.py"


def load_converter():
    spec = importlib.util.spec_from_file_location("omniroute_update_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_skill(path: Path, name: str, *, extensions: dict | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    frontmatter: dict = {
        "name": name,
        "description": f"{name} fixture.",
        "metadata": {"version": "1.0"},
    }
    if extensions is not None:
        frontmatter["extensions"] = extensions
    (path / "SKILL.md").write_text("---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n# Fixture\n", encoding="utf-8")


def make_local_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "ls" / "skills").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    (repo / "ls" / "config" / "pack.yaml").write_text(
        """
pack_id: localsetup
namespace: ls
version: 3
global:
  package_root: ~/.local/share/localsetup/packages
  registry: ~/.local/share/localsetup/registry.json
repo:
  lockfile: .localsetup/lock.json
optional_packs: []
packs: {}
workflow_packs: {}
distribution_channels: []
public_private:
  public_paths: []
  private_paths: []
extensions:
  skill_taxonomy:
    ls-omniroute-model-discovery:
      class: integrations
      sort_priority: 50
      tags: [omniroute, proxy]
      owner_scope: skill
""",
        encoding="utf-8",
    )
    return repo


def test_upstream_to_local_name_mapping() -> None:
    converter = load_converter()

    assert converter.upstream_to_local_name("cli-chat") == "ls-cli-chat"
    assert converter.upstream_to_local_name("omni-auth") == "ls-omni-auth"


def test_default_ref_is_pinned_to_validated_source_commit() -> None:
    converter = load_converter()

    assert converter.DEFAULT_REF == "5458026c216f77a3da68ea49152dc33470cfe2cb"


def test_boundary_coverage_owners_match_operator_workflow() -> None:
    converter = load_converter()

    assert converter.NATIVE_COVERAGE["config-codex-cli"] == [
        "ls-omniroute-task-triage",
        "ls-omniroute-admin-automation",
    ]
    assert converter.NATIVE_COVERAGE["omni-version-manager"] == [
        "ls-omniroute-admin-automation",
        "ls-omniroute-task-triage",
    ]
    assert "cli-skill-collector" not in converter.NATIVE_COVERAGE
    assert "ponytail" not in converter.NATIVE_COVERAGE


def test_reads_upstream_skill_manifests_from_local_fixture(tmp_path: Path) -> None:
    converter = load_converter()
    source = tmp_path / "OmniRoute"
    write_skill(source / "skills" / "omniroute", "omniroute")
    write_skill(source / "skills" / "cli-chat", "cli-chat")

    skills, metadata = converter.read_upstream_skills(
        source_path=source,
        source_repo="https://example.invalid/OmniRoute.git",
        ref="main",
        timeout=5,
    )

    assert [skill.name for skill in skills] == ["cli-chat", "omniroute"]
    assert skills[0].path == "skills/cli-chat/SKILL.md"
    assert len(skills[0].sha256) == 64
    assert metadata["skills_root"].endswith("OmniRoute/skills")


def test_reads_local_frontmatter_omniroute_extension(tmp_path: Path) -> None:
    converter = load_converter()
    repo = make_local_repo(tmp_path)
    write_skill(
        repo / "ls" / "skills" / "ls-cli-chat",
        "ls-cli-chat",
        extensions={
            "omniroute": {
                "source_skill": "cli-chat",
                "source_sha256": "abc",
                "source_commit": "def",
            }
        },
    )

    [local] = converter.read_local_skills(repo)

    assert local.name == "ls-cli-chat"
    assert local.omniroute["source_skill"] == "cli-chat"


def test_classifies_missing_current_stale_local_only_and_untracked(tmp_path: Path) -> None:
    converter = load_converter()
    current_hash = "a" * 64
    stale_hash = "b" * 64
    upstream = [
        converter.UpstreamSkill("cli-a2a", "skills/cli-a2a/SKILL.md", current_hash, "commit1", "2026-05-24T00:00:00Z"),
        converter.UpstreamSkill("cli-chat", "skills/cli-chat/SKILL.md", current_hash, None, None),
        converter.UpstreamSkill("cli-tools", "skills/cli-tools/SKILL.md", stale_hash, "commit2", "2026-05-24T00:00:00Z"),
    ]
    local = [
        converter.LocalSkill(
            "ls-cli-chat",
            "ls/skills/ls-cli-chat/SKILL.md",
            {},
            {"source_skill": "cli-chat", "source_sha256": current_hash},
            ["omniroute"],
        ),
        converter.LocalSkill(
            "ls-cli-tools",
            "ls/skills/ls-omniroute-tools/SKILL.md",
            {},
            {"source_skill": "cli-tools", "source_sha256": "c" * 64, "source_commit": "commit1"},
            ["omniroute"],
        ),
        converter.LocalSkill(
            "ls-cli-removed",
            "ls/skills/ls-omniroute-removed/SKILL.md",
            {},
            {"source_skill": "cli-removed", "source_sha256": "d" * 64},
            ["omniroute"],
        ),
        converter.LocalSkill("ls-omniroute-model-discovery", "ls/skills/ls-omniroute-model-discovery/SKILL.md", {}, {}, ["omniroute"]),
    ]

    rows = converter.classify(upstream, local)
    statuses = {(row.status, row.upstream_skill, row.local_skill) for row in rows}

    assert ("missing-local", "cli-a2a", None) in statuses
    assert ("current", "cli-chat", "ls-cli-chat") in statuses
    assert ("stale-local", "cli-tools", "ls-cli-tools") in statuses
    assert ("local-only", "cli-removed", "ls-cli-removed") in statuses
    assert ("untracked-local", None, "ls-omniroute-model-discovery") in statuses


def test_classifies_consolidated_native_coverage() -> None:
    converter = load_converter()
    current_hash = "a" * 64
    upstream = [
        converter.UpstreamSkill("cli-chat", "skills/cli-chat/SKILL.md", current_hash, "commit1", "2026-07-04T00:00:00Z"),
        converter.UpstreamSkill("omni-cache", "skills/omni-cache/SKILL.md", current_hash, "commit1", "2026-07-04T00:00:00Z"),
        converter.UpstreamSkill("omni-github-skills", "skills/omni-github-skills/SKILL.md", current_hash, "commit1", "2026-07-04T00:00:00Z"),
    ]
    local = [
        converter.LocalSkill(
            "ls-omniroute-model-discovery",
            "ls/skills/ls-omniroute-model-discovery/SKILL.md",
            {},
            {"source_kind": "localsetup-native", "local_role": "proxy-discovery"},
            ["omniroute"],
        ),
        converter.LocalSkill(
            "ls-omniroute-admin-automation",
            "ls/skills/ls-omniroute-admin-automation/SKILL.md",
            {},
            {"source_kind": "localsetup-native", "local_role": "admin-automation"},
            ["omniroute"],
        ),
    ]

    rows = converter.classify(upstream, local)
    statuses = {(row.status, row.upstream_skill, row.local_skill) for row in rows}

    assert ("covered-native", "cli-chat", "ls-omniroute-model-discovery") in statuses
    assert ("covered-native", "omni-cache", "ls-omniroute-model-discovery, ls-omniroute-admin-automation") in statuses
    assert ("covered-native", "omni-github-skills", "ls-omniroute-model-discovery, ls-omniroute-admin-automation") in statuses


def test_classifies_localsetup_native_omniroute_skills() -> None:
    converter = load_converter()
    local = [
        converter.LocalSkill(
            "ls-omniroute-model-discovery",
            "ls/skills/ls-omniroute-model-discovery/SKILL.md",
            {},
            {"source_kind": "localsetup-native", "local_role": "proxy-discovery"},
            ["omniroute"],
        ),
    ]

    rows = converter.classify([], local)

    assert len(rows) == 1
    assert rows[0].status == "local-native"
    assert rows[0].local_skill == "ls-omniroute-model-discovery"
    assert "proxy-discovery" in rows[0].detail


def test_json_and_markdown_report_output(tmp_path: Path) -> None:
    converter = load_converter()
    repo = make_local_repo(tmp_path)
    source = tmp_path / "OmniRoute"
    write_skill(source / "skills" / "omniroute", "omniroute")
    write_skill(repo / "ls" / "skills" / "ls-omniroute-model-discovery", "ls-omniroute-model-discovery")

    args = Namespace(
        repo_root=str(repo),
        source_path=str(source),
        source_repo="https://example.invalid/OmniRoute.git",
        ref="main",
        timeout=5,
        include_regex=None,
        output="json",
        out=None,
    )
    report = converter.build_report(args)
    json_text = json.dumps(report)
    markdown = converter.render_markdown(report)

    assert '"missing-local"' in json_text
    assert "| missing-local | omniroute |" in markdown
    assert "| untracked-local |  | ls-omniroute-model-discovery |" in markdown


def test_freshness_summary_defaults_to_converted_skill_blockers_only() -> None:
    converter = load_converter()
    rows = [
        converter.ReportRow("missing-local", "cli-a2a", None, "ls-cli-a2a", "skills/cli-a2a/SKILL.md", "missing"),
        converter.ReportRow("untracked-local", None, "ls-omniroute-model-discovery", None, None, "untracked"),
        converter.ReportRow("covered-native", "omni-cache", "ls-omniroute-model-discovery", "ls-omniroute-model-discovery", "skills/omni-cache/SKILL.md", "covered"),
        converter.ReportRow("current", "cli-chat", "ls-cli-chat", "ls-cli-chat", "skills/cli-chat/SKILL.md", "ok"),
    ]

    summary = converter.freshness_summary(rows)

    assert summary["fresh"] is True
    assert summary["blocking_count"] == 0


def test_freshness_summary_can_require_full_and_strict_validation() -> None:
    converter = load_converter()
    rows = [
        converter.ReportRow("missing-local", "cli-a2a", None, "ls-cli-a2a", "skills/cli-a2a/SKILL.md", "missing"),
        converter.ReportRow("untracked-local", None, "ls-omniroute-model-discovery", None, None, "untracked"),
    ]

    summary = converter.freshness_summary(rows, require_all_upstream=True, strict_untracked=True)

    assert summary["fresh"] is False
    assert summary["blocking_count"] == 2
    assert summary["blocking_statuses"] == ["local-only", "missing-local", "stale-local", "untracked-local"]


def test_freshness_command_exit_codes_for_policy(tmp_path: Path) -> None:
    converter = load_converter()
    repo = make_local_repo(tmp_path)
    source = tmp_path / "OmniRoute"
    write_skill(source / "skills" / "omniroute", "omniroute")
    write_skill(repo / "ls" / "skills" / "ls-omniroute-model-discovery", "ls-omniroute-model-discovery")
    base_args = [
        "freshness",
        "--repo-root",
        str(repo),
        "--source-path",
        str(source),
        "--source-repo",
        "https://example.invalid/OmniRoute.git",
        "--output",
        "json",
    ]

    assert converter.main(base_args) == 0
    assert converter.main([*base_args, "--require-all-upstream"]) == 1
    assert converter.main([*base_args, "--strict-untracked"]) == 1
