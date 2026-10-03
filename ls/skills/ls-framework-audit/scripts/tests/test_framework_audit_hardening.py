from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_ROOT))

import run_framework_audit as audit  # noqa: E402


def test_sanitize_output_path_rejects_control_characters() -> None:
    try:
        audit._sanitize_output_path("/tmp/audit\nreport.md")
    except ValueError as exc:
        assert "control characters" in str(exc)
    else:
        raise AssertionError("expected ValueError for control character path")


def test_sanitize_output_path_rejects_long_component(tmp_path: Path) -> None:
    long_name = "a" * (audit.PATH_COMPONENT_MAX + 1)

    try:
        audit._sanitize_output_path(str(tmp_path / long_name))
    except ValueError as exc:
        assert "path component too long" in str(exc)
    else:
        raise AssertionError("expected ValueError for long path component")


def test_format_subprocess_failure_preserves_stdout_and_stderr() -> None:
    result = subprocess.CompletedProcess(
        args=["run_smoke.py"],
        returncode=7,
        stdout="first line\nsecond line\n",
        stderr="traceback-ish context\n",
    )

    message = audit._format_subprocess_failure("Skill matrix ls-example: smoke", result)

    assert "Skill matrix ls-example: smoke failed (exit 7)" in message
    assert "stdout:" in message
    assert "first line" in message
    assert "second line" in message
    assert "stderr:" in message
    assert "traceback-ish context" in message


def test_report_items_indent_multiline_evidence() -> None:
    lines: list[str] = []

    audit._append_report_items(lines, ["smoke failed\nstdout:\nboom"])

    assert lines == ["- smoke failed", "  stdout:", "  boom"]


def test_read_facts_version_uses_top_level_version(tmp_path: Path) -> None:
    facts = tmp_path / "ls" / "docs" / "_generated" / "facts.json"
    facts.parent.mkdir(parents=True)
    facts.write_text(
        dedent(
            """\
            {
              "skills": [{"id": "ls-example", "version": "1.0"}],
              "version": "3.8.6"
            }
            """
        ),
        encoding="utf-8",
    )

    assert audit._read_facts_version(tmp_path) == audit.FactsVersionRead(
        "valid", version="3.8.6"
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{", "malformed JSON"),
        ("[]", "top level must be a JSON object"),
        ("{}", "top-level version must be a non-empty string"),
        ('{"version": ""}', "top-level version must be a non-empty string"),
        ('{"version": 4}', "top-level version must be a non-empty string"),
    ],
)
def test_read_facts_version_rejects_existing_invalid_payloads(
    tmp_path: Path, payload: str, message: str
) -> None:
    facts = tmp_path / "ls" / "docs" / "_generated" / "facts.json"
    facts.parent.mkdir(parents=True)
    facts.write_text(payload, encoding="utf-8")

    result = audit._read_facts_version(tmp_path)

    assert result.state == "invalid"
    assert result.error is not None
    assert message in result.error


