"""Release documentation records are deterministic and based on committed evidence."""
from __future__ import annotations

import subprocess
import json
from pathlib import Path

import pytest

from ls.core.release_docs import check, load_record, plan, render_outputs, tracked_documents, validate_record
from ls.core.release_docs.render import notes


def test_version_only_frontmatter_is_not_a_new_semantic_change():
    from ls.core.release_docs.planning import _version_metadata_only
    before = "---\nversion: 4.22\nstatus: ACTIVE\n---\nKeep this guidance.\n"
    after = before.replace("version: 4.22", "version: 4.23")
    assert _version_metadata_only(before, after, "4.22.9", "4.23.0")
    assert not _version_metadata_only(before, after.replace("Keep", "Change"), "4.22.9", "4.23.0")
    assert not _version_metadata_only(before, after.replace("4.23", "9.99"), "4.22.9", "4.23.0")
    assert not _version_metadata_only("", after, "4.22.9", "4.23.0")

def test_semantic_mapping_uses_source_references_not_generic_words_or_doc_links() -> None:
    from ls.core.release_docs.planning import _matches_document
    assert not _matches_document("ls/docs/OTHER.md", "An agent reads content in README.md at VERSION.", "ls/core/release_docs/agent.py")
    assert not _matches_document("ls/docs/OTHER.md", "Read README.md", "README.md")
    assert _matches_document("ls/docs/OTHER.md", "Calls ls.core.release_docs.agent", "ls/core/release_docs/agent.py")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "ls/docs/releases").mkdir(parents=True)
    (tmp_path / "ls/skills/example").mkdir(parents=True)
    (tmp_path / "ls/core").mkdir(parents=True)
    (tmp_path / "ls/tests").mkdir(parents=True)
    (tmp_path / "VERSION").write_text("4.4.0\n")
    (tmp_path / "README.md").write_text("# Home\n\n**Version:** 4.4.0<br>\n\n<!-- release-summary:start -->\nold\n<!-- release-summary:end -->\n")
    (tmp_path / "ls/README.md").write_text("# Framework\n\n**Version:** 4.4.0<br>\n\n<!-- release-link:start -->\nold\n<!-- release-link:end -->\n")
    (tmp_path / "ls/docs/README.md").write_text("# Docs\n\n<!-- release-link:start -->\nold\n<!-- release-link:end -->\n")
    (tmp_path / "ls/docs/QUICKSTART.md").write_text("# Quickstart\n\n## Update\n")
    (tmp_path / "ls/docs/ADAPTER_OWNERSHIP.md").write_text("# Adapter ownership\n")
    (tmp_path / "ls/docs/ACTIVE.md").write_text("---\nstatus: ACTIVE\nversion: 4.4\nowner_skill: example\n---\n\n# Active\n")
    (tmp_path / "ls/docs/DRAFT.md").write_text("---\nstatus: DRAFT\nversion: 4.4\nowner_skill: example\n---\n\n# Draft\n")
    (tmp_path / "docs/private.md").parent.mkdir()
    (tmp_path / "docs/private.md").write_text("private maintenance planning\n")
    (tmp_path / "ls/skills/example/SKILL.md").write_text("# Example\n")
    (tmp_path / "ls/docs/releases/4.3.0.md").write_text("# Historical\n")
    (tmp_path / "AGENTS.md").write_text("private policy\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "chore: base")
    git(tmp_path, "tag", "v4.4.0")
    return tmp_path


def record(repo: Path) -> dict:
    return {
        "schema_version": 1,
        "version": "4.4.1",
        "source_commit": git(repo, "rev-parse", "HEAD"),
        "baseline_tag": "v4.4.0",
        "summary": "The release makes documentation updates reliable.",
        "highlights": [{"text": "Release sections are rendered from one checked record.", "evidence": ["README.md"]}],
        "compatibility": ["No compatibility changes are required."],
        "update": ["Install the current package before updating managed files."],
        "verification": ["Download the `.sha256` checksum and `.cdx.json` SBOM sidecars with the archive."],
    }


def test_release_notes_link_to_versioned_guide(repo: Path) -> None:
    rendered = notes(record(repo))
    assert "[release guide](https://github.com/CruxExperts/localsetup/blob/v4.4.1/ls/docs/releases/4.4.1.md)" in rendered


def test_record_validation_load_and_rendered_candidate_check(repo: Path) -> None:
    value = validate_record(record(repo))
    record_path = repo / "ls/docs/releases/4.4.1.json"
    record_path.write_text(json.dumps(value))
    assert load_record(repo, "4.4.1") == value
    outputs = render_outputs(repo, value)
    assert "## What's new in 4.4.1" in outputs["README.md"]
    assert "releases/4.4.1.md" in outputs["ls/docs/README.md"]
    assert not check(repo, value)["ok"]
    for relative, text in outputs.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    result = check(repo, value, expected_commit=value["source_commit"], target_version="4.4.1")
    assert result["ok"], result["findings"]


def test_record_rejects_unsafe_evidence_and_bad_baseline_tag(repo: Path) -> None:
    unsafe = record(repo)
    unsafe["highlights"][0]["evidence"] = ["../secret"]
    with pytest.raises(ValueError, match="evidence"):
        validate_record(unsafe)
    malformed = record(repo)
    malformed["baseline_tag"] = "4.4.0"
    with pytest.raises(ValueError, match="baseline_tag"):
        validate_record(malformed)


def test_inventory_and_plan_use_tracked_committed_sources(repo: Path, monkeypatch) -> None:
    (repo / "ls/core/release_feature.py").write_text("VALUE = 1\n")
    (repo / "ls/tests/test_release_feature.py").write_text("def test_value(): assert True\n")
    git(repo, "add", "ls/core/release_feature.py", "ls/tests/test_release_feature.py")
    git(repo, "commit", "-qm", "fix: make release evidence available")
    documents = tracked_documents(repo)
    assert "README.md" in documents
    assert "ls/skills/example/SKILL.md" in documents
    assert "ls/docs/ACTIVE.md" in documents
    assert "ls/docs/DRAFT.md" not in documents
    assert "docs/private.md" not in documents
    assert "AGENTS.md" not in documents
    assert "ls/docs/releases/4.3.0.md" not in documents
    from ls.core.release_docs import planning
    original_read = planning._committed_text
    reads = []
    def counted_read(root, ref, path):
        reads.append(path)
        return original_read(root, ref, path)
    monkeypatch.setattr(planning, "_committed_text", counted_read)
    result = plan(repo, base="v4.4.0")
    assert reads.count("ls/docs/ACTIVE.md") == 1
    assert result["target_version"] == "4.4.1"
    assert result["source_commit"] == git(repo, "rev-parse", "HEAD")
    assert {row["path"] for row in result["source_material"]} == set(result["changed_paths"])
    assert any("release_feature.py" in row["diff"] for row in result["source_material"])
    assert result["documents"] == documents
    assert result["reference_material"][0]["path"] == "ls/docs/QUICKSTART.md"
    assert "UNCHANGED OPERATIONAL REFERENCE" in result["reference_material"][0]["content"]


def test_missing_managed_block_fails_closed_and_expected_source_can_bind(repo: Path) -> None:
    (repo / "README.md").write_text("# Home\n")
    value = record(repo)
    result = check(repo, value, target_version="4.4.1")
    assert not result["ok"]
    assert result["findings"][0]["code"] == "render_failure"
    (repo / "README.md").write_text("# Home\n\n<!-- release-summary:start -->\nold\n<!-- release-summary:end -->\n")
    outputs = render_outputs(repo, value)
    for relative, text in outputs.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    receipt = repo / "ls/docs/_generated/receipt.md"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("generated documentation receipt\n")
    git(repo, "add", "ls/docs/_generated/receipt.md")
    git(repo, "commit", "-qm", "docs: generated receipt")
    assert check(repo, value, target_version="4.4.1")["ok"]
    assert any(item["code"] == "source_commit_mismatch" for item in check(repo, value, expected_commit="a" * 40)["findings"])


def test_explicit_published_base_limits_source_material_and_repair_keeps_current_version(repo: Path) -> None:
    anchor = git(repo, "rev-parse", "HEAD")
    (repo / ".localsetup-release.json").write_text(json.dumps({
        "schema_version": 1,
        "policy": "sequential-logical-slices",
        "anchor": {"commit": anchor, "version": "4.4.0", "tag": "v4.4.0"},
        "overrides": [],
    }))
    git(repo, "add", ".localsetup-release.json")
    git(repo, "commit", "-qm", "chore: select release policy", "-m", "Release-Type: none")
    (repo / "first.py").write_text("FIRST = 1\n")
    git(repo, "add", "first.py")
    git(repo, "commit", "-qm", "fix: first published change")
    published = git(repo, "rev-parse", "HEAD")
    (repo / "second.py").write_text("SECOND = 2\n")
    git(repo, "add", "second.py")
    git(repo, "commit", "-qm", "fix: current release change")
    result = plan(repo, base=published)
    assert result["changed_paths"] == ["second.py"]
    assert [row["path"] for row in result["source_material"]] == ["second.py"]
    repair = plan(repo, base=published, repair=True)
    assert repair["target_version"] == "4.4.0"


def test_commit_evidence_must_precede_the_record_source(repo: Path) -> None:
    value = record(repo)
    (repo / "later.py").write_text("LATER = 1\n")
    git(repo, "add", "later.py")
    git(repo, "commit", "-qm", "fix: later unrelated change")
    value["highlights"][0]["evidence"] = [git(repo, "rev-parse", "HEAD")]
    outputs = render_outputs(repo, value)
    for relative, text in outputs.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    result = check(repo, value, target_version="4.4.1")
    assert any(item["code"] == "missing_evidence" for item in result["findings"])


def test_post_source_commits_allow_release_sync_but_reject_executable_changes(repo: Path) -> None:
    value = record(repo)
    for relative, text in render_outputs(repo, value).items():
        if relative in {"README.md", "ls/README.md"}:
            text = text.replace("**Version:** 4.4.0<br>", "**Version:** 4.4.1<br>")
        (repo / relative).write_text(text)
    (repo / "VERSION").write_text("4.4.1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "docs: checked release record and version")
    assert check(repo, value)["ok"]
    (repo / "ls/core/later.py").write_text("VALUE = 1\n")
    git(repo, "add", "ls/core/later.py")
    git(repo, "commit", "-qm", "fix: change executable after record")
    report = check(repo, value)
    assert any(item["code"] == "post_source_change" and item["path"] == "ls/core/later.py"
               for item in report["findings"])
    (repo / "pyproject.toml").write_text('version = "4.4.1"\nrequires-python = ">=3.13"\n')
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "-qm", "chore: change runtime configuration")
    report = check(repo, value)
    assert any(item["code"] == "post_source_change" and item["path"] == "pyproject.toml"
               for item in report["findings"])


def test_deleted_source_remains_evidence_of_a_release_removal(repo: Path) -> None:
    removed = "ls/skills/example/SKILL.md"
    (repo / removed).unlink()
    git(repo, "add", "--", removed)
    git(repo, "commit", "-qm", "fix: remove obsolete example")
    value = record(repo)
    value["highlights"][0]["evidence"] = [removed]
    for relative, text in render_outputs(repo, value).items():
        (repo / relative).write_text(text)
    assert check(repo, value, target_version=value["version"])["ok"]


def test_minor_version_sync_preserves_historical_release_guides(repo: Path) -> None:
    from ls.core.versioning_models import SemVer
    from ls.core.versioning_sync import update_doc_frontmatter_versions
    history = repo / "ls/docs/releases/4.3.0.md"
    original = "---\nstatus: ACTIVE\nversion: 4.3\nowner_skill: example\n---\n# Historical release\n"
    history.write_text(original)
    changed = update_doc_frontmatter_versions(repo, SemVer.parse("4.5.0"))
    assert history.read_text() == original
    assert "ls/docs/ACTIVE.md" in changed


def test_stale_verification_archive_version_is_not_hidden_by_rendering(repo: Path) -> None:
    value = record(repo)
    value["verification"] = ["Download localsetup-v4.4.0.tar.gz and its .sha256 and .cdx.json sidecars."]
    for relative, text in render_outputs(repo, value).items():
        (repo / relative).write_text(text)
    report = check(repo, value, target_version=value["version"])
    assert any(item["code"] == "artifact_version_mismatch" for item in report["findings"])
