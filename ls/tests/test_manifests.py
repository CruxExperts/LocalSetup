import argparse
import importlib.util
import json
import subprocess
import shutil
from pathlib import Path

import pytest
import yaml

from ls.core.baseline import classify_path
from ls.core.baseline import tracked_files
from ls.core.manifests import load_pack_config, load_platforms, validate_manifest_schemas
from ls.core.selection import resolve_package_selection
from ls.core.skill_index_scrub import audit as scrub_audit
from ls.core.skill_index_scrub.reporting import build_report
from ls.core.skills import ALLOWED_SKILL_TAXONOMY_CLASSES
from ls.core.skills import load_skill_catalog
from ls.core.skills import selected_skill_names
from ls.core.skills import skill_taxonomy_payload
from ls.core.skills import validate_skill_catalog
from ls.core.workflows import selected_workflow_names, validate_workflow_catalog
from ls.core.workflows import load_workflow_catalog


ROOT = Path(__file__).resolve().parents[2]


def test_dependency_pr_validation_exercises_manifest_inputs() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pr-validation.yml").read_text(encoding="utf-8")
    dependabot = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert "dependency-manifest-validation:" in workflow
    assert "github.actor == 'dependabot[bot]'" in workflow
    assert "uv lock --check" in workflow
    assert "uv sync --frozen --all-groups" in workflow
    assert "uv run --frozen --group s3-sdk pytest" in workflow
    publish = (
        ROOT / ".github/workflows/publish.yml"
    ).read_text(encoding="utf-8")
    assert "ci_evidence.py" in publish and "--require" in publish
    assert "--group s3-sdk pytest" not in publish
    assert "package-ecosystem: uv" in dependabot
    assert "dependency-name: PGPy" not in dependabot


def test_skill_index_scrub_can_prune_dead_urls(tmp_path: Path) -> None:
    module_path = ROOT / "ls" / "tools" / "skill_index_scrub.py"
    spec = importlib.util.spec_from_file_location("skill_index_scrub_under_test", module_path)
    assert spec is not None and spec.loader is not None
    scrub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scrub)

    index = tmp_path / "PUBLIC_SKILL_INDEX.yaml"
    index.write_text(
        """
schema_version: 2
updated: '2026-05-18T00:00:00Z'
skills:
- name: dead
  description: Removed upstream.
  url: https://example.invalid/dead
- name: transient
  description: Temporary upstream failure.
  url: https://example.invalid/transient
- name: network
  description: Temporary network failure.
  url: https://example.invalid/network
- name: fixable
  description: "Anthropic skill: placeholder."
  url: https://example.com/fixable
  summary_short: "Anthropic skill: placeholder."
  summary_long: "Anthropic skill: placeholder."
  quality_signals: {}
- name: no-license
  description: "Anthropic skill: No License"
  url: https://github.com/anthropics/skills/tree/main/skills/no-license
  source_registry: https://github.com/anthropics/skills/tree/main/skills
  summary_short: "Anthropic skill: No License"
  summary_long: "Anthropic skill: No License"
  quality_signals: {}
""",
        encoding="utf-8",
    )
    updated, pruned = scrub.apply_fixes(
        index,
        [
            {
                "name": "dead",
                "url": "https://example.invalid/dead",
                "url_live": False,
                "url_status": 404,
                "action": "dead_url",
            },
            {
                "name": "transient",
                "url": "https://example.invalid/transient",
                "url_live": False,
                "url_status": 503,
                "action": "dead_url",
            },
            {
                "name": "network",
                "url": "https://example.invalid/network",
                "url_live": False,
                "url_status": 0,
                "action": "dead_url",
            },
            {
                "name": "fixable",
                "url": "https://example.com/fixable",
                "url_live": True,
                "action": "fixable",
                "fetched_desc": "Real upstream description.",
            },
            {
                "name": "no-license",
                "url": "https://github.com/anthropics/skills/tree/main/skills/no-license",
                "source_registry": "https://github.com/anthropics/skills/tree/main/skills",
                "url_live": True,
                "action": "fixable",
                "fetched_desc": "Copied no-license upstream description.",
            },
        ],
        prune_dead_urls=True,
    )

    payload = yaml.safe_load(index.read_text(encoding="utf-8"))
    assert updated == 1
    assert pruned == 1
    assert [skill["name"] for skill in payload["skills"]] == ["transient", "network", "fixable", "no-license"]
    assert payload["skills"][2]["description"] == "Real upstream description."
    assert payload["skills"][3]["description"] == "Anthropic skill: No License"