def test_read_facts_version_distinguishes_absent_and_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert audit._read_facts_version(tmp_path).state == "absent"
    facts = tmp_path / "ls" / "docs" / "_generated" / "facts.json"
    facts.parent.mkdir(parents=True)
    facts.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_facts_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == facts:
            raise OSError("fixture read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_facts_read)
    result = audit._read_facts_version(tmp_path)

    assert result.state == "invalid"
    assert result.error is not None
    assert "OSError: fixture read failure" in result.error


def test_version_facts_warns_only_when_facts_are_absent(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("3.8.6\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("**Version:** 3.8.6\n", encoding="utf-8")

    errors, warnings = audit.phase_version_facts(tmp_path)
    assert errors == []
    assert warnings == ["facts.json missing; version/facts comparison partial"]

    facts = tmp_path / "ls" / "docs" / "_generated" / "facts.json"
    facts.parent.mkdir(parents=True)
    facts.write_text("not json", encoding="utf-8")
    errors, warnings = audit.phase_version_facts(tmp_path)
    assert warnings == []
    assert len(errors) == 1
    assert errors[0].startswith("facts.json invalid: malformed JSON")


def test_framework_root_resolves_installed_package_source_layout(tmp_path: Path) -> None:
    installed_script_dir = tmp_path / "localsetup" / "packages" / "ls-framework-audit" / "scripts"
    installed_script_dir.mkdir(parents=True)
    framework = tmp_path / "localsetup" / "source" / "ls"
    (framework / "lib").mkdir(parents=True)
    (framework / "lib" / "deps.py").write_text("# placeholder\n", encoding="utf-8")

    assert audit._select_framework_root(installed_script_dir) == framework.resolve()


def test_installed_package_script_imports_support_lib_from_source_layout(tmp_path: Path) -> None:
    installed_script_dir = tmp_path / "localsetup" / "packages" / "ls-framework-audit" / "scripts"
    installed_script_dir.mkdir(parents=True)
    script = installed_script_dir / "run_framework_audit.py"
    shutil.copy2(SCRIPT_ROOT / "run_framework_audit.py", script)
    framework = tmp_path / "localsetup" / "source" / "ls"
    (framework / "lib").mkdir(parents=True)
    (framework / "lib" / "deps.py").write_text(
        "def require_deps(names):\n    return None\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--repo-root" in result.stdout
    assert "--framework-root" in result.stdout


def test_repo_root_defaults_to_caller_cwd_with_repo_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "consumer"
    nested = repo / "tools"
    (repo / ".localsetup").mkdir(parents=True)
    (repo / ".localsetup" / "lock.json").write_text("{}\n", encoding="utf-8")
    nested.mkdir()
    monkeypatch.chdir(nested)

    assert audit._repo_root() == repo.resolve()


def test_repo_root_from_framework_subdirectory_uses_containing_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "ls" / "skills" / "ls-framework-audit" / "scripts"
    script_dir.mkdir(parents=True)
    (repo / "ls" / "README.md").write_text("# Framework\n", encoding="utf-8")
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    monkeypatch.chdir(script_dir)

    assert audit._repo_root() == repo.resolve()


def test_main_honors_explicit_repo_root_and_framework_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "consumer"
    framework = tmp_path / "framework" / "ls"
    repo.mkdir()
    framework.mkdir(parents=True)
    seen: dict[str, tuple[Path, ...]] = {}

    def fake_doc_checks(root: Path, fw: Path) -> list[str]:
        seen["doc"] = (root, fw)
        return []

    monkeypatch.setattr(audit, "phase_doc_checks", fake_doc_checks)
    monkeypatch.setattr(
        audit,
        "validate_audit_roots",
        lambda root, fw: audit.RootValidation(True, True, ()),
    )
    monkeypatch.setattr(audit, "phase_link_checks", lambda root: ([], []))
    monkeypatch.setattr(audit, "phase_skill_matrix", lambda root, fw: ([], []))
    monkeypatch.setattr(audit, "phase_version_facts", lambda root: ([], []))
    monkeypatch.setattr(audit, "phase_maintainer_refs", lambda root: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_framework_audit.py",
            "--repo-root",
            str(repo),
            "--framework-root",
            str(framework),
        ],
    )

    assert audit.main() == 0
    assert capsys.readouterr().out.strip() == "Errors: 0, Warnings: 0"
    assert seen["doc"] == (repo.resolve(), framework.resolve())


def test_link_checks_separate_missing_targets_from_plain_warnings(tmp_path: Path) -> None:
    (tmp_path / "target.md").write_text("# Existing Section\n", encoding="utf-8")
    (tmp_path / "examples").mkdir()
    (tmp_path / "README.md").write_text(
        dedent(
            """\
            # Home
            [valid](target.md#existing-section)
            [missing file](missing.md)
            [missing anchor](target.md#not-there)
            [same file](#home)
            [valid directory](examples/)
            [external](https://example.com/docs)
            [external scheme](ftp://example.com/archive)
            See docs/plain-reference.md for background.
            ```markdown
            [fenced missing](also-missing.md)
            ```
            """
        ),
        encoding="utf-8",
    )
    generated = tmp_path / "ls" / "docs" / "_generated" / "ignored.md"
    generated.parent.mkdir(parents=True)
    generated.write_text("[ignored](missing.md)\n", encoding="utf-8")
    private = tmp_path / ".agents" / "state" / "ignored.md"
    private.parent.mkdir(parents=True)
    private.write_text("[ignored](missing.md)\n", encoding="utf-8")
    localsetup_private = tmp_path / ".localsetup" / "state" / "ignored.md"
    localsetup_private.parent.mkdir(parents=True)
    localsetup_private.write_text("[ignored](missing.md)\n", encoding="utf-8")
    omp_private = tmp_path / ".omp" / "runs" / "ignored.md"
    omp_private.parent.mkdir(parents=True)
    omp_private.write_text("[ignored](missing.md)\n", encoding="utf-8")
    upstream = tmp_path / "ls" / "skills" / "example" / "references" / "upstream" / "ignored.md"
    upstream.parent.mkdir(parents=True)
    upstream.write_text("[archived upstream link](missing.md)\n", encoding="utf-8")
    nested_state = tmp_path / "docs" / "state" / "checked.md"
    nested_state.parent.mkdir(parents=True)
    nested_state.write_text("[checked](missing.md)\n", encoding="utf-8")

    errors, warnings = audit.phase_link_checks(tmp_path)

    assert len(errors) == 3
    assert any("Missing Markdown link target README.md:3: missing.md" in item for item in errors)
    assert any("Missing Markdown anchor README.md:4: target.md#not-there" in item for item in errors)
    assert any(
        "Missing Markdown link target docs/state/checked.md:1: missing.md" in item
        for item in errors
    )
    assert warnings == [
        "Plain link candidate README.md:9: See docs/plain-reference.md for background."
    ]


@pytest.mark.parametrize("venv_name", [".venv", ".venv-codex"])
def test_link_checks_skip_project_virtual_environment_markdown(
    tmp_path: Path, venv_name: str
) -> None:
    (tmp_path / "README.md").write_text(
        "[authored broken link](missing-authored.md)\n", encoding="utf-8"
    )
    installed_doc = (
        tmp_path
        / venv_name
        / "lib"
        / "python3.12"
        / "site-packages"
        / "openai"
        / "README.md"
    )
    installed_doc.parent.mkdir(parents=True)
    installed_doc.write_text(
        "[installed package broken link](missing-package.md)\n", encoding="utf-8"
    )

    errors, warnings = audit.phase_link_checks(tmp_path)

    assert errors == [
        "Missing Markdown link target README.md:1: missing-authored.md"
    ]
    assert warnings == []


@pytest.mark.parametrize("invalid_kind", ["repo", "framework"])
def test_main_reports_invalid_roots_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_kind: str
) -> None:
    repo = tmp_path / "repo"
    framework = tmp_path / "framework" / "ls"
    if invalid_kind != "repo":
        repo.mkdir()
        (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (repo / "README.md").write_text("**Version:** 1.0.0\n", encoding="utf-8")
    if invalid_kind != "framework":
        for directory in ("lib", "docs", "skills", "tests"):
            (framework / directory).mkdir(parents=True, exist_ok=True)
        (framework / "lib" / "deps.py").write_text("# fixture\n", encoding="utf-8")
    report = tmp_path / f"{invalid_kind}-report.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_framework_audit.py",
            "--repo-root",
            str(repo),
            "--framework-root",
            str(framework),
            "--output",
            str(report),
        ],
    )

    assert audit.main() == 1
    text = report.read_text(encoding="utf-8")
    assert "## Errors" in text
    assert f"{invalid_kind.title()}" in text


def test_main_reports_every_maintainer_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    framework = tmp_path / "framework"
    repo.mkdir()
    framework.mkdir()
    findings = [f"doc-{index}.md:1: private maintainer" for index in range(23)]
    report = tmp_path / "report.md"
    monkeypatch.setattr(
        audit,
        "validate_audit_roots",
        lambda root, fw: audit.RootValidation(True, True, ()),
    )
    monkeypatch.setattr(audit, "phase_doc_checks", lambda root, fw: [])
    monkeypatch.setattr(audit, "phase_link_checks", lambda root: ([], []))
    monkeypatch.setattr(audit, "phase_skill_matrix", lambda root, fw: ([], []))
    monkeypatch.setattr(audit, "phase_version_facts", lambda root: ([], []))
    monkeypatch.setattr(audit, "phase_maintainer_refs", lambda root: findings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_framework_audit.py",
            "--repo-root",
            str(repo),
            "--framework-root",
            str(framework),
            "--output",
            str(report),
        ],
    )

    assert audit.main() == 0
    text = report.read_text(encoding="utf-8")
    assert "Warnings: 23" in text
    assert text.count("Maintainer ref: doc-") == 23


