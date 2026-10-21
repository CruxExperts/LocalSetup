#!/usr/bin/env python3
# Purpose: Capture Git repo state to a timestamped controller task directory.
# Created: 2026-02-20
# Last updated: 2026-09-02

"""
Capture Git work tree state beneath an explicit controller task-state directory.
Usage: snapshot_git_state.py [REPO_PATH] --output-dir .agents/state/<task-slug>
REPO_PATH defaults to current directory. Must be inside a Git work tree.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PATH_MAX = 4096
REPO_ARG_MAX = 1024


def _sanitize_path(value: str) -> Path:
    if not isinstance(value, str) or len(value) > REPO_ARG_MAX:
        raise ValueError(f"repo path: invalid length or type (max {REPO_ARG_MAX})")
    value = value.strip().strip("\x00")
    if not value:
        value = "."
    path = Path(value).resolve()
    if not path.exists():
        raise ValueError(f"repo path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"repo path is not a directory: {path}")
    return path


def _sanitize_task_output_dir(value: str) -> Path:
    if not isinstance(value, str) or len(value) > PATH_MAX:
        raise ValueError(f"output directory: invalid length or type (max {PATH_MAX})")
    value = value.strip().strip("\x00")
    if not value:
        raise ValueError("output directory is required")
    path = Path(value).expanduser().resolve()
    parts = path.parts
    try:
        agents_index = parts.index(".agents")
    except ValueError as exc:
        raise ValueError("output directory must be under .agents/state/<task-slug>") from exc
    if len(parts) <= agents_index + 2 or parts[agents_index + 1] != "state":
        raise ValueError("output directory must be under .agents/state/<task-slug>")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"output directory is not a directory: {path}")
    return path


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return (result.stdout or "") if result.returncode == 0 else ""


def _resolve_git_path(repo: Path, option: str) -> Path:
    value = _run_git(repo, "rev-parse", "--path-format=absolute", option).strip()
    if not value:
        raise ValueError(f"could not resolve {option}")
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError(f"resolved {option} is not a directory: {path}")
    return path


def _format_cmd(command: list[str]) -> str:
    return " ".join(command)


def _run_capture(
    repo: Path,
    out_dir: Path,
    name: str,
    *git_args: str,
    accepted_exit_codes: frozenset[int] = frozenset({0}),
) -> bool:
    command = ["git", "-C", str(repo), *git_args]
    out_file = out_dir / f"{name}.txt"
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        content = (
            f"# {name}\n# command: {_format_cmd(command)}\n"
            f"# exit_code: {result.returncode}\n\n"
        )
        content += result.stdout or ""
        if result.stderr:
            content += result.stderr
        out_file.write_text(content, encoding="utf-8", errors="replace")
        return result.returncode in accepted_exit_codes
    except Exception as exc:
        out_file.write_text(
            f"# {name}\n# command: {_format_cmd(command)}\n"
            f"# error: {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
            errors="replace",
        )
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture Git work tree state beneath an explicit controller task-state directory."
    )
    parser.add_argument("repo", nargs="?", default=".", help="Git work tree to inspect")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Controller task directory under .agents/state/<task-slug>",
    )
    parser.add_argument("--json", action="store_true", help="Emit the snapshot result as JSON")
    args = parser.parse_args()

    try:
        repo = _sanitize_path(args.repo)
        task_output_dir = _sanitize_task_output_dir(args.output_dir)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            print(f"Error: '{repo}' is not inside a Git work tree", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    toplevel = _run_git(repo, "rev-parse", "--show-toplevel").strip()
    if not toplevel:
        print("Error: could not get Git toplevel", file=sys.stderr)
        return 2
    toplevel = str(Path(toplevel).resolve())
    try:
        git_dir = _resolve_git_path(repo, "--git-dir")
        git_common_dir = _resolve_git_path(repo, "--git-common-dir")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    out_dir = task_output_dir / "git-state-snapshots" / stamp
    out_dir.mkdir(parents=True, exist_ok=False)
    partial_failures: list[str] = []

    (out_dir / "context.txt").write_text(
        f"snapshot_time={stamp}\ntarget={repo}\ntoplevel={toplevel}\n"
        f"git_dir={git_dir}\ngit_common_dir={git_common_dir}\n"
        f"task_output_dir={task_output_dir}\n"
        f"git_version={_run_git(repo, '--version').strip()}\n",
        encoding="utf-8",
        errors="replace",
    )

    head_path = git_dir / "HEAD"
    if head_path.is_file():
        (out_dir / "head-file.txt").write_text(
            head_path.read_text(encoding="utf-8", errors="replace"),
            encoding="utf-8",
            errors="replace",
        )
    worktrees_dir = git_common_dir / "worktrees"
    if worktrees_dir.is_dir():
        result = subprocess.run(
            ["ls", "-la", str(worktrees_dir)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            partial_failures.append("worktrees-dir-listing")
        (out_dir / "worktrees-dir-listing.txt").write_text(
            result.stdout or result.stderr or "",
            encoding="utf-8",
            errors="replace",
        )

    captures = [
        ("status", ("status", "--porcelain=v2", "--branch")),
        ("branch_current", ("branch", "--show-current")),
        ("symbolic_ref_head", ("symbolic-ref", "-q", "HEAD")),
        ("worktree_list", ("worktree", "list", "--porcelain")),
        ("branch_all_verbose", ("branch", "-vv", "--all")),
        ("remote_verbose", ("remote", "-v")),
        ("show_ref", ("show-ref", "--head")),
        ("rev_parse_head", ("rev-parse", "--verify", "HEAD^{commit}")),
        ("reflog_head", ("reflog", "--date=iso", "-n", "50", "HEAD")),
        ("fsck", ("fsck", "--full", "--no-reflogs")),
    ]
    for name, git_args in captures:
        accepted = frozenset({0, 1}) if name == "symbolic_ref_head" else frozenset({0})
        if not _run_capture(repo, out_dir, name, *git_args, accepted_exit_codes=accepted):
            partial_failures.append(name)

    manifest = {
        "schema_version": 1,
        "snapshot_time": stamp,
        "repo": str(repo),
        "toplevel": toplevel,
        "git_dir": str(git_dir),
        "git_common_dir": str(git_common_dir),
        "task_output_dir": str(task_output_dir),
        "snapshot_dir": str(out_dir),
        "partial_failures": partial_failures,
    }
    (out_dir / "snapshot.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(manifest, sort_keys=True))
    else:
        print("Git state snapshot captured.")
        print(f"Directory: {out_dir}")
    if partial_failures:
        failure_list = ", ".join(partial_failures)
        print(f"Warning: partial snapshot; failed capture(s): {failure_list}", file=sys.stderr)
        if not args.json:
            print("Use the captured files plus warning list before changing refs or worktrees.")
        return 1
    if not args.json:
        print("Use these files to diagnose before changing refs or worktrees.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