def test_skill_index_scrub_report_distinguishes_skipped_url_checks() -> None:
    result = {
        "name": "clean", "url": "https://example.com/skill", "url_live": None,
        "url_status": None, "desc_stub": False, "desc_reason": "",
        "fetched_desc": None, "fetched_source": None, "action": "ok",
    }
    args = argparse.Namespace(skip_url_check=True, skip_desc_fetch=False, fix=False)
    report = build_report([result], args, "current", False)
    assert "URL check: skipped" in report
    assert "| Dead / unreachable URLs | not checked |" in report
    assert "URL liveness was not checked" in report
    assert "Index looks clean" not in report

    args.skip_url_check = False
    result["url_live"] = True
    report = build_report([result], args, "current", False)
    assert "URL check: enabled" in report
    assert "| Dead / unreachable URLs | 0 |" in report
    assert "description and URL-liveness checks passed" in report


def test_skill_index_scrub_report_does_not_pass_incomplete_checks() -> None:
    args = argparse.Namespace(skip_url_check=False, skip_desc_fetch=False, fix=False)
    result = {
        "name": "failed", "url": "https://example.com/skill", "url_live": None,
        "url_status": None, "desc_stub": False, "desc_reason": "",
        "fetched_desc": None, "fetched_source": None, "action": "error",
        "error": "probe failed",
    }
    report = build_report([result], args, "current", False)
    assert "## Worker Errors (1)" in report
    assert "All audited" not in report

    result["action"] = "ok"
    report = build_report([result], args, "current", False)
    assert "All audited" not in report


def test_skill_index_scrub_skip_url_check_avoids_liveness_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_probe(*args: object, **kwargs: object) -> tuple[bool, int]:
        raise AssertionError("URL liveness probe must not run")

    monkeypatch.setattr(scrub_audit, "check_url_liveness", fail_probe)
    result = scrub_audit.audit_skill(
        {"name": "skill", "url": "https://example.com/skill", "description": "A sufficiently descriptive skill entry."},
        skip_url_check=True,
        skip_desc_fetch=True,
    )
    assert result["url_live"] is None
    assert result["url_status"] is None


def test_skill_index_scrub_full_url_check_uses_liveness_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []

    def live_probe(url: str, **kwargs: object) -> tuple[bool, int]:
        observed.append(url)
        return True, 200

    monkeypatch.setattr(scrub_audit, "check_url_liveness", live_probe)
    result = scrub_audit.audit_skill(
        {"name": "skill", "url": "https://example.com/skill", "description": "A sufficiently descriptive skill entry."},
        skip_url_check=False,
        skip_desc_fetch=True,
    )
    assert observed == ["https://example.com/skill"]
    assert result["url_live"] is True
    assert result["url_status"] == 200


def test_pack_manifest_loads() -> None:
    root = Path(__file__).resolve().parents[2]
    pack = load_pack_config(root)
    assert pack.pack_id == "localsetup"
    assert pack.namespace == "ls"
    assert pack.lockfile == ".localsetup/lock.json"
    assert "core" in pack.packs
    assert "experimental" in pack.packs
    assert pack.selection_profiles["normal"]["packs"] == [
        "bootstrap",
        "core",
        "dev",
        "frontend",
        "architecture",
        "ops",
        "publishing",
    ]
    assert pack.selection_profiles["normal"]["description"]


def test_platform_manifest_has_supported_client_projections() -> None:
    root = Path(__file__).resolve().parents[2]
    platforms = load_platforms(root)
    ids = {p.platform_id for p in platforms}
    assert ids == {
        "codex", "claude-code", "cursor", "kilo", "opencode", "openclaw",
        "github-copilot-cli", "github-copilot-vscode", "cline-cli", "cline-vscode",
        "amp-cli", "goose-cli", "pi-cli", "hermes-agent", "qwen-code-cli",
        "kimi-cli", "factory-droid", "antigravity-app", "gemini-cli", "omp-cli",
    }
    by_id = {platform.platform_id: platform for platform in platforms}
    assert by_id["codex"].repo_paths == [".agents/skills"]
    assert by_id["codex"].global_paths == ["~/.agents/skills"]
    assert by_id["opencode"].global_paths == ["~/.agents/skills"]