@pytest.mark.parametrize(
    "runtime_path",
    [
        Path(".codex/runs/ledger.md"),
        Path(".omp/runs/ledger.md"),
        Path(".agents/state/ledger.md"),
        Path(".localsetup/state/ledger.md"),
        Path(".codex/sessions/session.md"),
        Path(".codex/logs/log.md"),
        Path(".codex/tmp/scratch.md"),
        Path(".localsetup-maint/audit.md"),
        Path("graphify-out/report.md"),
        Path("state/report.md"),
        Path("data/report.md"),
    ],
)
def test_maintainer_ref_scan_skips_private_runtime_paths(tmp_path: Path, runtime_path: Path) -> None:
    public_doc = tmp_path / "README.md"
    private_doc = tmp_path / runtime_path
    private_doc.parent.mkdir(parents=True)
    public_doc.write_text("This mentions private maintainer context.\n", encoding="utf-8")
    private_doc.write_text("This mentions private maintainer context.\n", encoding="utf-8")

    findings = audit.phase_maintainer_refs(tmp_path)

    assert findings == ["README.md:1: This mentions private maintainer context."]


def test_doc_checks_distinguish_target_and_framework_missing_paths(tmp_path: Path) -> None:
    repo = tmp_path / "consumer"
    framework = tmp_path / "framework" / "ls"
    repo.mkdir()
    framework.mkdir(parents=True)

    errors = audit.phase_doc_checks(repo, framework)

    assert "Missing target repo doc/path: VERSION" in errors
    assert "Missing target repo doc/path: README.md" in errors
    assert "Missing framework source doc/path: docs/VERSIONING.md" in errors
    assert "Missing framework source doc/path: README.md" in errors


