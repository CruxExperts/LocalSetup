import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ls.core.provenance import provenance_report


def copy_docs_alignment_repo(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    (repo / "ls").mkdir(parents=True)
    shutil.copytree(source / "ls" / "config", repo / "ls" / "config")
    shutil.copytree(source / "ls" / "core", repo / "ls" / "core")
    shutil.copytree(source / "ls" / "skills", repo / "ls" / "skills")
    shutil.copytree(source / "ls" / "workflows", repo / "ls" / "workflows")
    shutil.copytree(source / "ls" / "tools", repo / "ls" / "tools")
    shutil.copytree(source / "assets", repo / "assets")
    shutil.copy2(source / "VERSION", repo / "VERSION")
    shutil.copy2(source / "pyproject.toml", repo / "pyproject.toml")
    (repo / "ls" / "docs").mkdir(parents=True)
    shutil.copytree(source / "ls" / "docs" / "_generated", repo / "ls" / "docs" / "_generated")
    for rel in (
        "README.md",
        "FEATURES.md",
        "PLATFORM_REGISTRY.md",
        "OUTPUT_AND_DOC_GENERATION.md",
        "DOCUMENT_LIFECYCLE_MANAGEMENT.md",
        "WORKFLOW_STANDARD.md",
        "migration/skill-alias-map.md",
    ):
        src = source / "ls" / "docs" / rel
        dst = repo / "ls" / "docs" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for rel in ("README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md", "SUPPORT.md"):
        shutil.copy2(source / rel, repo / rel)
    (repo / ".github" / "workflows").mkdir(parents=True)
    shutil.copy2(source / ".github" / "workflows" / "docs-sync.yml", repo / ".github" / "workflows" / "docs-sync.yml")
    return repo


def run_tool(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "ls/tools/docs_alignment.py", "--repo-root", ".", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=check,
    )


def init_clean_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "chore: initial"], cwd=repo, check=True)


def test_public_inventory_excludes_untracked_local_notes(tmp_path: Path):
    from ls.core.docs_alignment.io import _markdown_files
    (tmp_path / "README.md").write_text("# Public\n")
    init_clean_git_repo(tmp_path)
    (tmp_path / "PRIVATE_NOTES.md").write_text("Do not publish\n")
    assert [path.name for path in _markdown_files(tmp_path)] == ["README.md"]


def test_theme_picture_sources_are_counted_and_checked(tmp_path: Path) -> None:
    from ls.core.docs_alignment.assets import collect_asset_manifest
    from ls.core.docs_alignment.io import _markdown_links

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "hero-dark.png").write_bytes(b"dark")
    (tmp_path / "assets" / "hero-light.png").write_bytes(b"light")
    (tmp_path / "README.md").write_text(
        '<picture><source media="(prefers-color-scheme: dark)" '
        'srcset="assets/hero-dark.png 1x, assets/hero-light.png 2x">'
        '<img src="assets/hero-light.png" alt="LocalSetup diagram"></picture>\n',
        encoding="utf-8",
    )
    links = list(_markdown_links((tmp_path / "README.md").read_text()))
    assert [target for kind, target, _, _ in links if kind == "source"] == [
        "assets/hero-dark.png", "assets/hero-light.png"
    ]
    assets = collect_asset_manifest(tmp_path)["assets"]
    assert all(asset["references"] == ["README.md"] for asset in assets)