def test_manifest_schemas_reject_unknown_pack_and_platform_fields(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "repo"
    shutil.copytree(source / "ls" / "config", root / "ls" / "config")
    pack_path = root / "ls" / "config" / "pack.yaml"
    platforms_path = root / "ls" / "config" / "platforms.yaml"
    pack_path.write_text(pack_path.read_text(encoding="utf-8") + "\nunknown_control_field: true\n", encoding="utf-8")
    platforms_path.write_text(
        platforms_path.read_text(encoding="utf-8").replace("    native_config: symlink_or_reference", "    native_config: symlink_or_reference\n    unknown_platform_field: true", 1),
        encoding="utf-8",
    )

    issues = validate_manifest_schemas(root)

    assert any("pack.yaml schema validation failed" in issue and "unknown_control_field" in issue for issue in issues)
    assert any("platforms.yaml schema validation failed" in issue and "unknown_platform_field" in issue for issue in issues)


def test_facts_json_aligns_with_live_version_and_catalogs() -> None:
    root = Path(__file__).resolve().parents[2]
    facts = json.loads((root / "ls" / "docs" / "_generated" / "facts.json").read_text(encoding="utf-8"))
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    platforms = load_platforms(root)
    platform_ids = [p.platform_id for p in platforms]
    skill_count = len(list((root / "ls" / "skills").glob("ls-*/SKILL.md")))
    workflow_count = len(load_workflow_catalog(root))

    assert facts["version"] == version
    assert facts["provenance"]["schema_version"] == 1
    assert facts["provenance"]["emitter"] == "generate-docs"
    assert facts["major_minor"] == ".".join(version.split(".")[:2])
    assert facts["platform_count"] == len(platform_ids)
    assert sorted(row["id"] for row in facts["platforms"]) == sorted(platform_ids)
    assert facts["skill_count"] == skill_count
    assert facts["workflow_count"] == workflow_count


def test_framework_docs_lifecycle_frontmatter_is_recursive() -> None:
    root = Path(__file__).resolve().parents[2]
    missing: list[str] = []
    stale: list[str] = []

    for path in sorted((root / "ls" / "docs").glob("**/*.md")):
        rel = path.relative_to(root)
        if any(part in {"_generated", "local-context"} for part in rel.parts):
            continue
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            missing.append(str(rel))
            continue
        header = text.split("---", 2)[1]
        if "status:" not in header or "version:" not in header:
            missing.append(str(rel))
        if "version: 3.0" in header:
            stale.append(str(rel))

    assert missing == []
    assert stale == []


def test_catalog_validation_and_pack_selection() -> None:
    root = Path(__file__).resolve().parents[2]

    assert validate_skill_catalog(root) == []
    assert validate_workflow_catalog(root) == []
    assert "ls-context" in selected_skill_names(root, ["core"])
    assert "ls-cloudflare-dns" not in selected_skill_names(root, ["core"])
    assert "ls-workflow-ops-tmux-session" in selected_workflow_names(root, ["core"])
    assert "ls-workflow-tmux-terminal-mode" in selected_workflow_names(root, ["core"])
    assert "ls-workflow-ops-tmux-session" in selected_workflow_names(root, ["ops"])
    assert "ls-workflow-tmux-terminal-mode" in selected_workflow_names(root, ["ops"])
    assert "ls-system-info" in selected_skill_names(root, ["ops"])
    assert "ls-context-index" in selected_skill_names(root, ["dev"])
    assert "ls-context-index" in selected_skill_names(root, ["harness"])
    assert "ls-framework-audit" in selected_skill_names(root, ["bootstrap"])
    assert "ls-framework-audit" in selected_skill_names(root, ["dev"])
    assert not (root / "ls/workflows/ls-workflow-context-index-query").exists()
    assert not (root / "ls/workflows/ls-workflow-context-index-refresh").exists()
    assert "ls-workflow-context-index-query" not in selected_workflow_names(root, ["dev"])
    assert "ls-workflow-context-index-refresh" not in selected_workflow_names(root, ["dev", "harness"])
    ledger = (root / "ls/skills/ls-context-index/docs/source-ledger.md").read_text(encoding="utf-8")
    assert "ls-workflow-context-index-query" not in ledger
    assert "ls-workflow-context-index-refresh" not in ledger
    for selector in (
        "ls-workflow-context-index-query",
        "context-index-query",
        "query context index",
        "context search",
        "ls-workflow-context-index-refresh",
        "context-index-refresh",
        "context refresh",
        "refresh context index",
    ):
        with pytest.raises(ValueError, match="unknown workflow selector"):
            resolve_package_selection(root, workflows=[selector])
    assert not (root / "ls/workflows/ls-workflow-skills-index-refresh").exists()
    assert "ls-skill-discovery" in selected_skill_names(root, ["dev"])
    assert "ls-workflow-skills-index-refresh" not in selected_workflow_names(root, ["dev"])
    for selector in (
        "ls-workflow-skills-index-refresh",
        "skills-index-refresh",
        "refresh skills",
        "scrub index",
    ):
        with pytest.raises(ValueError, match="unknown workflow selector"):
            resolve_package_selection(root, workflows=[selector])
    assert not (root / "ls/workflows/ls-workflow-audit-framework").exists()
    assert "ls-workflow-audit-framework" not in selected_workflow_names(root, ["bootstrap", "dev"])
    for selector in (
        "ls-workflow-audit-framework",
        "audit-framework",
        "run audit",
        "framework audit",
    ):
        with pytest.raises(ValueError, match="unknown workflow selector"):
            resolve_package_selection(root, workflows=[selector])


def test_skill_taxonomy_covers_all_shipped_skills_and_allowed_classes() -> None:
    root = Path(__file__).resolve().parents[2]
    pack = load_pack_config(root)
    skill_names = {path.parent.name for path in (root / "ls" / "skills").glob("ls-*/SKILL.md")}
    taxonomy = pack.skill_taxonomy

    assert set(taxonomy) == skill_names
    assert len(taxonomy) == len(skill_names)
    assert {row["class"] for row in taxonomy.values()} <= ALLOWED_SKILL_TAXONOMY_CLASSES
    assert {row["owner_scope"] for row in taxonomy.values()} == {"skill"}


def test_skill_catalog_uses_taxonomy_sort_order_and_payload_fields() -> None:
    root = Path(__file__).resolve().parents[2]
    catalog = load_skill_catalog(root)
    sort_keys = [(skill.sort_priority, skill.name) for skill in catalog]
    payload = skill_taxonomy_payload(root)

    assert sort_keys == sorted(sort_keys)
    assert [row["id"] for row in payload["skills"]] == [skill.name for skill in catalog]
    assert payload["count"] == len(catalog)
    assert payload["skills"][0]["sort_priority"] == 10
    assert {"class", "sort_priority", "tags", "owner_scope", "packs"} <= set(payload["skills"][0])


def test_agent_queue_example_yaml_has_expected_runtime_shape() -> None:
    root = Path(__file__).resolve().parents[2]
    module_path = root / "ls" / "tools" / "agentq_transport_client" / "agentq_transport_client" / "file_drop.py"
    spec = importlib.util.spec_from_file_location("agentq_file_drop_under_test", module_path)
    assert spec is not None and spec.loader is not None
    file_drop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(file_drop)

    payload = yaml.safe_load((root / "ls" / "config" / "agent_queue.example.yaml").read_text(encoding="utf-8"))

    assert payload["layout"] in {"flat", "structured"}
    assert payload["queue_path"] == ".agent/queue"
    assert payload["agent_trust_registry_path"] == "ls/config/agent_trust_registry.yaml"
    assert set(payload["transports_enabled"]) == {"mail", "file_drop"}
    assert payload["version_mismatch_policy"] in {"warn", "block", "allow_log"}
    assert payload["post_ingest_mailbox"]
    assert payload["sealed_extension"].startswith(".")
    assert payload["ignore_globs"] == file_drop.default_ignore_globs()
    assert payload["archive_retention_days"] > 0
    assert payload["archive_max_total_gb"] > 0


def test_skill_allowed_tools_frontmatter_is_space_separated() -> None:
    root = Path(__file__).resolve().parents[2]

    for skill_md in sorted((root / "ls" / "skills").glob("ls-*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        frontmatter = text.split("---", 2)[1]
        metadata = yaml.safe_load(frontmatter) or {}
        allowed_tools = metadata.get("allowed-tools")
        if allowed_tools is None:
            continue

        assert isinstance(allowed_tools, str), skill_md
        assert "," not in allowed_tools, skill_md
        assert allowed_tools.split(), skill_md


def test_skill_normalization_has_one_normative_owner_and_blocking_conflict_rule() -> None:
    root = Path(__file__).resolve().parents[2]
    normalizer = (root / "ls/skills/ls-skill-normalizer/SKILL.md").read_text(encoding="utf-8")
    public_mirror = (root / "ls/docs/SKILL_NORMALIZATION.md").read_text(encoding="utf-8")
    importer = (root / "ls/skills/ls-skill-importer/SKILL.md").read_text(encoding="utf-8")
    importing_doc = (root / "ls/docs/SKILL_IMPORTING.md").read_text(encoding="utf-8")

    assert normalizer.count("normative normalization execution contract") == 1
    assert "owner_skill: ls-skill-normalizer" in public_mirror
    assert "synchronized public mirror and detailed reference" in public_mirror
    for text in (normalizer, public_mirror):
        assert "the skill controls" in text
        assert "stop before any affected write" in text
        assert "higher-level user, repository, and safety policy" in text

    for text in (normalizer, public_mirror, importer, importing_doc):
        assert "single source of truth" not in text.lower()
        assert "normalization source of truth" not in text.lower()
    for text in (importer, importing_doc):
        assert "`ls-skill-normalizer` as the normative normalization contract" in text
        assert "synchronized public checklist and examples" in text

    assert "keep as is" in public_mirror
    assert "keep platform-specific but normalized" in public_mirror
    assert "fully normalize" in public_mirror
    assert "documents first" in normalizer
    assert "tooling second" in normalizer
    assert "present them for approval, then write" in normalizer
    assert "If approved" in normalizer
    assert "TOOLING_POLICY.md" in normalizer
    assert "INPUT_HARDENING_STANDARD.md" in normalizer
    assert "keep-original-tooling exception" in importing_doc
    assert "Replicate behavior" in public_mirror
    assert "Update all documents" in public_mirror
    assert "Pass full vetting" in importer
    assert "Validate the frozen bytes" in importer
    assert "Copy, register, and confirm" in importer
    assert "the normalization gate remains unpassed" in public_mirror
    assert "copy as-is and warn" not in public_mirror


def test_old_workflow_skill_references_are_cut_over() -> None:
    root = Path(__file__).resolve().parents[2]
    stale = [
        "ls-agentic-" + "prd-batch",
        "ls-agentic-" + "umbrella-queue",
        "ls-decision-tree-" + "workflow",
        "ls-tmux-shared-session-" + "workflow",
        "ls-publish-" + "workflow",
        "localsetup-publish-" + "workflow",
        "WORKFLOW_" + "INDEX.md",
        "MAINTENANCE_" + "WORKFLOW.md",
        "scripts/" + "publish",
    ]
    scanned_suffixes = {".md", ".mdc", ".yaml", ".json", ".py", ".sh", ".ps1"}
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in scanned_suffixes:
            continue
        if any(part in {".git", ".codex", ".localsetup-maint", ".venv", ".venv-codex", "__pycache__"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in stale:
            if needle in text:
                offenders.append(f"{path.relative_to(root)}:{needle}")
    assert offenders == []


def test_codex_templates_distinguish_current_and_historical_skill_roots() -> None:
    root = Path(__file__).resolve().parents[2]
    codex_context = (root / "ls" / "templates" / "codex" / "AGENTS.md").read_text(encoding="utf-8")
    finalizer = yaml.safe_load(
        (root / "ls" / "templates" / "config" / "localsetup_finalizer.yaml").read_text(encoding="utf-8")
    )

    assert "`.agents/skills` is the current shared repository skills root" in codex_context
    assert "`.codex/skills` is the historical Codex preservation and transition surface" in codex_context
    for key in ("managed_output_globs", "stage_allowlist_globs"):
        assert ".agents/skills/**" in finalizer[key]
        assert ".codex/skills/**" in finalizer[key]


def test_static_skill_indexes_use_four_omniroute_owners() -> None:
    root = Path(__file__).resolve().parents[2]
    paths = [
        root / "ls/templates/claude-code/CLAUDE.md",
        root / "ls/templates/codex/AGENTS.md",
        root / "ls/templates/cursor/ls-context-index.md",
        root / "ls/templates/cursor/ls-context.mdc",
        root / "ls/templates/kilo/AGENTS.md",
        root / "ls/templates/kilo/instructions.md",
        root / "ls/templates/openclaw/OPENCLAW_CONTEXT.md",
        root / "ls/templates/opencode/AGENTS.md",
    ]
    retained = {
        "ls-omniroute",
        "ls-omniroute-admin-automation",
        "ls-omniroute-proxy",
        "ls-omniroute-update",
    }
    removed = {
        "ls-omniroute-codex",
        "ls-omniroute-context",
        "ls-omniroute-integrations",
        "ls-omniroute-observability",
    }

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert all(name in text for name in retained), path
        assert all(name not in text for name in removed), path


def test_authored_current_omniroute_surfaces_have_no_removed_local_slugs() -> None:
    root = Path(__file__).resolve().parents[2]
    removed = {
        "ls-omniroute-codex",
        "ls-omniroute-context",
        "ls-omniroute-integrations",
        "ls-omniroute-observability",
    }
    paths = [
        root / "ls/config/pack.yaml",
        root / "ls/tests/skill_smoke_commands.yaml",
        *sorted((root / "ls/templates").rglob("*.md")),
    ]
    for owner in (
        "ls-omniroute",
        "ls-omniroute-proxy",
        "ls-omniroute-admin-automation",
        "ls-omniroute-update",
    ):
        paths.extend(sorted((root / "ls/skills" / owner).rglob("*.md")))
        paths.extend(sorted((root / "ls/skills" / owner).rglob("*.yaml")))

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert all(name not in text for name in removed), path


def test_generated_artifact_provenance_includes_dependency_ledger_owners() -> None:
    from ls.core import docs
    from ls.core.docs_artifacts import common

    expected = {
        "ls/config/dependency-ledger.yaml",
        "ls/config/dependency-ledger.schema.json",
    }
    assert expected <= set(docs.ARTIFACT_SOURCE_INPUTS)
    assert expected <= set(common.ARTIFACT_SOURCE_INPUTS)


def test_baseline_file_classification() -> None:
    assert classify_path("ls/skills/ls-context/SKILL.md") == "keep"
    assert classify_path("ls/workflows/ls-workflow-ops-tmux-session/SKILL.md") == "keep"
    assert classify_path("ls/docs/_generated/artifact-registry.json") == "generate"
    assert classify_path("ls/docs/_generated/skill_aliases.json") == "generate"
    assert classify_path("ls/docs/local-context/SECRETS_OVERVIEW.md") == "private-maintainer"
    assert classify_path("scripts/generate-doc-artifacts") == "private-maintainer"


def test_baseline_tracked_files_excludes_untracked_local_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)

    files = tracked_files(root)

    assert "tracked.txt" in files
    assert "untracked.txt" not in files


def test_generated_implementation_map_includes_workflow_sources() -> None:
    root = Path(__file__).resolve().parents[2]
    file_map = (root / "ls" / "docs" / "_generated" / "implementation-file-map.md").read_text(
        encoding="utf-8"
    )

    assert "ls/config/workflow.schema.json" in file_map
    assert "ls/docs/_generated/workflow-catalog.json" in file_map
    assert "ls/workflows/ls-workflow-ops-tmux-session/SKILL.md" in file_map
    assert "ls/docs/local-context/" not in file_map
    assert ".localsetup-maint/" not in file_map


def test_verify_rules_wrapper_prefers_uv_project_venv() -> None:
    wrapper = (ROOT / "ls" / "tools" / "verify_rules").read_text(encoding="utf-8")

    assert 'REPO_ROOT="$(cd "$ENGINE_DIR/.." && pwd)"' in wrapper
    assert '$REPO_ROOT/.venv/bin/python' in wrapper
    assert 'LOCALSETUP_PYTHON_BIN' in wrapper