def test_skill_matrix_requires_one_smoke_row_per_skill(tmp_path: Path) -> None:
    fw = tmp_path / "ls"
    (fw / "skills" / "ls-present").mkdir(parents=True)
    (fw / "skills" / "ls-missing").mkdir(parents=True)
    (fw / "tests").mkdir(parents=True)
    (fw / "tests" / "skill_smoke_commands.yaml").write_text(
        dedent(
            """\
            ls-present: "N/A"
            """
        ),
        encoding="utf-8",
    )

    errors, warnings = audit.phase_skill_matrix(tmp_path, fw)

    assert warnings == []
    assert errors == [
        "skill_smoke_commands.yaml missing entries for skill dirs: ls-missing"
    ]


def test_skill_matrix_supports_repo_root_smoke_entries(tmp_path: Path) -> None:
    fw = tmp_path / "ls"
    (fw / "skills" / "ls-present").mkdir(parents=True)
    sandbox_scripts = fw / "skills" / "ls-skill-sandbox-tester" / "scripts"
    sandbox_scripts.mkdir(parents=True)
    (sandbox_scripts / "create_sandbox.py").write_text("# placeholder\n", encoding="utf-8")
    (sandbox_scripts / "run_smoke.py").write_text("# placeholder\n", encoding="utf-8")
    (fw / "tests").mkdir(parents=True)
    (fw / "tests" / "skill_smoke_commands.yaml").write_text(
        dedent(
            """\
            ls-present:
              cwd: repo-root
              command: "python3 -c \\"from pathlib import Path; Path('repo-root-smoke.txt').write_text('ok')\\""
            ls-skill-sandbox-tester: "N/A"
            """
        ),
        encoding="utf-8",
    )

    errors, warnings = audit.phase_skill_matrix(tmp_path, fw)

    assert warnings == []
    assert errors == []
    assert (tmp_path / "repo-root-smoke.txt").read_text(encoding="utf-8") == "ok"


def test_skill_matrix_rejects_invalid_structured_smoke_entry(tmp_path: Path) -> None:
    fw = tmp_path / "ls"
    (fw / "skills" / "ls-present").mkdir(parents=True)
    sandbox_scripts = fw / "skills" / "ls-skill-sandbox-tester" / "scripts"
    sandbox_scripts.mkdir(parents=True)
    (sandbox_scripts / "create_sandbox.py").write_text("# placeholder\n", encoding="utf-8")
    (sandbox_scripts / "run_smoke.py").write_text("# placeholder\n", encoding="utf-8")
    (fw / "tests").mkdir(parents=True)
    (fw / "tests" / "skill_smoke_commands.yaml").write_text(
        dedent(
            """\
            ls-present:
              cwd: elsewhere
              command: "python3 -V"
            ls-skill-sandbox-tester: "N/A"
            """
        ),
        encoding="utf-8",
    )

    errors, warnings = audit.phase_skill_matrix(tmp_path, fw)

    assert warnings == []
    assert errors == [
        "Skill matrix ls-present: invalid smoke entry: mapping cwd must be 'skill-sandbox' or 'repo-root'"
    ]