def test_inventory_discovers_docs_assets_skills_workflows_and_ci(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    payload = json.loads(run_tool(repo, "inventory").stdout)

    assert payload["schema_version"] == "1.0"
    assert payload["counts"]["skills"] >= 49
    expected_workflows = list((repo / "ls" / "workflows").glob("*/workflow.yaml"))
    assert expected_workflows
    assert payload["counts"]["workflows"] == len(expected_workflows)
    assert payload["counts"]["platforms"] == 20
    assert any(row["path"] == "README.md" for row in payload["docs"])
    assert any(row["path"].startswith("assets/") for row in payload["assets"])
    assert ".github/workflows/docs-sync.yml" in payload["ci_workflows"]
    framework_docs = [row for row in payload["docs"] if row["class"] == "framework" and row["status"] == "ACTIVE"]
    assert framework_docs
    assert all(row["owner_skill"] or row["owner_package"] for row in framework_docs)


def test_audit_catches_active_framework_doc_missing_owner(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    doc = repo / "ls" / "docs" / "OUTPUT_AND_DOC_GENERATION.md"
    text = doc.read_text(encoding="utf-8")
    text = re.sub(r"\nowner_skill: [^\n]+\n", "\n", text, count=1)
    doc.write_text(text, encoding="utf-8")

    payload = json.loads(run_tool(repo, "audit").stdout)

    assert any(finding["category"] == "ownership" for finding in payload["findings"])
    assert payload["ok"] is False


def test_inventory_ignores_gitignored_markdown_when_git_metadata_exists(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    (repo / ".gitignore").write_text("docs/\n", encoding="utf-8")
    run = subprocess.run
    run(["git", "init", "-q"], cwd=repo, check=True)
    run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    run(["git", "add", "."], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "chore: initial"], cwd=repo, check=True)
    ignored = repo / "docs" / "reference" / "markdown-reference-audit.md"
    ignored.parent.mkdir(parents=True, exist_ok=True)
    ignored.write_text("# Local audit output\n", encoding="utf-8")

    payload = json.loads(run_tool(repo, "inventory").stdout)

    assert "docs/reference/markdown-reference-audit.md" not in {row["path"] for row in payload["docs"]}


def test_audit_catches_stale_skill_count_and_version_drift(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    readme = repo / "README.md"
    readme.write_text(
        re.sub(r"\d+ shipped capability skills plus \d+ first-class workflow packages", "46 shipped capability skills plus 17 first-class workflow packages", readme.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    facts = repo / "ls" / "docs" / "_generated" / "facts.json"
    data = json.loads(facts.read_text(encoding="utf-8"))
    data["version"] = "0.0.0"
    facts.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    payload = json.loads(run_tool(repo, "audit").stdout)

    categories = {finding["category"] for finding in payload["findings"]}
    assert "stale_count" in categories
    assert "generated_drift" in categories
    assert payload["ok"] is False


def test_audit_catches_lifecycle_links_images_and_fences(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    bad = repo / "ls" / "docs" / "BAD.md"
    bad.write_text("# Bad\n\n![ ](missing.png)\n[missing](missing.md)\n```bash\nunterminated\n", encoding="utf-8")

    payload = json.loads(run_tool(repo, "audit").stdout)
    categories = {finding["category"] for finding in payload["findings"]}

    assert "lifecycle" in categories
    assert "link" in categories
    assert "asset" in categories
    assert "markdown" in categories


def test_audit_skips_links_inside_inert_source_snapshots(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    source_snapshot = repo / "ls" / "skills" / "ls-context" / "references" / "upstream" / "UPSTREAM_SKILL.source.md"
    source_snapshot.parent.mkdir(parents=True, exist_ok=True)
    source_snapshot.write_text("# Upstream\n\n[missing upstream sidecar](sidecar.md)\n", encoding="utf-8")

    payload = json.loads(run_tool(repo, "audit").stdout)

    assert not any(
        finding["category"] == "link" and finding["path"].endswith("UPSTREAM_SKILL.source.md")
        for finding in payload["findings"]
    )


def test_apply_dry_run_does_not_mutate_files(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    readme = repo / "README.md"
    before = readme.read_text(encoding="utf-8")

    payload = json.loads(run_tool(repo, "apply", "--scope", "all", "--dry-run").stdout)

    assert payload["dry_run"] is True
    assert readme.read_text(encoding="utf-8") == before
    assert payload["changed"]


def test_apply_generated_and_assets_write_stable_outputs(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)

    run_tool(repo, "apply", "--scope", "generated")

    for rel in (
        "ls/docs/_generated/docs-inventory.json",
        "ls/docs/_generated/docs-truth-map.json",
        "ls/docs/_generated/docs-audit-result.json",
        "ls/docs/_generated/docs-asset-manifest.json",
    ):
        payload = json.loads((repo / rel).read_text(encoding="utf-8"))
        assert payload["schema_version"] == "1.0"
        assert payload["provenance"]["schema_version"] == 1
        assert payload["provenance"]["emitter"] == "docs-align"
    inventory = json.loads((repo / "ls/docs/_generated/docs-inventory.json").read_text(encoding="utf-8"))
    assert inventory["repo"] == "."
    summary = (repo / "ls/docs/_generated/docs-alignment-summary.md").read_text(encoding="utf-8")
    assert "localsetup_provenance:" in summary
    assert "# Documentation Alignment Summary" in summary
    assert "# Asset Inventory" in (repo / "assets" / "README.md").read_text(encoding="utf-8")


def test_check_ci_exits_nonzero_on_major_findings(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    readme = repo / "README.md"
    readme.write_text(
        re.sub(r"\d+ shipped capability skills plus \d+ first-class workflow packages", "46 shipped capability skills plus 17 first-class workflow packages", readme.read_text(encoding="utf-8")),
        encoding="utf-8",
    )

    completed = run_tool(repo, "check", "--ci", check=False)

    assert completed.returncode == 1
    assert "stale_count" in completed.stdout


def test_check_ci_exits_nonzero_on_asset_readme_drift(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    run_tool(repo, "apply", "--scope", "generated")
    (repo / "assets" / "README.md").write_text("# stale\n", encoding="utf-8")

    completed = run_tool(repo, "check", "--ci", check=False)

    assert completed.returncode == 1
    assert "asset_readme.drift" in completed.stdout


def test_managed_public_count_update_preserves_surrounding_content(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    readme = repo / "README.md"
    readme.write_text(
        re.sub(r"\d+ shipped capability skills plus \d+ first-class workflow packages", "46 shipped capability skills plus 17 first-class workflow packages", readme.read_text(encoding="utf-8")),
        encoding="utf-8",
    )

    payload = json.loads(run_tool(repo, "apply", "--scope", "public").stdout)
    text = readme.read_text(encoding="utf-8")

    assert "README.md" in payload["changed"]
    assert "# LocalSetup" in text
    facts = json.loads((repo / "ls" / "docs" / "_generated" / "facts.json").read_text(encoding="utf-8"))
    assert f"{facts['skill_count']} shipped capability skills plus" in text
    assert "<!-- facts-block:start -->" in text


def test_cli_wrapper_delegates_docs_align(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)

    completed = subprocess.run(
        [sys.executable, "ls/tools/localsetup.py", "--repo", ".", "docs-align", "inventory"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "1.0"


def test_mutable_state_policy_surfaces_keep_backlog_outside_framework_source() -> None:
    repo = Path(__file__).resolve().parents[2]
    surfaces = [
        "ls/docs/SKILLS_AND_RULES.md",
        "ls/docs/REPO_AND_DATA_SEPARATION.md",
        "ls/skills/ls-backlog-and-reminders/SKILL.md",
        "ls/skills/ls-backlog-and-reminders/references/backlog-template.md",
        "ls/templates/codex/AGENTS.md",
        "ls/templates/claude-code/CLAUDE.md",
        "ls/templates/cursor/ls-context.mdc",
        "ls/templates/kilo/AGENTS.md",
        "ls/templates/kilo/instructions.md",
        "ls/templates/opencode/AGENTS.md",
        "ls/templates/openclaw/OPENCLAW_CONTEXT.md",
    ]
    banned = (
        "write freely",
        "freely writable",
        "NOT protected",
        "under ls/ or repo-level path",
        "ls/backlog.md",
    )
    combined = "\n".join((repo / rel).read_text(encoding="utf-8") for rel in surfaces)

    for phrase in banned:
        assert phrase not in combined
    assert "outside `ls/`" in combined
    assert ".localsetup/backlog.md" in combined


def test_generated_alignment_outputs_are_checkout_deterministic(tmp_path: Path) -> None:
    repo_a = copy_docs_alignment_repo(tmp_path / "a")
    repo_b = copy_docs_alignment_repo(tmp_path / "b")

    run_tool(repo_a, "apply", "--scope", "generated")
    run_tool(repo_b, "apply", "--scope", "generated")

    for rel in (
        "ls/docs/_generated/docs-inventory.json",
        "ls/docs/_generated/docs-truth-map.json",
        "ls/docs/_generated/docs-asset-manifest.json",
        "ls/docs/_generated/docs-audit-result.json",
        "ls/docs/_generated/docs-alignment-summary.md",
        "assets/README.md",
    ):
        assert (repo_a / rel).read_text(encoding="utf-8") == (repo_b / rel).read_text(encoding="utf-8")

    before = {
        rel: (repo_a / rel).read_text(encoding="utf-8")
        for rel in (
            "ls/docs/_generated/docs-inventory.json",
            "ls/docs/_generated/docs-truth-map.json",
            "ls/docs/_generated/docs-asset-manifest.json",
            "ls/docs/_generated/docs-audit-result.json",
            "ls/docs/_generated/docs-alignment-summary.md",
            "assets/README.md",
        )
    }
    run_tool(repo_a, "apply", "--scope", "generated")
    assert {
        rel: (repo_a / rel).read_text(encoding="utf-8")
        for rel in before
    } == before


def test_generated_artifact_provenance_survives_clean_commit(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    init_clean_git_repo(repo)

    subprocess.run(
        [sys.executable, "ls/tools/generate_docs_artifacts.py", "--repo-root", "."],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "docs: regenerate artifacts"], cwd=repo, check=True)

    report = provenance_report(repo)

    assert report["warnings"] == []


def test_generated_artifact_registry_is_stable_across_generator_order(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    broad = [sys.executable, "ls/tools/generate_docs_artifacts.py", "--repo-root", "."]
    alias = [sys.executable, "ls/tools/localsetup.py", "--source-root", ".", "generate-docs"]

    subprocess.run(broad, cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(alias, cwd=repo, text=True, capture_output=True, check=True)
    first = (repo / "ls/docs/_generated/artifact-registry.json").read_text(encoding="utf-8")
    subprocess.run(broad, cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(alias, cwd=repo, text=True, capture_output=True, check=True)
    second = (repo / "ls/docs/_generated/artifact-registry.json").read_text(encoding="utf-8")

    assert second == first


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "ls/tools/localsetup.py", "--source-root", ".", "generate-docs"],
        [sys.executable, "ls/tools/generate_docs_artifacts.py", "--repo-root", "."],
    ],
)
def test_generate_docs_stale_projection_fails_before_any_output_write(tmp_path: Path, command: list[str]) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    watched = [
        repo / "ls/docs/_generated/skill_aliases.json",
        repo / "ls/docs/SKILLS.md",
        repo / "ls/docs/_generated/artifact-registry.json",
    ]
    before = {path: path.read_bytes() if path.exists() else None for path in watched}
    (repo / "ls/config/platforms.yaml").write_text("platforms: []\n", encoding="utf-8")

    completed = subprocess.run(command, cwd=repo, text=True, capture_output=True, check=False)

    assert completed.returncode != 0
    assert "client-registry generate" in completed.stderr
    assert {path: path.read_bytes() if path.exists() else None for path in watched} == before


def test_broad_generator_refreshes_alias_owned_docs_before_alignment(tmp_path: Path) -> None:
    repo = copy_docs_alignment_repo(tmp_path)
    migration = repo / "ls" / "docs" / "migration" / "skill-alias-map.md"
    migration.write_text(
        re.sub(r"\nowner_package: [^\n]+\n", "\n", migration.read_text(encoding="utf-8"), count=1),
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, "ls/tools/generate_docs_artifacts.py", "--repo-root", "."],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "owner_package: generate-docs" in migration.read_text(encoding="utf-8")
    audit = json.loads((repo / "ls/docs/_generated/docs-audit-result.json").read_text(encoding="utf-8"))
    assert not any(
        finding["category"] == "ownership"
        and finding["path"] == "ls/docs/migration/skill-alias-map.md"
        for finding in audit["findings"]
    )
    inventory = json.loads((repo / "ls/docs/_generated/docs-inventory.json").read_text(encoding="utf-8"))
    migration_row = next(
        row for row in inventory["docs"] if row["path"] == "ls/docs/migration/skill-alias-map.md"
    )
    assert migration_row["owner_package"] == "generate-docs"


def test_verified_upstream_docs_keep_ownership_without_hiding_owned_links(tmp_path: Path) -> None:
    from ls.core.docs_alignment.audit import audit
    from ls.core.docs_alignment.inventory import collect_inventory
    from ls.core.sdk_payload.ownership import upstream_documents

    repo = copy_docs_alignment_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "vendor", repo / "vendor")
    owned = repo / "vendor" / "unowned" / "README.md"
    owned.parent.mkdir()
    owned.write_text("[Broken owned reference](missing-file.md)\n")
    inventory = collect_inventory(repo)
    upstream = [row for row in inventory["docs"] if row["class"] == "upstream"]
    assert len(upstream) == 64
    assert all("/blob/" in row["upstream"]["source_url"] for row in upstream)
    findings = audit(repo)["findings"]
    assert any(f["path"] == "vendor/unowned/README.md" and f["category"] == "link" for f in findings)
    assert not any(f["path"].startswith("vendor/lscli/") for f in findings)
    retained = repo / upstream[0]["path"]
    retained.write_text(retained.read_text() + "\nModified retained content\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        upstream_documents(repo)
