from __future__ import annotations

from pathlib import Path

from .git_subprocess import run_git


def tracked_files(repo_root: Path) -> list[str]:
    completed = run_git(
        repo_root,
        ["ls-files", "--cached"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    return sorted(
        line
        for line in completed.stdout.splitlines()
        if line and (repo_root / line).exists()
    )


def classify_path(path: str) -> str:
    if (
        path.startswith(".agents/")
        or path.startswith(".localsetup-maint/")
        or path.startswith("docs/")
        or path.startswith("ls/docs/local-context/")
        or path.startswith("scripts/")
        or path.startswith("state/")
    ):
        return "private-maintainer"
    if path.startswith("ls/docs/_generated/"):
        return "generate"
    if path.startswith("ls/skills/ls-"):
        return "keep"
    if path.startswith("ls/skills/localsetup-"):
        return "legacy-migration"
    if path.startswith(("ls/workflows/ls-workflow-", "ls/workflows/ls-wf-")):
        return "keep"
    if path.startswith("ls/core/") or path in {
        "ls/config/pack.yaml",
        "ls/config/clients.yaml",
        "ls/config/clients.schema.json",
        "ls/config/platforms.yaml",
        "ls/tools/localsetup.py",
    }:
        return "refactor"
    if path.startswith("ls/") or path in {"README.md", "AGENTS.md", "LICENSE", "SECURITY.md"}:
        return "keep"
    return "keep"


def implementation_file_map(repo_root: Path) -> list[dict]:
    rows: list[dict] = []
    for path in tracked_files(repo_root):
        classification = classify_path(path)
        if classification == "private-maintainer":
            continue
        rows.append({"path": path, "classification": classification})
    return rows
