import json
from pathlib import Path

import ls.core.cli as cli_mod
from ls.core.skills import validate_candidate_skill


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_candidate_skill_validate_json_read_only(tmp_path: Path, capsys) -> None:
    root = _repo_root()
    home = tmp_path / "home"
    home.mkdir(parents=True)

    candidate_dir = tmp_path / "candidate" / "ls-example"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "SKILL.md").write_text(
        "---\nname: ls-example\ndescription: Example candidate\n---\n\nBody text.\n",
        encoding="utf-8",
    )

    # Guard against accidental write paths in this command.
    forbidden = {
        root / "ls" / "skills",
        home / ".local" / "share" / "localsetup" / "source",
        home / ".local" / "share" / "localsetup" / "packages",
        root / ".codex" / "skills",
        root / ".claude" / "skills",
        root / ".cursor" / "skills",
        root / ".kilo" / "skills",
        root / ".openclaw" / "skills",
        root / ".opencode" / "skills",
        root / ".agents" / "skills",
    }
    original_write_text = Path.write_text

    def guarded_write_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        resolved = self.expanduser().resolve()
        for blocked_root in forbidden:
            blocked = blocked_root.expanduser().resolve()
            if resolved == blocked or blocked in resolved.parents:
                raise AssertionError(f"unexpected write into managed/adapter root: {resolved}")
        return original_write_text(self, *args, **kwargs)

    # We patch the class method for the duration of the CLI call only.
    Path.write_text = guarded_write_text  # type: ignore[assignment]
    try:
        code = cli_mod._main(
            [
                "--source-root",
                str(root),
                "--home",
                str(home),
                "candidate-skill",
                "validate",
                "--candidate",
                str(candidate_dir),
                "--json",
            ]
        )
    finally:
        Path.write_text = original_write_text  # type: ignore[assignment]

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["candidate"]["name"] == "ls-example"
    assert payload["candidate"]["description"] == "Example candidate"
    # Safety findings are references only.
    for finding in payload["safety"]["findings"]:
        assert "file" in finding
        assert "line" in finding
        assert "col" in finding
        assert "pattern_id" in finding
        assert "description" in finding
        assert "matched" not in finding


def test_candidate_skill_proposal_stdout_and_managed_path_blocking(tmp_path: Path, capsys) -> None:
    root = _repo_root()

    managed_candidate = root / "ls" / "skills" / "ls-localsetup-project-context"

    code = cli_mod._main(
        [
            "--source-root",
            str(root),
            "candidate-skill",
            "proposal",
            "--candidate",
            str(managed_candidate),
            "--output",
            "-",
        ]
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "# Candidate skill proposal" in output
    assert "Validation: blocked" in output
    assert "inside managed LocalSetup content" in output


def test_candidate_skill_proposal_output_blocks_adapter_path(tmp_path: Path, capsys) -> None:
    root = _repo_root()
    candidate_dir = tmp_path / "candidate" / "ls-example"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "SKILL.md").write_text(
        "---\nname: ls-example\ndescription: Example candidate\n---\n\nBody text.\n",
        encoding="utf-8",
    )
    output = root / ".codex" / "skills" / "proposal.md"

    code = cli_mod._main(
        [
            "--source-root",
            str(root),
            "candidate-skill",
            "proposal",
            "--candidate",
            str(candidate_dir),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "output path blocked" in captured.err
    assert not output.exists()


def test_candidate_skill_validate_blocks_adapter_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".codex" / "skills" / "ls-adapter-only").mkdir(parents=True)
    (root / ".codex" / "skills" / "ls-adapter-only" / "SKILL.md").write_text(
        "---\nname: ls-adapter-only\ndescription: Adapter path candidate\n---\n",
        encoding="utf-8",
    )

    payload = validate_candidate_skill(root, root / ".codex" / "skills" / "ls-adapter-only")

    assert payload["ok"] is False
    blockers = payload["validation"]["blockers"]
    assert any("inside an adapter skill directory" in item for item in blockers)


def test_candidate_skill_validate_reports_malformed_frontmatter(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    candidate = root / "docs" / "localsetup" / "skills" / "ls-bad"
    candidate.mkdir(parents=True)
    (candidate / "SKILL.md").write_text("---\nname: [unterminated\n---\n", encoding="utf-8")

    payload = validate_candidate_skill(root, candidate)

    assert payload["ok"] is False
    assert any("frontmatter is invalid YAML" in item for item in payload["validation"]["blockers"])
