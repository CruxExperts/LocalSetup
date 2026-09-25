import os
import subprocess
from pathlib import Path

import pytest

from ls.core import provenance, provenance_source
from ls.core.baseline import tracked_files
from ls.core.git_subprocess import GIT_ENV_TO_SCRUB
from ls.core.provenance import (
    MARKER_LEGACY,
    base_provenance,
    build_package_marker,
    load_package_marker,
    provenance_report,
    source_dirty,
    source_remote_url,
    source_tag,
)
from ls.core.lockfile import save_json
from ls.core.provenance_source import (
    generated_artifact_parent_source_commit,
    generated_docs_source_ref,
    is_generated_output_path,
    is_generated_receipt_path,
)


def clean_git_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in GIT_ENV_TO_SCRUB:
        env.pop(name, None)
    env.update(overrides)
    return env


def run(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=clean_git_env(),
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-q")
    run(repo, "config", "user.email", "test@example.invalid")
    run(repo, "config", "user.name", "Test User")
    (repo / "VERSION").write_text("4.9.0\n", encoding="utf-8")
    package = repo / "ls" / "skills" / "ls-demo"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\nname: ls-demo\n---\n", encoding="utf-8")
    run(repo, "add", ".")
    run(repo, "commit", "-q", "-m", "chore: initial")
    return repo


def test_receipt_stops_at_authored_refresh_subject(tmp_path: Path):
    repo = make_git_repo(tmp_path)
    (repo / "README.md").write_text("# Authored release guide\n")
    run(repo, "add", "README.md")
    run(repo, "commit", "-qm", "docs: refresh homepage")
    source = run(repo, "rev-parse", "HEAD")
    generated = repo / "ls/docs/_generated/facts.json"
    generated.parent.mkdir(parents=True)
    generated.write_text("{}\n")
    run(repo, "add", "ls/docs/_generated/facts.json")
    run(repo, "commit", "-qm", "docs: refresh generated receipt")
    assert generated_artifact_parent_source_commit(repo) == source
    assert generated_docs_source_ref(repo, "HEAD^") is None


def make_poison_index(tmp_path: Path) -> tuple[Path, Path]:
    alien = tmp_path / "alien"
    alien.mkdir()
    run(alien, "init", "-q")
    plugin_file = alien / ".agents" / "plugins" / "marketplace.json"
    plugin_file.parent.mkdir(parents=True)
    plugin_file.write_text('{"name": "plugin-marketplace"}\n', encoding="utf-8")
    poison_index = tmp_path / "poison.index"
    subprocess.run(
        ["git", "add", "-A"],
        cwd=alien,
        env=clean_git_env(GIT_INDEX_FILE=str(poison_index)),
        text=True,
        capture_output=True,
        check=True,
    )
    return poison_index, alien


def test_provenance_payload_clean_dirty_and_tagged_git_state(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    package = repo / "ls" / "skills" / "ls-demo"

    clean_marker = build_package_marker(
        repo,
        package,
        package_name="ls-demo",
        package_type="skill",
        source_path=package,
        emitter="test",
        installed_at=False,
    )
    assert clean_marker["framework_version"] == "4.9.0"
    assert clean_marker["source_dirty"] is False
    assert clean_marker["source_tag"] is None
    assert clean_marker["source_tree_sha"]
    assert clean_marker["package_digest"]
    assert clean_marker["source_provenance_hash"]

    run(repo, "tag", "v4.9.0")
    assert source_tag(repo) == "v4.9.0"
    tagged_marker = build_package_marker(
        repo,
        package,
        package_name="ls-demo",
        package_type="skill",
        source_path=package,
        emitter="test",
        installed_at=False,
    )
    assert tagged_marker["source_tag"] == "v4.9.0"

    (package / "SKILL.md").write_text("---\nname: ls-demo\ndescription: Dirty\n---\n", encoding="utf-8")
    assert source_dirty(repo) is True
    dirty_marker = build_package_marker(
        repo,
        package,
        package_name="ls-demo",
        package_type="skill",
        source_path=package,
        emitter="test",
        installed_at=False,
    )
    assert dirty_marker["source_dirty"] is True


def test_source_dirty_includes_untracked_package_sources(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    package = repo / "ls" / "skills" / "ls-demo"
    (package / "UNTRACKED.txt").write_text("included in copy\n", encoding="utf-8")

    marker = build_package_marker(
        repo,
        package,
        package_name="ls-demo",
        package_type="skill",
        source_path=package,
        emitter="test",
        installed_at=False,
    )

    assert marker["source_dirty"] is True


def test_source_dirty_ignores_generated_doc_outputs(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    generated_files = [
        repo / "ls" / "docs" / "SKILLS.md",
        repo / "ls" / "docs" / "WORKFLOW_REGISTRY.md",
        repo / "ls" / "docs" / "_generated" / "facts.json",
        repo / "ls" / "docs" / "_generated" / "skill-taxonomy.json",
        repo / "ls" / "docs" / "migration" / "skill-alias-map.md",
        repo / "assets" / "README.md",
    ]
    for path in generated_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n", encoding="utf-8")

    assert source_dirty(repo) is False

    (repo / "VERSION").write_text("3.9.1\n", encoding="utf-8")

    assert source_dirty(repo) is True


def test_source_dirty_ignores_only_existing_facts_block_updates(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt_paths = [
        repo / "README.md",
        repo / "ls" / "docs" / "README.md",
        repo / "ls" / "docs" / "FEATURES.md",
    ]
    for path in receipt_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Document\n\n"
            "<!-- facts-block:start -->\n"
            "- Current version: `4.9.0`\n"
            "<!-- facts-block:end -->\n\n"
            "Manual content.\n",
            encoding="utf-8",
        )
    run(repo, "add", *(str(path.relative_to(repo)) for path in receipt_paths))
    run(repo, "commit", "-q", "-m", "docs: add facts block documents")

    for path in receipt_paths:
        path.write_text(
            path.read_text(encoding="utf-8").replace("`4.9.0`", "`4.9.1`"),
            encoding="utf-8",
        )
    assert source_dirty(repo) is False
    run(repo, "add", *(str(path.relative_to(repo)) for path in receipt_paths))
    assert source_dirty(repo) is False


    root_readme = receipt_paths[0]
    root_readme.write_text(
        root_readme.read_text(encoding="utf-8").replace("Manual content.", "Manual source edit."),
        encoding="utf-8",
    )
    assert source_dirty(repo) is True



def test_source_dirty_ignores_facts_block_only_line_insertion(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace(
            "<!-- facts-block:start -->\n",
            "<!-- facts-block:start -->\n- Current version: `4.9.1`\n",
        ),
        encoding="utf-8",
    )

    assert source_dirty(repo) is False


@pytest.mark.parametrize(
    "diff",
    [
        "@@ -3 +3 @@\n-old\n+new\n-Manual content.\n+Manual source edit.\n",
        "@@ -3,2 +3,2 @@\n-old\n+new\n",
        "@@ -3 +3 @@@\n-old\n+new\n",
        f"@@ -{'9' * 5_000} +3 @@\n-old\n+new\n",
    ],
)
def test_facts_block_hunk_parser_rejects_mismatched_body_ranges(diff: str) -> None:
    before = [
        "# Document",
        provenance_source.FACTS_BLOCK_START,
        "old",
        provenance_source.FACTS_BLOCK_END,
        "Manual content.",
    ]
    after = [
        "# Document",
        provenance_source.FACTS_BLOCK_START,
        "new",
        provenance_source.FACTS_BLOCK_END,
        "Manual source edit.",
    ]

    assert not provenance_source._diff_hunks_are_within_facts_block(diff, before, after)


def test_facts_block_hunk_parser_accepts_section_header() -> None:
    before = [
        "# Document",
        provenance_source.FACTS_BLOCK_START,
        "old",
        provenance_source.FACTS_BLOCK_END,
        "Manual content.",
    ]
    after = [
        "# Document",
        provenance_source.FACTS_BLOCK_START,
        "new",
        provenance_source.FACTS_BLOCK_END,
        "Manual content.",
    ]

    assert provenance_source._diff_hunks_are_within_facts_block(
        "@@ -3 +3 @@ section heading\n-old\n+new\n",
        before,
        after,
    )


def test_source_dirty_includes_non_lf_separator_outside_facts_block(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n"
        "<!-- facts-block:start -->\n"
        "- Fact: `a\vb\vc`\n"
        "<!-- facts-block:end -->\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("Manual content.", "Manual source edit."),
        encoding="utf-8",
    )

    assert source_dirty(repo) is True




def test_source_dirty_ignores_non_lf_separator_in_facts_block(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n"
        "<!-- facts-block:start -->\n"
        "- Fact: `a\v@@ -99 +99 @@`\n"
        "<!-- facts-block:end -->\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("`a\v@@ -99 +99 @@`", "`b\v@@ -99 +99 @@`"),
        encoding="utf-8",
    )

    assert source_dirty(repo) is False


def test_source_dirty_ignores_facts_block_update_with_forced_diff_color(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    run(repo, "config", "color.diff", "always")
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("`4.9.0`", "`4.9.1`"),
        encoding="utf-8",
    )

    assert source_dirty(repo) is False


def test_source_dirty_forces_canonical_diff_indicators(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    run(repo, "config", "diff.outputIndicatorOld", "!")
    run(repo, "config", "diff.outputIndicatorNew", "?")
    run(repo, "config", "diff.outputIndicatorContext", ".")
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("`4.9.0`", "`4.9.1`"),
        encoding="utf-8",
    )

    assert source_dirty(repo) is False


def test_source_dirty_ignores_crlf_facts_block_update(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    run(repo, "config", "core.autocrlf", "false")
    receipt = repo / "README.md"
    receipt.write_bytes(
        b"# Document\r\n"
        b"\r\n"
        b"<!-- facts-block:start -->\r\n"
        b"- Current version: `4.9.0`\r\n"
        b"<!-- facts-block:end -->\r\n"
        b"\r\n"
        b"Manual content.\r\n"
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_bytes(receipt.read_bytes().replace(b"`4.9.0`", b"`4.9.1`"))

    assert source_dirty(repo) is False






def test_source_dirty_includes_manual_change_hidden_by_textconv(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    converter = tmp_path / "receipt-textconv.py"
    converter.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():\n"
        "    print(line if line.startswith('- Current version:') else 'constant')\n",
        encoding="utf-8",
    )
    converter.chmod(0o755)
    run(repo, "config", "diff.receipt.textconv", str(converter))
    (repo / ".gitattributes").write_text("README.md diff=receipt\n", encoding="utf-8")
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", ".gitattributes", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8")
        .replace("`4.9.0`", "`4.9.1`")
        .replace("Manual content.", "Manual source edit."),
        encoding="utf-8",
    )

    assert source_dirty(repo) is True


def test_source_dirty_includes_staged_manual_receipt_edit_with_facts_only_worktree(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("Manual content.", "Manual source edit."),
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    receipt.write_text(
        receipt.read_text(encoding="utf-8")
        .replace("`4.9.0`", "`4.9.1`")
        .replace("Manual source edit.", "Manual content."),
        encoding="utf-8",
    )

    assert run(repo, "status", "--short") == "MM README.md"
    assert source_dirty(repo) is True


def test_source_dirty_includes_mode_only_receipt_change(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.chmod(0o755)

    assert source_dirty(repo) is True


def test_source_dirty_includes_mode_change_with_facts_block_update(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("`4.9.0`", "`4.9.1`"),
        encoding="utf-8",
    )
    receipt.chmod(0o755)

    assert source_dirty(repo) is True
    run(repo, "add", "README.md")
    assert source_dirty(repo) is True


def test_source_dirty_includes_worktree_mode_change_with_staged_facts_update(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("`4.9.0`", "`4.9.1`"),
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("`4.9.1`", "`4.9.0`"),
        encoding="utf-8",
    )
    receipt.chmod(0o755)

    assert run(repo, "status", "--short") == "MM README.md"
    assert source_dirty(repo) is True


def test_source_dirty_includes_symlink_receipt_with_facts_update(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    target = tmp_path / "receipt-target.md"
    target.write_text(
        receipt.read_text(encoding="utf-8").replace("`4.9.0`", "`4.9.1`"),
        encoding="utf-8",
    )
    receipt.unlink()
    receipt.symlink_to(target)

    assert source_dirty(repo) is True



def test_source_dirty_includes_undecodable_staged_receipt(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    original_text = receipt.read_text(encoding="utf-8")
    receipt.write_bytes(b"\xff")
    run(repo, "add", "README.md")
    receipt.write_text(original_text, encoding="utf-8")

    assert source_dirty(repo) is True


def test_source_dirty_ignores_autocrlf_line_endings_with_facts_update(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    run(repo, "config", "core.autocrlf", "true")
    (repo / ".gitattributes").write_text("README.md text\n", encoding="utf-8")
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", ".gitattributes", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")
    receipt.unlink()
    run(repo, "checkout", "--", "README.md")
    assert b"\r\n" in receipt.read_bytes()

    receipt.write_bytes(receipt.read_bytes().replace(b"`4.9.0`", b"`4.9.1`"))

    assert source_dirty(repo) is False


def test_source_dirty_includes_git_visible_line_ending_change_with_facts_update(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    run(repo, "config", "core.autocrlf", "false")
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_bytes(
        receipt.read_bytes()
        .replace(b"`4.9.0`", b"`4.9.1`")
        .replace(b"\n", b"\r\n")
    )

    assert source_dirty(repo) is True


def test_source_dirty_uses_lf_boundaries_for_lone_carriage_returns(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_bytes(
        b"# Title\rSubtitle\n"
        b"<!-- facts-block:start -->\n"
        b"- Current version: `4.9.0`\n"
        b"<!-- facts-block:end -->\n"
        b"Manual content.\n"
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")

    receipt.write_bytes(
        receipt.read_bytes().replace(b"`4.9.0`", b"`4.9.1`"),
    )
    assert source_dirty(repo) is False

    receipt.write_bytes(
        receipt.read_bytes().replace(b"Manual content.", b"Manual source edit."),
    )
    assert source_dirty(repo) is True






@pytest.mark.parametrize(
    "probe_error",
    [
        OSError("metadata probe failed"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
)
def test_source_dirty_fails_closed_when_receipt_metadata_probe_errors(
    tmp_path: Path,
    monkeypatch,
    probe_error: OSError | UnicodeDecodeError,
) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    receipt.write_text(
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n",
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt")
    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("`4.9.0`", "`4.9.1`"),
        encoding="utf-8",
    )

    original_run_git = provenance.run_git

    def raise_for_metadata_probe(repo_root: Path, args: list[str], **kwargs: object) -> object:
        if args[0] == "diff" and "--summary" in args:
            raise probe_error
        return original_run_git(repo_root, args, **kwargs)

    monkeypatch.setattr(provenance, "run_git", raise_for_metadata_probe)

    assert source_dirty(repo) is True


@pytest.mark.parametrize(
    "probe_failure",
    [
        "nonzero",
        OSError("status probe failed"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
)
def test_source_dirty_fails_closed_when_status_probe_fails(
    tmp_path: Path,
    monkeypatch,
    probe_failure: str | OSError | UnicodeDecodeError,
) -> None:
    repo = make_git_repo(tmp_path)
    original_run_git = provenance.run_git

    def fail_status_probe(repo_root: Path, args: list[str], **kwargs: object) -> object:
        if args[0] != "status":
            return original_run_git(repo_root, args, **kwargs)
        if probe_failure == "nonzero":
            return subprocess.CompletedProcess(args, 1, "", "fatal: status failed")
        raise probe_failure

    monkeypatch.setattr(provenance, "run_git", fail_status_probe)

    assert source_dirty(repo) is True


def test_repo_git_probes_ignore_inherited_scratch_index(tmp_path: Path, monkeypatch) -> None:
    repo = make_git_repo(tmp_path)
    package = repo / "ls" / "skills" / "ls-demo"
    expected_commit = run(repo, "rev-parse", "HEAD")
    expected_tree = run(repo, "rev-parse", "HEAD^{tree}")
    poison_index, alien = make_poison_index(tmp_path)

    raw_ls_files = subprocess.run(
        ["git", "ls-files", "--cached"],
        cwd=repo,
        env=clean_git_env(GIT_INDEX_FILE=str(poison_index)),
        text=True,
        capture_output=True,
        check=True,
    )
    assert ".agents/plugins/marketplace.json" in raw_ls_files.stdout

    monkeypatch.setenv("GIT_INDEX_FILE", str(poison_index))
    monkeypatch.setenv("GIT_DIR", str(alien / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(alien))

    marker = build_package_marker(
        repo,
        package,
        package_name="ls-demo",
        package_type="skill",
        source_path=package,
        emitter="test",
        installed_at=False,
    )

    assert marker["source_commit"] == expected_commit
    assert marker["source_tree_sha"] == expected_tree
    assert marker["source_dirty"] is False
    assert "VERSION" in tracked_files(repo)
    assert ".agents/plugins/marketplace.json" not in tracked_files(repo)


def test_source_remote_url_is_normalized_for_ci_parity(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)

    run(repo, "remote", "add", "origin", "https://github.com/CruxExperts/localsetup.git")
    assert source_remote_url(repo) == "https://github.com/CruxExperts/localsetup"

    run(repo, "remote", "set-url", "origin", "git@github.com:CruxExperts/localsetup.git")
    assert source_remote_url(repo) == "https://github.com/CruxExperts/localsetup"


def test_generated_artifact_provenance_uses_parent_for_generated_commits(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    generated = repo / "ls" / "docs" / "_generated" / "facts.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    parent = run(repo, "rev-parse", "HEAD")

    generated.write_text("{}\n", encoding="utf-8")
    run(repo, "add", str(generated.relative_to(repo)))
    run(repo, "commit", "-q", "-m", "docs: refresh generated artifacts")

    normal = base_provenance(repo, emitter="test")
    generated_mode = base_provenance(repo, emitter="test", generated_commit_parent=True)

    assert normal["source_commit"] == run(repo, "rev-parse", "HEAD")
    assert normal["source_dirty"] is False
    assert generated_mode["source_commit"] == parent
    assert generated_mode["source_dirty"] is False


def test_generated_artifact_provenance_walks_generated_commit_chain(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    generated = repo / "ls" / "docs" / "_generated" / "facts.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    source = run(repo, "rev-parse", "HEAD")

    generated.write_text('{"step": 1}\n', encoding="utf-8")
    run(repo, "add", str(generated.relative_to(repo)))
    run(repo, "commit", "-q", "-m", "docs: refresh generated artifacts")

    generated.write_text('{"step": 2}\n', encoding="utf-8")
    run(repo, "add", str(generated.relative_to(repo)))
    run(repo, "commit", "-q", "-m", "docs: refresh generated provenance")

    generated_mode = base_provenance(repo, emitter="test", generated_commit_parent=True)

    assert generated_mode["source_commit"] == source
    assert generated_mode["source_dirty"] is False


def test_generated_receipt_provenance_uses_release_source_and_keeps_root_readme_dirty(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    receipt_paths = [
        repo / "README.md",
        repo / "ls" / "docs" / "README.md",
        repo / "ls" / "docs" / "FEATURES.md",
    ]
    for path in receipt_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Document\n\n"
            "<!-- facts-block:start -->\n"
            "- Current version: `4.9.0`\n"
            "<!-- facts-block:end -->\n\n"
            "Manual content.\n",
            encoding="utf-8",
        )
    run(repo, "add", *(str(path.relative_to(repo)) for path in receipt_paths))
    run(repo, "commit", "-q", "-m", "docs: add receipt templates")
    release_source = run(repo, "rev-parse", "HEAD")
    release_source_tree = run(repo, "rev-parse", "HEAD^{tree}")

    (repo / "VERSION").write_text("4.9.1\n", encoding="utf-8")
    run(repo, "add", "VERSION")
    run(repo, "commit", "-q", "-m", "chore: sync release version 4.9.1")

    for path in receipt_paths:
        path.write_text(
            path.read_text(encoding="utf-8").replace("`4.9.0`", "`4.9.1`"),
            encoding="utf-8",
        )
    run(repo, "add", *(str(path.relative_to(repo)) for path in receipt_paths))
    run(repo, "commit", "-q", "-m", "docs: refresh release version artifacts")

    assert generated_docs_source_ref(repo, "HEAD") == release_source
    generated_mode = base_provenance(repo, emitter="test", generated_commit_parent=True)
    assert generated_mode["source_commit"] == release_source
    assert generated_mode["source_tree_sha"] == release_source_tree
    assert generated_mode["source_dirty"] is True

    (repo / "README.md").write_text("manual source edit\n", encoding="utf-8")
    assert source_dirty(repo) is True



def test_generated_receipt_provenance_rejects_manual_receipt_refresh(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    initial = (
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n"
    )
    receipt.write_text(initial, encoding="utf-8")
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt template")

    receipt.write_text(
        initial.replace("Manual content.", "Manual source edit."),
        encoding="utf-8",
    )
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: refresh manual receipt")

    assert generated_docs_source_ref(repo, "HEAD") is None


@pytest.mark.parametrize(
    "replacement",
    [
        ("<!-- facts-block:end -->", ""),
        (
            "<!-- facts-block:end -->",
            "<!-- facts-block:end -->\n<!-- facts-block:start -->",
        ),
    ],
    ids=["missing-end", "duplicate-start"],
)
def test_generated_receipt_provenance_rejects_malformed_refresh(
    tmp_path: Path,
    replacement: tuple[str, str],
) -> None:
    repo = make_git_repo(tmp_path)
    receipt = repo / "README.md"
    initial = (
        "# Document\n\n"
        "<!-- facts-block:start -->\n"
        "- Current version: `4.9.0`\n"
        "<!-- facts-block:end -->\n\n"
        "Manual content.\n"
    )
    receipt.write_text(initial, encoding="utf-8")
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: add receipt template")

    receipt.write_text(initial.replace(*replacement), encoding="utf-8")
    run(repo, "add", "README.md")
    run(repo, "commit", "-q", "-m", "docs: refresh malformed receipt")

    assert generated_docs_source_ref(repo, "HEAD") is None


def test_generated_artifact_provenance_uses_dirty_parent_for_release_sync(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    parent = run(repo, "rev-parse", "HEAD")
    parent_tree = run(repo, "rev-parse", "HEAD^{tree}")

    (repo / "VERSION").write_text("3.9.1\n", encoding="utf-8")
    generated = repo / "ls" / "docs" / "SKILLS.md"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("generated for release\n", encoding="utf-8")
    run(repo, "add", "VERSION", str(generated.relative_to(repo)))
    run(repo, "commit", "-q", "-m", "chore: sync release version 3.9.1")

    normal = base_provenance(repo, emitter="test")
    generated_mode = base_provenance(repo, emitter="test", generated_commit_parent=True)

    assert normal["source_commit"] == run(repo, "rev-parse", "HEAD")
    assert normal["source_dirty"] is False
    assert generated_mode["source_commit"] == parent
    assert generated_mode["source_tree_sha"] == parent_tree
    assert generated_mode["source_dirty"] is True


def test_generated_artifact_provenance_skips_generated_refresh_before_release_sync(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    (repo / "ls" / "skills" / "ls-demo" / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Updated\n---\n",
        encoding="utf-8",
    )
    run(repo, "add", ".")
    run(repo, "commit", "-q", "-m", "feat: update runtime")
    source = run(repo, "rev-parse", "HEAD")

    generated = repo / "ls" / "docs" / "_generated" / "facts.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("{}\n", encoding="utf-8")
    run(repo, "add", ".")
    run(repo, "commit", "-q", "-m", "docs: refresh generated artifacts")

    (repo / "VERSION").write_text("4.9.1\n", encoding="utf-8")
    run(repo, "add", "VERSION")
    run(repo, "commit", "-q", "-m", "chore: sync release version 4.9.1")

    assert generated_docs_source_ref(repo, "HEAD^") == source
    assert generated_artifact_parent_source_commit(repo) == source


def test_generated_artifact_provenance_uses_release_sync_source_for_pr_merge_commit(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")

    (repo / "ls" / "skills" / "ls-demo" / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Updated\n---\n",
        encoding="utf-8",
    )
    run(repo, "add", "ls/skills/ls-demo/SKILL.md")
    run(repo, "commit", "-q", "-m", "feat!: update runtime\n\nRelease-Type: major")
    source = run(repo, "rev-parse", "HEAD")
    source_tree = run(repo, "rev-parse", "HEAD^{tree}")

    (repo / "VERSION").write_text("4.0.0\n", encoding="utf-8")
    generated = repo / "ls" / "docs" / "_generated" / "facts.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("{}\n", encoding="utf-8")
    run(repo, "add", "VERSION", str(generated.relative_to(repo)))
    run(repo, "commit", "-q", "-m", "chore: sync release version 4.0.0")
    release_sync = run(repo, "rev-parse", "HEAD")
    merge_tree = run(repo, "rev-parse", f"{release_sync}^{{tree}}")
    merge = run(
        repo,
        "commit-tree",
        merge_tree,
        "-p",
        base,
        "-p",
        release_sync,
        "-m",
        f"Merge {release_sync} into {base}",
    )
    run(repo, "reset", "--hard", merge)

    generated_mode = base_provenance(repo, emitter="test", generated_commit_parent=True)

    assert generated_mode["source_commit"] == source
    assert generated_mode["source_tree_sha"] == source_tree
    assert generated_mode["source_dirty"] is True



def test_generated_artifact_provenance_uses_generated_refresh_source_for_pr_merge_commit(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")

    (repo / "ls" / "skills" / "ls-demo" / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Corrected\n---\n",
        encoding="utf-8",
    )
    run(repo, "add", "ls/skills/ls-demo/SKILL.md")
    run(repo, "commit", "-q", "-m", "fix: correct release source")
    source = run(repo, "rev-parse", "HEAD")
    source_tree = run(repo, "rev-parse", "HEAD^{tree}")

    generated = repo / "ls" / "docs" / "_generated" / "facts.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("{}\n", encoding="utf-8")
    run(repo, "add", str(generated.relative_to(repo)))
    run(repo, "commit", "-q", "-m", "docs: refresh release version artifacts")
    generated_refresh = run(repo, "rev-parse", "HEAD")
    merge_tree = run(repo, "rev-parse", f"{generated_refresh}^{{tree}}")
    merge = run(
        repo,
        "commit-tree",
        merge_tree,
        "-p",
        base,
        "-p",
        generated_refresh,
        "-m",
        f"Merge {generated_refresh} into {base}",
    )
    run(repo, "reset", "--hard", merge)

    generated_mode = base_provenance(repo, emitter="test", generated_commit_parent=True)

    assert generated_mode["source_commit"] == source
    assert generated_mode["source_tree_sha"] == source_tree
    assert generated_mode["source_dirty"] is False


def test_generated_artifact_provenance_does_not_use_generated_first_parent_for_main_into_goal_merge(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")

    (repo / "ls" / "skills" / "ls-demo" / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Updated\n---\n",
        encoding="utf-8",
    )
    run(repo, "add", "ls/skills/ls-demo/SKILL.md")
    run(repo, "commit", "-q", "-m", "feat: update source")
    generated = repo / "ls" / "docs" / "_generated" / "facts.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("{}\n", encoding="utf-8")
    run(repo, "add", str(generated.relative_to(repo)))
    run(repo, "commit", "-q", "-m", "docs: refresh generated artifacts")
    generated_refresh = run(repo, "rev-parse", "HEAD")

    run(repo, "branch", "main", base)
    run(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main advance\n", encoding="utf-8")
    run(repo, "add", "main.txt")
    run(repo, "commit", "-q", "-m", "fix: advance main")
    main_advance = run(repo, "rev-parse", "HEAD")

    run(repo, "checkout", "-q", "-b", "goal", generated_refresh)
    run(repo, "merge", "--no-ff", "--no-edit", "main")

    assert run(repo, "rev-parse", "HEAD^1") == generated_refresh
    assert run(repo, "rev-parse", "HEAD^2") == main_advance
    assert generated_artifact_parent_source_commit(repo) is None

    generated_mode = base_provenance(repo, emitter="test", generated_commit_parent=True)

    assert generated_mode["source_commit"] == run(repo, "rev-parse", "HEAD")
    assert generated_mode["source_tree_sha"] == run(repo, "rev-parse", "HEAD^{tree}")
    assert generated_mode["source_dirty"] is False


def test_generated_artifact_provenance_does_not_use_generated_first_parent_for_ordinary_second_parent_merge(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")

    generated = repo / "ls" / "docs" / "_generated" / "facts.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("{}\n", encoding="utf-8")
    run(repo, "add", str(generated.relative_to(repo)))
    run(repo, "commit", "-q", "-m", "docs: refresh generated artifacts")
    generated_refresh = run(repo, "rev-parse", "HEAD")

    run(repo, "branch", "feature", base)
    run(repo, "checkout", "-q", "feature")
    (repo / "feature.txt").write_text("ordinary source change\n", encoding="utf-8")
    run(repo, "add", "feature.txt")
    run(repo, "commit", "-q", "-m", "feat: ordinary source change")
    ordinary_feature = run(repo, "rev-parse", "HEAD")

    run(repo, "checkout", "-q", "-b", "goal", generated_refresh)
    run(repo, "merge", "--no-ff", "--no-edit", "feature")

    assert run(repo, "rev-parse", "HEAD^1") == generated_refresh
    assert run(repo, "rev-parse", "HEAD^2") == ordinary_feature
    assert generated_artifact_parent_source_commit(repo) is None

    generated_mode = base_provenance(repo, emitter="test", generated_commit_parent=True)

    assert generated_mode["source_commit"] == run(repo, "rev-parse", "HEAD")
    assert generated_mode["source_tree_sha"] == run(repo, "rev-parse", "HEAD^{tree}")
    assert generated_mode["source_dirty"] is False


def test_package_marker_loads_json_and_legacy_text(tmp_path: Path) -> None:
    package = tmp_path / "ls-demo"
    package.mkdir()
    save_json(package / ".localsetup-managed.json", {"schema_version": 1, "package_name": "ls-demo"})
    assert load_package_marker(package)["package_name"] == "ls-demo"

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / MARKER_LEGACY).write_text("source=localsetup-context\n", encoding="utf-8")
    marker = load_package_marker(legacy)
    assert marker["schema_version"] == 0
    assert marker["legacy_marker"] is True
    assert marker["source"] == "source=localsetup-context"


def test_package_digest_ignores_runtime_bytecode_but_detects_real_changes(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    global_root = tmp_path / "global"
    package = global_root / "ls-demo"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\nname: ls-demo\n---\n", encoding="utf-8")
    source = package / "scripts.py"
    source.write_text("print('source-v1')\n", encoding="utf-8")
    hidden = package / ".metadata"
    hidden.write_text("hidden-v1\n", encoding="utf-8")

    marker = build_package_marker(
        repo,
        package,
        package_name="ls-demo",
        package_type="skill",
        source_path=package,
        emitter="test",
        installed_at=False,
    )
    save_json(package / ".localsetup-managed.json", marker)
    digest = marker["package_digest"]
    lock = {"package_provenance": {"ls-demo": {"package_digest": digest}}}
    registry = {"packages": {"ls-demo": {"digest": digest}}}

    cache = package / "nested" / "__pycache__"
    cache.mkdir(parents=True)
    cache_bytecode = cache / "scripts.cpython-312.pyc"
    cache_bytecode.write_bytes(b"nested-bytecode")
    cache_data = cache / "runtime-data.txt"
    cache_data.write_bytes(b"nested-cache-data")
    standalone_pyc = package / "standalone.pyc"
    standalone_pyc.write_bytes(b"standalone-bytecode")
    standalone_pyo = package / "standalone.pyo"
    standalone_pyo.write_bytes(b"optimized-bytecode")

    matching = provenance_report(repo, lock=lock, registry=registry, global_root=global_root, adapters=[])
    assert matching["warnings"] == []
    assert matching["packages"]["ls-demo"]["lock_digest"] == digest
    assert matching["packages"]["ls-demo"]["global_digest"] == digest
    assert source.is_file()
    assert hidden.is_file()
    assert cache_bytecode.read_bytes() == b"nested-bytecode"
    assert cache_data.read_bytes() == b"nested-cache-data"
    assert standalone_pyc.read_bytes() == b"standalone-bytecode"
    assert standalone_pyo.read_bytes() == b"optimized-bytecode"

    source.write_text("print('source-v2')\n", encoding="utf-8")
    stale_source = provenance_report(repo, lock=lock, registry=registry, global_root=global_root, adapters=[])
    assert "target lock references stale package digest for ls-demo" in stale_source["warnings"]

    source.write_text("print('source-v1')\n", encoding="utf-8")
    hidden.write_text("hidden-v2\n", encoding="utf-8")
    stale_hidden = provenance_report(repo, lock=lock, registry=registry, global_root=global_root, adapters=[])
    assert "target lock references stale package digest for ls-demo" in stale_hidden["warnings"]


def test_package_digest_ignores_missing_and_bytecode_only_packages(tmp_path: Path) -> None:
    assert provenance.package_digest(tmp_path / "missing") is None

    package = tmp_path / "bytecode-only"
    package.mkdir()
    save_json(package / ".localsetup-managed.json", {"schema_version": 1})
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"bytecode")
    (package / "module.pyo").write_bytes(b"optimized-bytecode")
    assert provenance.package_digest(package) is None
