#!/usr/bin/env python3
# Purpose: Disposable Git state regression scenarios.
# Created: 2026-02-20
# Last updated: 2026-09-02

"""
Run regression scenarios that verify guided_repair_plan detection.
Usage: regression_harness.py [--scenario NAME] [--list] [--keep-temp]
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GUIDED_SCRIPT = SCRIPT_DIR / "guided_repair_plan.py"
SCENARIOS = [
    "orphaned-worktree",
    "detached-head",
    "zero-hash-worktree",
    "manual-phantom-branch-lock",
]
NAME_MAX = 64


class HarnessError(RuntimeError):
    pass


def _sanitize(value: str) -> str:
    if not isinstance(value, str) or len(value) > NAME_MAX:
        raise ValueError(f"scenario name invalid (max {NAME_MAX})")
    value = " ".join(value.split()).strip()
    if not value:
        raise ValueError("scenario name empty")
    return value


def run_cmd(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        command = " ".join(args)
        detail = (result.stderr or result.stdout or "").strip()
        raise HarnessError(f"command failed ({result.returncode}) in {cwd}: {command}\n{detail}")
    return result


def run_guided(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(GUIDED_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(SCRIPT_DIR),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise HarnessError(
            f"guided_repair_plan.py failed ({result.returncode}) for {' '.join(args)}\n{detail}"
        )
    return result


def make_repo(work_root: Path, name: str) -> Path:
    repo = work_root / name
    repo.mkdir(parents=True, exist_ok=True)
    run_cmd(repo, "git", "init", "-q")
    run_cmd(repo, "git", "config", "user.name", "Harness Bot")
    run_cmd(repo, "git", "config", "user.email", "harness@example.com")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    run_cmd(repo, "git", "add", "seed.txt")
    run_cmd(repo, "git", "commit", "-q", "-m", "seed")
    return repo


def task_output_dir(work_root: Path) -> Path:
    output = work_root / ".agents" / "state" / "git-state-regression-harness"
    output.mkdir(parents=True, exist_ok=True)
    return output


def scenario_orphaned_worktree(work_root: Path) -> bool:
    repo = make_repo(work_root, "orphaned-worktree")
    run_cmd(repo, "git", "branch", "repair-me")
    worktree = work_root / "orphaned-worktree-wt"
    run_cmd(repo, "git", "worktree", "add", "-q", str(worktree), "repair-me")
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    result = run_guided(
        "--repo",
        str(repo),
        "--output-dir",
        str(task_output_dir(work_root)),
    )
    output = result.stdout or ""
    return "[orphaned-worktree-metadata]" in output and "git worktree prune -v" in output


def scenario_detached_head(work_root: Path) -> bool:
    repo = make_repo(work_root, "detached-head")
    run_cmd(repo, "git", "checkout", "-q", "--detach")
    result = run_guided(
        "--repo",
        str(repo),
        "--output-dir",
        str(task_output_dir(work_root)),
    )
    output = result.stdout or ""
    return (
        "[detached-head-state]" in output
        and "porcelain v2" in output
        and "git branch rescue/" in output
    )


def scenario_zero_hash_worktree(work_root: Path) -> bool:
    repo = make_repo(work_root, "zero-hash-worktree")
    run_cmd(repo, "git", "branch", "zero-head")
    worktree = work_root / "zero-hash-worktree-wt"
    run_cmd(repo, "git", "worktree", "add", "-q", str(worktree), "zero-head")
    worktree_metadata = repo / ".git" / "worktrees"
    if worktree_metadata.is_dir():
        for directory in worktree_metadata.iterdir():
            if directory.is_dir():
                (directory / "HEAD").write_text(
                    "0000000000000000000000000000000000000000\n",
                    encoding="utf-8",
                )
                break
    result = run_guided(
        "--repo",
        str(repo),
        "--output-dir",
        str(task_output_dir(work_root)),
    )
    return "[zero-hash-worktree-entry]" in (result.stdout or "")


def scenario_manual_phantom_branch_lock(work_root: Path) -> bool:
    del work_root
    result = run_guided("--symptom", "phantom-branch-lock", timeout=30)
    output = result.stdout or ""
    return "[phantom-branch-lock]" in output and "git worktree list --porcelain" in output


def run_scenario(name: str, work_root: Path) -> bool:
    if name == "orphaned-worktree":
        return scenario_orphaned_worktree(work_root)
    if name == "detached-head":
        return scenario_detached_head(work_root)
    if name == "zero-hash-worktree":
        return scenario_zero_hash_worktree(work_root)
    if name == "manual-phantom-branch-lock":
        return scenario_manual_phantom_branch_lock(work_root)
    print(f"Error: unknown scenario '{name}'", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Regression harness for Git state repair plans.")
    parser.add_argument("--scenario", metavar="NAME", help="Run single scenario")
    parser.add_argument("--list", action="store_true", help="List scenarios")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temp workspace")
    args = parser.parse_args()

    if args.list:
        for scenario in SCENARIOS:
            print(scenario)
        return 0
    if not GUIDED_SCRIPT.is_file():
        print(f"Error: guided script not found: {GUIDED_SCRIPT}", file=sys.stderr)
        return 2

    scenarios = [args.scenario] if args.scenario else SCENARIOS
    if args.scenario:
        try:
            _sanitize(args.scenario)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        if args.scenario not in SCENARIOS:
            print(f"Error: unknown scenario '{args.scenario}'", file=sys.stderr)
            return 2

    work_root = Path(tempfile.mkdtemp(prefix="git-state-harness-"))
    try:
        passed_count = 0
        failed_count = 0
        for scenario in scenarios:
            try:
                passed = run_scenario(scenario, work_root)
            except (HarnessError, subprocess.TimeoutExpired) as exc:
                print(f"ERROR {scenario}: {exc}", file=sys.stderr)
                passed = False
            if passed:
                print(f"PASS {scenario}")
                passed_count += 1
            else:
                print(f"FAIL {scenario}")
                failed_count += 1
        print()
        print(f"Harness result: {passed_count} passed, {failed_count} failed")
        return 0 if failed_count == 0 else 1
    finally:
        if not args.keep_temp and work_root.exists():
            shutil.rmtree(work_root, ignore_errors=True)
        elif args.keep_temp:
            print(f"Keeping harness workspace: {work_root}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
