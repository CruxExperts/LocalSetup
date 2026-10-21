from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from ls.core.docs_artifacts.writers import write_workflow_registry
from ls.core.manifests import load_pack_config, load_platforms
from ls.core.paths import PathValidationError
from ls.core.skills import validate_skill_catalog
from ls.core.workflows import validate_workflow_catalog
from ls.tests.manifest_test_helpers import ROOT, make_workflow_validation_repo


def test_skill_frontmatter_schema_rejects_unknown_field(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    shutil.copy2(ROOT / "ls" / "config" / "skill-frontmatter.schema.json", root / "ls" / "config" / "skill-frontmatter.schema.json")
    skill_md = root / "ls" / "skills" / "ls-localsetup-project-context" / "SKILL.md"
    skill_md.write_text("---\nname: ls-localsetup-project-context\ndescription: Context.\nunknown: true\n---\n", encoding="utf-8")

    issues = validate_skill_catalog(root)

    assert any("frontmatter schema validation failed" in issue and "unknown" in issue for issue in issues)


def test_workflow_schema_rejects_unknown_field(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    shutil.copy2(ROOT / "ls" / "config" / "workflow.schema.json", root / "ls" / "config" / "workflow.schema.json")
    workflow_path = root / "ls" / "workflows" / "ls-workflow-demo" / "workflow.yaml"
    workflow_path.write_text(workflow_path.read_text(encoding="utf-8") + "\nunknown: true\n", encoding="utf-8")

    issues = validate_workflow_catalog(root)

    assert any("workflow.yaml schema validation failed" in issue and "unknown" in issue for issue in issues)


def test_workflow_catalog_rejects_alias_collision(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    manifest = root / "ls" / "workflows" / "ls-workflow-demo" / "workflow.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("aliases: [demo flow]", "aliases: [ls-localsetup-project-context]"), encoding="utf-8")

    issues = validate_workflow_catalog(root)
    assert any("workflow alias conflicts" in issue for issue in issues)


def test_workflow_catalog_rejects_unsafe_required_paths(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    manifest = root / "ls" / "workflows" / "ls-workflow-demo" / "workflow.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace("required_tools: []", "required_tools: [/bin/sh, 'C:\\\\Windows\\\\cmd.exe']")
        .replace("  - ls/docs/README.md", "  - /etc/passwd\n  - ../escape.md\n  - ~/.ssh/config"),
        encoding="utf-8",
    )

    issues = validate_workflow_catalog(root)
    assert sum("workflow requires unsafe tool path" in issue for issue in issues) == 2
    assert sum("workflow requires unsafe doc path" in issue for issue in issues) == 3


def test_workflow_catalog_accepts_localsetup_resolver_tokens(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    tools = root / "ls" / "tools"
    tools.mkdir(parents=True)
    (tools / "tmux_ops").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    docs = root / "ls" / "docs" / "ops"
    docs.mkdir(parents=True)
    (docs / "tmux-ops-managed.md").write_text("# Managed\n", encoding="utf-8")
    manifest = root / "ls" / "workflows" / "ls-workflow-demo" / "workflow.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace("required_tools: []", "required_tools: [localsetup://tool/tmux_ops]")
        .replace("  - ls/docs/README.md", "  - localsetup://doc/ops/tmux-ops-managed.md")
        .replace("migration: {}", "migration:\n  source: localsetup://doc/ops/tmux-ops-managed.md"),
        encoding="utf-8",
    )

    issues = validate_workflow_catalog(root)

    assert issues == []


def test_workflow_registry_renders_resolver_doc_tokens_as_local_links(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW_REGISTRY.md"
    workflow = {
        "id": "tmux",
        "package": "ls-wf-sudo-tmux-session",
        "name": "Sudo Tmux Session",
        "description": "Run tmux.",
        "aliases": [],
        "required_skills": [],
        "required_docs": ["localsetup://doc/ops/tmux-ops-managed.md"],
        "required_tools": ["localsetup://tool/tmux_ops"],
    }

    write_workflow_registry(path, "4.1", [workflow], tmp_path)
    text = path.read_text(encoding="utf-8")

    assert "../../localsetup://doc" not in text
    assert "[tmux-ops-managed.md](ops/tmux-ops-managed.md)" in text


def test_tmux_ops_workflow_trigger_metadata_covers_elevated_execution() -> None:
    workflow_dir = ROOT / "ls" / "workflows" / "ls-wf-sudo-tmux-session"
    skill_text = (workflow_dir / "SKILL.md").read_text(encoding="utf-8").lower()
    manifest_path = workflow_dir / "workflow.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest_text = yaml.safe_dump(manifest, sort_keys=True).lower()
    trigger_surface = f"{skill_text}\n{manifest_text}"

    for term in ("sudo", "elevated", "password prompt", "require_escalated", "tmux_ops run", "pseudo"):
        assert term in trigger_surface

    assert manifest["workflow_id"] == "sudo-tmux-session"
    assert manifest["display_name"] == "Sudo Tmux Session"
    assert "ls-workflow-ops-tmux-session" in manifest["aliases"]
    assert "ops-tmux-session" in manifest["aliases"]

    assert manifest["required_skills"] == ["ls-safety-and-backup"]
    assert manifest["required_tools"] == ["ls/tools/tmux_ops"]
    assert manifest["required_docs"] == [
        "ls/docs/ops/tmux-ops-managed.md",
        "ls/docs/ops/tmux-ops-remote.md",
    ]
    assert manifest["smoke"] == [{"id": "tmux_ops_exists", "check": "ls/tools/tmux_ops exists"}]
    assert manifest["migration"]["source"] == "ls/docs/ops/tmux-ops-managed.md"
    assert "localsetup://" not in manifest_path.read_text(encoding="utf-8")

    assert [gate["id"] for gate in manifest["gates"]] == ["execution_authorization", "probe_gate"]
    authorization_rule = manifest["gates"][0]["rule"].lower()
    for term in (
        "before every run",
        "ls-wf-risky-operations-safety-approval",
        "ls-safety-and-backup",
        "risk classification",
        "backup or no-backup decision",
        "rollback",
        "exact command or edit",
        "values",
        "target",
        "consequences",
        "affected scope",
        "immediate explicit user approval",
        "still matches",
        "changed records",
    ):
        assert term in authorization_rule

    assert "tmux is only the elevated or pty transport" in skill_text
    assert "sudo ready" in skill_text
    assert "do not authorize a command" in skill_text
    for term in (
        "<hostname.upper()>_sudo_key",
        "generic_sudo_key",
        "credential_outcome",
        "credential_variable",
        "credential_candidates",
        "cached",
        "hostname dots and hyphens",
        "never forwards a local ssh environment value",
        "ambiguous",
        "policy/mfa failure",
    ):
        assert term in skill_text
    assert "raw tmux send-keys" not in trigger_surface
    assert "tmux send-keys" not in trigger_surface


def test_ops_guarded_workflow_enforces_safety_and_handoff_contract() -> None:
    workflow_dir = ROOT / "ls" / "workflows" / "ls-wf-risky-operations-safety-approval"
    skill_text = (workflow_dir / "SKILL.md").read_text(encoding="utf-8").lower()
    manifest = yaml.safe_load((workflow_dir / "workflow.yaml").read_text(encoding="utf-8"))

    assert manifest["required_skills"] == ["ls-framework-compliance", "ls-safety-and-backup"]
    assert manifest["required_docs"] == [
        "ls/skills/ls-safety-and-backup/SKILL.md",
        "ls/workflows/ls-wf-sudo-tmux-session/SKILL.md",
    ]

    gates = manifest["gates"]
    assert [gate["id"] for gate in gates] == [
        "risk_classification",
        "backup_and_rollback_review",
        "freeze_approval_payload",
        "explicit_pre_execution_approval",
        "tmux_handoff_preconditions",
    ]
    gate_rules = "\n".join(gate["rule"] for gate in gates).lower()
    for required_term in (
        "risk class",
        "backup",
        "rollback",
        "exact command or edit",
        "values",
        "target",
        "explicit user approval",
        "only after every prior gate passes",
        "ls-wf-sudo-tmux-session",
    ):
        assert required_term in gate_rules

    package_text = f"{skill_text}\n{yaml.safe_dump(manifest, sort_keys=True).lower()}"
    assert "workflow_registry" not in package_text
    assert "generated workflow registry" not in package_text
    assert "sudo ready" in skill_text
    assert "frozen approval payload still matches" in skill_text


def test_repo_convert_workflow_requires_report_evidence_before_apply() -> None:
    skill_path = ROOT / "ls" / "workflows" / "ls-wf-repository-localsetup-conversion" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8").lower()
    backup_assignment = 'backup_dir="$target_root/.localsetup/backups/conversion-$(date -u +%y%m%dt%h%m%sz)"'
    report_command = (
        'localsetup convert --target-directory "$target_root" --backup-dir "$backup_dir" '
        "--tools codex --packs core"
    )
    apply_command = f"{report_command} --yes"

    assert skill_text.count(report_command) == 2
    assert skill_text.count(apply_command) == 1
    assert skill_text.count('--backup-dir "$backup_dir"') == 2
    assert skill_text.index(backup_assignment) < skill_text.index(report_command)
    assert skill_text.index(report_command) < skill_text.index("blockers are empty")
    assert skill_text.index("blockers are empty") < skill_text.index(apply_command)
    assert "every conversion starts with a report-only run" in skill_text
    assert "do not add `--yes` to this first command" in skill_text
    assert "unmanaged or ambiguous content is a blocker" in skill_text
    assert "source checkout and target repository root are the intended roots" in skill_text
    assert "the same timestamped `backup_dir`" in skill_text
    assert "conversion-report.json" in skill_text
    assert "only after that evidence is explicit" in skill_text


def test_server_triage_patch_workflow_is_plan_only() -> None:
    workflow_dir = ROOT / "ls" / "workflows" / "ls-wf-server-triage-patch-planning"
    skill_text = (workflow_dir / "SKILL.md").read_text(encoding="utf-8").lower()
    manifest = yaml.safe_load((workflow_dir / "workflow.yaml").read_text(encoding="utf-8"))

    assert manifest["required_skills"] == [
        "ls-system-info",
        "ls-linux-service-triage",
        "ls-linux-patcher",
    ]
    assert manifest["required_tools"] == []
    assert manifest["required_docs"] == ["ls/docs/WORKFLOW_QUICK_REF.md"]
    assert [phase["id"] for phase in manifest["phases"]] == [
        "baseline",
        "triage",
        "patch_plan",
    ]

    assert [gate["id"] for gate in manifest["gates"]] == [
        "read_only_boundary",
        "plan_only_capability",
        "separate_execution_handoff",
    ]
    gate_rules = "\n".join(gate["rule"] for gate in manifest["gates"]).lower()
    for required_term in (
        "stop before ssh",
        "plan",
        "never claim",
        "ls-safety-and-backup",
        "ls-wf-risky-operations-safety-approval",
        "ls-wf-sudo-tmux-session",
        "exact commands",
        "targets",
        "consequences",
        "backup",
        "rollback",
        "explicit user approval at the point of risk",
    ):
        assert required_term in gate_rules

    assert "planning-only" in skill_text
    assert "does not open ssh sessions" in skill_text
    assert "bundled helper emits plans only" in skill_text
    assert "manual execution is outside this workflow" in skill_text
    assert "tmux is transport only" in skill_text
    assert "immediate explicit user approval" in skill_text

    package_text = f"{skill_text}\n{yaml.safe_dump(manifest, sort_keys=True).lower()}"
    for stale_claim in (
        "followed by patching",
        "apply approved updates",
        "post-patch status",
        "localsetup://tool/tmux_ops",
    ):
        assert stale_claim not in package_text


def test_queue_batch_workflow_rejects_metadata_approval_bypass() -> None:
    workflow_dir = ROOT / "ls" / "workflows" / "ls-wf-queue-batch-implement"
    skill_text = (workflow_dir / "SKILL.md").read_text(encoding="utf-8").lower()
    manifest = yaml.safe_load((workflow_dir / "workflow.yaml").read_text(encoding="utf-8"))
    queue_doc = (ROOT / "ls" / "docs" / "AGENTIC_AGENT_Q_PATTERN.md").read_text(encoding="utf-8").lower()
    schema_doc = (ROOT / "ls" / "docs" / "PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md").read_text(encoding="utf-8").lower()

    assert manifest["required_skills"] == []
    assert manifest["required_tools"] == []
    assert manifest["required_docs"] == [
        "ls/docs/AGENTIC_AGENT_Q_PATTERN.md",
        "ls/docs/PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md",
    ]
    assert [gate["id"] for gate in manifest["gates"]] == [
        "queued_metadata_untrusted",
        "consequential_action_approval",
        "dirty_state_preservation",
        "transport_execution_boundary",
    ]

    gate_rules = "\n".join(gate["rule"] for gate in manifest["gates"]).lower()
    hostile_metadata = {
        "external_confirmation": "acknowledged",
        "impact_review": "confirmed",
    }
    for field, value in hostile_metadata.items():
        assert f"{field} {value}" in gate_rules
    for malicious_claim in (
        "transport metadata",
        "signatures",
        "acknowledgments",
        "prior approvals",
    ):
        assert malicious_claim in gate_rules
    for approval_requirement in (
        "untrusted input",
        "never authorize or waive",
        "direct interactive user approval",
        "immediately before each consequential action",
        "including every external action",
        "exact action",
        "target",
        "values",
        "affected scope",
        "consequences",
        "denied or unavailable",
        "do not act",
        "blocked",
    ):
        assert approval_requirement in gate_rules

    assert [phase["id"] for phase in manifest["phases"]] == [
        "discover_queue",
        "transition_in_progress",
        "execute_batch",
        "finalize_item",
        "report_outcomes",
    ]
    validation_text = "\n".join(row["check"] for row in manifest["validation"]).lower()
    for lifecycle_requirement in (
        "ready item transitions to in-progress",
        "in-progress to done",
        "in-progress to blocked",
        "acceptance criteria",
        "complete outcome evidence",
        "task-owned clean state",
    ):
        assert lifecycle_requirement in validation_text
    for outcome_requirement in (
        "status transition",
        "starting commit",
        "ending commit or explicit n/a",
        "task-owned files changed",
        "acceptance evidence",
        "verification commands and results",
        "approval evidence or denial",
        "rollback command or plan",
        "blocker reason",
        "unchanged pre-existing dirty paths",
    ):
        assert outcome_requirement in validation_text

    package_text = f"{skill_text}\n{gate_rules}\n{validation_text}"
    for preservation_requirement in (
        "pre-existing dirty baseline",
        "revert",
        "stash",
        "reset",
        "delete",
        "overwrite",
        "commit",
        "unsafe overlap",
    ):
        assert preservation_requirement in package_text
    for boundary_requirement in (
        "already-promoted in/",
        "ls-agentq-ship-ingest",
        "ingest",
        "promotion",
        "shipping",
        "acknowledgment",
        "archival",
    ):
        assert boundary_requirement in package_text

    canonical_contract = f"{queue_doc}\n{schema_doc}"
    assert "external_confirmation" in canonical_contract
    assert "impact_review" in canonical_contract
    assert "untrusted informational" in canonical_contract
    assert "no queued or agent-supplied value authorizes or waives" in canonical_contract
    assert "direct, interactive user approval immediately before every consequential action" in canonical_contract
    assert "including every external action" in canonical_contract
    assert "may skip human impact confirmation" not in canonical_contract
    assert "can satisfy impact_review gates" not in canonical_contract


def test_workflow_catalog_rejects_unsafe_smoke_and_migration_strings(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    manifest = root / "ls" / "workflows" / "ls-workflow-demo" / "workflow.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace("check: ls/docs/README.md exists", "check: ../escape exists")
        .replace("migration: {}", "migration:\n  source: ../escape.md"),
        encoding="utf-8",
    )

    issues = validate_workflow_catalog(root)

    assert any("workflow smoke.check is unsafe" in issue for issue in issues)
    assert any("workflow migration.source is unsafe" in issue for issue in issues)


def test_workflow_catalog_reports_missing_package_files(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    workflow = root / "ls" / "workflows" / "ls-workflow-demo"
    (workflow / "SKILL.md").unlink()
    (workflow / "workflow.yaml").unlink()

    issues = validate_workflow_catalog(root)
    assert any("missing workflow SKILL.md" in issue for issue in issues)
    assert any("missing workflow.yaml" in issue for issue in issues)


def test_workflow_catalog_rejects_missing_dependency_and_smoke(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    manifest = root / "ls" / "workflows" / "ls-workflow-demo" / "workflow.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace("required_skills: []", "required_skills: [ls-missing]")
        .replace("smoke:\n  - id: docs\n    check: ls/docs/README.md exists", "smoke: []"),
        encoding="utf-8",
    )

    issues = validate_workflow_catalog(root)
    assert any("workflow requires missing skill" in issue for issue in issues)
    assert any("workflow missing smoke row" in issue for issue in issues)


def test_workflow_catalog_rejects_duplicate_id_and_bad_package_name(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    workflow2 = root / "ls" / "workflows" / "ls-workflow-wrong"
    workflow2.mkdir()
    (workflow2 / "SKILL.md").write_text(
        "---\nname: ls-workflow-wrong\ndescription: Wrong workflow.\n---\n",
        encoding="utf-8",
    )
    (workflow2 / "workflow.yaml").write_text(
        (root / "ls" / "workflows" / "ls-workflow-demo" / "workflow.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    issues = validate_workflow_catalog(root)
    assert any("duplicate workflow_id" in issue for issue in issues)
    assert any("workflow package/id mismatch" in issue for issue in issues)


def test_workflow_catalog_rejects_alias_that_matches_later_workflow_id(tmp_path: Path) -> None:
    root = make_workflow_validation_repo(tmp_path)
    manifest = root / "ls" / "workflows" / "ls-workflow-demo" / "workflow.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("aliases: [demo flow]", "aliases: [zeta]"),
        encoding="utf-8",
    )
    workflow2 = root / "ls" / "workflows" / "ls-workflow-zeta"
    workflow2.mkdir()
    (workflow2 / "SKILL.md").write_text(
        "---\nname: ls-workflow-zeta\ndescription: Zeta workflow.\n---\n",
        encoding="utf-8",
    )
    (workflow2 / "workflow.yaml").write_text(
        """
workflow_id: zeta
display_name: Zeta
aliases: [zeta flow]
invocation: Zeta only.
required_skills: []
required_tools: []
required_docs:
  - ls/docs/README.md
gates: []
phases: []
validation: []
outputs:
  - Zeta output
smoke:
  - id: docs
    check: ls/docs/README.md exists
migration: {}
""",
        encoding="utf-8",
    )

    issues = validate_workflow_catalog(root)
    assert any("workflow alias conflicts with reserved name" in issue for issue in issues)


def test_pack_manifest_rejects_absolute_public_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    config = root / "ls" / "config"
    config.mkdir(parents=True)
    (config / "pack.yaml").write_text(
        """
pack_id: localsetup
namespace: ls
version: 3
global:
  home: ~/.local/share/localsetup
  package_root: ~/.local/share/localsetup/packages
  registry: ~/.local/share/localsetup/registry.json
repo:
  lockfile: .localsetup/lock.json
packs: {}
public_private:
  public_paths:
    - /etc/passwd
  private_paths: []
""",
        encoding="utf-8",
    )

    with pytest.raises(PathValidationError):
        load_pack_config(root)


@pytest.mark.parametrize(
    "bad_path",
    [
        "C:/Users/example/localsetup",
        "C:\\Users\\example\\localsetup",
        "~user/localsetup",
        "~/../escape",
        "safe\\..\\escape",
        "safe/..\\escape",
    ],
)
def test_pack_manifest_rejects_unsafe_public_paths(tmp_path: Path, bad_path: str) -> None:
    root = tmp_path / "repo"
    config = root / "ls" / "config"
    config.mkdir(parents=True)
    (config / "pack.yaml").write_text(
        f"""
pack_id: localsetup
namespace: ls
version: 3
global:
  home: ~/.local/share/localsetup
  package_root: ~/.local/share/localsetup/packages
  registry: ~/.local/share/localsetup/registry.json
repo:
  lockfile: .localsetup/lock.json
packs: {{}}
public_private:
  public_paths:
    - '{bad_path}'
  private_paths: []
""",
        encoding="utf-8",
    )

    with pytest.raises(PathValidationError):
        load_pack_config(root)


def test_platform_manifest_rejects_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    config = root / "ls" / "config"
    config.mkdir(parents=True)
    (config / "platforms.yaml").write_text(
        """
platforms:
  - id: codex
    repo_paths: ["../escape"]
    global_paths: ["~/.codex/skills"]
    verify_rules: []
    rollback_targets: [".codex/skills"]
""",
        encoding="utf-8",
    )

    with pytest.raises(PathValidationError):
        load_platforms(root)


@pytest.mark.parametrize(
    "bad_home",
    [
        "C:/Users/example/.codex/skills",
        "C:\\Users\\example\\.codex\\skills",
        "~user/.codex/skills",
        "~/../.codex/skills",
        "~/.codex\\..\\escape",
    ],
)
def test_platform_manifest_rejects_unsafe_home_paths(tmp_path: Path, bad_home: str) -> None:
    root = tmp_path / "repo"
    config = root / "ls" / "config"
    config.mkdir(parents=True)
    (config / "platforms.yaml").write_text(
        f"""
platforms:
  - id: codex
    repo_paths: [".codex/skills"]
    global_paths: ['{bad_home}']
    verify_rules: []
    rollback_targets: [".codex/skills"]
""",
        encoding="utf-8",
    )

    with pytest.raises(PathValidationError):
        load_platforms(root)