def test_skill_matrix_converts_skills_directory_oserror_to_audit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fw = tmp_path / "ls"
    skills = fw / "skills"
    skills.mkdir(parents=True)
    (fw / "tests").mkdir()
    (fw / "tests" / "skill_smoke_commands.yaml").write_text("{}\n", encoding="utf-8")
    original_iterdir = Path.iterdir

    def fail_skills_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == skills:
            raise OSError("fixture listing failure")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_skills_iterdir)

    errors, warnings = audit.phase_skill_matrix(tmp_path, fw)

    assert warnings == []
    assert len(errors) == 1
    assert "OSError: fixture listing failure" in errors[0]


def test_skill_matrix_stages_shared_helper_without_ambient_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fw = tmp_path / "ls"
    scripts = fw / "skills" / "ls-skill-sandbox-tester" / "scripts"
    scripts.mkdir(parents=True)
    original = SCRIPT_ROOT.parents[1] / "ls-skill-sandbox-tester" / "scripts"
    for name in ("create_sandbox.py", "run_smoke.py"):
        shutil.copy2(original / name, scripts / name)
    candidate = fw / "skills" / "ls-example"
    candidate.mkdir()
    (candidate / "probe.py").write_text(
        "import deps, importlib.util\nassert deps.VALUE == 'staged'\n"
        "assert importlib.util.find_spec('ambient_only') is None\n", encoding="utf-8"
    )
    (fw / "lib").mkdir()
    (fw / "lib" / "deps.py").write_text("VALUE = 'staged'\n", encoding="utf-8")
    (fw / "lib" / "ambient_only.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(fw / "lib"))
    (fw / "tests").mkdir()
    (fw / "tests" / "skill_smoke_commands.yaml").write_text(
        'ls-example: "python3 probe.py"\nls-skill-sandbox-tester: "N/A"\n', encoding="utf-8"
    )
    assert audit.phase_skill_matrix(tmp_path, fw) == ([], [])


def test_source_ownership_preserves_authored_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core.sdk_payload import ownership

    retained = "vendor/lscli/component/README.md"
    for name in [retained, "vendor/lscli/wrapper.md", "docs/build/guide.md", "build/lib/copy.md", "dist/copy.md"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Retained\n[missing](missing.md)\nprivate maintainer\n", encoding="utf-8")
    monkeypatch.setattr(ownership, "upstream_documents", lambda root: {retained: {}})
    (tmp_path / "README.md").write_text(
        f"[valid]({retained}#retained)\n[bad anchor]({retained}#absent)\n", encoding="utf-8"
    )
    errors, _ = audit.phase_link_checks(tmp_path)
    assert len(errors) == 3
    assert any("vendor/lscli/wrapper.md" in error for error in errors)
    assert any("docs/build/guide.md" in error for error in errors)
    assert any("Missing Markdown anchor README.md:2" in error for error in errors)
    refs = audit.phase_maintainer_refs(tmp_path)
    assert sorted(refs) == [
        "docs/build/guide.md:3: private maintainer",
        "vendor/lscli/wrapper.md:3: private maintainer",
    ]


def test_invalid_sdk_ownership_fails_without_exemptions(tmp_path: Path) -> None:
    path = tmp_path / "vendor/lscli/README.md"
    path.parent.mkdir(parents=True)
    path.write_text("[missing](missing.md)\nprivate maintainer\n", encoding="utf-8")
    errors, _ = audit.phase_link_checks(tmp_path)
    assert any("Could not verify upstream document ownership" in error for error in errors)
    assert any("Missing Markdown link target vendor/lscli/README.md" in error for error in errors)
    assert audit.phase_maintainer_refs(tmp_path) == ["vendor/lscli/README.md:2: private maintainer"]
