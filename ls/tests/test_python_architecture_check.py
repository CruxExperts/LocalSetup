from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "ls" / "tools" / "python_architecture_check.py"
POINTER = (
    "Python architecture: new and substantially refactored Python tooling follows "
    "ls/docs/PYTHON_ARCHITECTURE_STANDARD.md; keep entrypoints thin, package "
    "responsibilities explicit, and existing debt baseline-managed."
)
RESOLVER_POINTER = (
    "Python architecture: new and substantially refactored Python tooling follows "
    "`localsetup://doc/PYTHON_ARCHITECTURE_STANDARD.md`; keep entrypoints thin, package "
    "responsibilities explicit, and existing debt baseline-managed."
)
ANCHORS = (
    "# Scope And Authority",
    "# Environment Standard",
    "# Package Layout",
    "# Module Responsibilities",
    "# File Size Rules",
    "# Tooling And Validation",
    "# Refactoring Rules",
    "# Source Evidence",
)
POINTER_PATHS = (
    "AGENTS.md",
    "ls/docs/TOOLING_POLICY.md",
    "ls/docs/README.md",
    "ls/docs/AGENTIC_DESIGN_INDEX.md",
    "ls/skills/ls-context/SKILL.md",
    "ls/templates/codex/AGENTS.md",
    "ls/templates/opencode/AGENTS.md",
    "ls/templates/claude-code/CLAUDE.md",
    "ls/templates/kilo/AGENTS.md",
    "ls/templates/kilo/instructions.md",
    "ls/templates/openclaw/OPENCLAW_CONTEXT.md",
    "ls/templates/cursor/ls-context.mdc",
    "ls/templates/cursor/ls-context-index.md",
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)


def make_repo(tmp_path: Path, *, large_lines: int = 10) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    write(repo / "ls/core/demo.py", "\n".join("x = 1" for _ in range(large_lines)) + "\n")
    write(repo / "ls/tools/python_architecture_check.py", "# wrapper\n")
    write(
        repo / "ls/docs/PYTHON_ARCHITECTURE_STANDARD.md",
        "---\nstatus: ACTIVE\nversion: 4.0\nowner_skill: ls-script-and-docs-quality\n---\n\n"
        + "\n\n".join(ANCHORS)
        + "\n",
    )
    for rel_path in POINTER_PATHS:
        pointer = RESOLVER_POINTER if rel_path == "ls/skills/ls-context/SKILL.md" else POINTER
        write(repo / rel_path, pointer + "\n")
    write(
        repo / "ls/config/python-architecture-baseline.json",
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-05",
                "scope": "framework",
                "entries": [],
            },
            indent=2,
        )
        + "\n",
    )
    init_repo(repo)
    return repo


def run_checker(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python",
            str(CHECKER),
            "--repo-root",
            str(repo),
            "--baseline",
            "ls/config/python-architecture-baseline.json",
            *args,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def finding_codes(result: subprocess.CompletedProcess[str]) -> set[str]:
    return {finding["code"] for finding in payload(result)["findings"]}


def test_checker_clean_repo_passes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    result = run_checker(repo)

    assert result.returncode == 0
    assert payload(result)["ok"] is True


def test_checker_new_oversized_file_warns(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, large_lines=701)

    result = run_checker(repo)

    assert result.returncode == 0
    assert "PYA001_OVERSIZED_NEW" in finding_codes(result)


def test_checker_worsened_baselined_file_warns(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, large_lines=702)
    baseline = {
        "schema_version": "1.0",
        "generated_at": "2026-06-05",
        "scope": "framework",
        "entries": [
            {
                "id": "demo",
                "path": "ls/core/demo.py",
                "metric": "lines",
                "current_value": 701,
                "threshold": 700,
                "reason": "Existing fixture debt.",
                "owner": "test",
            }
        ],
    }
    write(repo / "ls/config/python-architecture-baseline.json", json.dumps(baseline) + "\n")

    result = run_checker(repo)

    assert result.returncode == 0
    assert "PYA002_OVERSIZED_WORSENED" in finding_codes(result)


def test_checker_stale_baseline_entry_warns(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    baseline = {
        "schema_version": "1.0",
        "generated_at": "2026-06-05",
        "scope": "framework",
        "entries": [
            {
                "id": "missing",
                "path": "ls/core/missing.py",
                "metric": "lines",
                "current_value": 701,
                "threshold": 700,
                "reason": "Deleted fixture debt.",
                "owner": "test",
            }
        ],
    }
    write(repo / "ls/config/python-architecture-baseline.json", json.dumps(baseline) + "\n")

    result = run_checker(repo)

    assert result.returncode == 0
    assert "PYA102_STALE_BASELINE_ENTRY" in finding_codes(result)


def test_checker_out_of_scope_existing_baseline_entry_is_not_stale(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write(repo / "ls/skills/ls-demo/scripts/demo.py", "x = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    baseline = {
        "schema_version": "1.0",
        "generated_at": "2026-06-05",
        "scope": "framework",
        "entries": [
            {
                "id": "skill-demo",
                "path": "ls/skills/ls-demo/scripts/demo.py",
                "metric": "lines",
                "current_value": 701,
                "threshold": 700,
                "reason": "Existing skill debt outside default framework scope.",
                "owner": "test",
            }
        ],
    }
    write(repo / "ls/config/python-architecture-baseline.json", json.dumps(baseline) + "\n")

    result = run_checker(repo)

    assert result.returncode == 0
    assert "PYA102_STALE_BASELINE_ENTRY" not in finding_codes(result)


def test_checker_malformed_baseline_exits_two(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write(repo / "ls/config/python-architecture-baseline.json", "{nope\n")

    result = run_checker(repo)

    assert result.returncode == 2
    assert "PYA004_BASELINE_MALFORMED" in finding_codes(result)


def test_checker_missing_doc_anchor_fails(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    text = (repo / "ls/docs/PYTHON_ARCHITECTURE_STANDARD.md").read_text(encoding="utf-8")
    write(repo / "ls/docs/PYTHON_ARCHITECTURE_STANDARD.md", text.replace("# Source Evidence", ""))

    result = run_checker(repo)

    assert result.returncode == 1
    assert "PYA005_REQUIRED_DOC_ANCHOR_MISSING" in finding_codes(result)


def test_checker_missing_template_pointer_fails(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write(repo / "ls/templates/codex/AGENTS.md", "missing pointer\n")

    result = run_checker(repo)

    assert result.returncode == 1
    assert "PYA006_REQUIRED_TEMPLATE_POINTER_MISSING" in finding_codes(result)


def test_checker_requires_resolver_pointer_for_context_skill(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write(repo / "ls/skills/ls-context/SKILL.md", POINTER + "\n")

    result = run_checker(repo)

    assert result.returncode == 1
    assert "PYA006_REQUIRED_TEMPLATE_POINTER_MISSING" in finding_codes(result)


def test_checker_missing_wrapper_fails(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "ls/tools/python_architecture_check.py").unlink()

    result = run_checker(repo)

    assert result.returncode == 1
    assert "PYA007_PUBLIC_WRAPPER_MISSING" in finding_codes(result)


def test_template_parity_uses_pointer_and_required_anchors() -> None:
    standard = (ROOT / "ls/docs/PYTHON_ARCHITECTURE_STANDARD.md").read_text(encoding="utf-8")
    for anchor in ANCHORS:
        assert anchor in standard

    for rel_path in POINTER_PATHS:
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        pointer = RESOLVER_POINTER if rel_path == "ls/skills/ls-context/SKILL.md" else POINTER
        assert pointer in text
        assert "# Source Evidence" not in text
