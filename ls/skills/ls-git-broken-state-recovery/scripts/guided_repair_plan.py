#!/usr/bin/env python3
# Purpose: Print staged Git recovery plans from explicit task-state snapshots.
# Created: 2026-02-20
# Last updated: 2026-09-02

"""
Print recommended Git recovery steps. Does not run repair commands.
Usage: guided_repair_plan.py --list | --symptom <key> | --repo <path> --output-dir <task-dir> | --snapshot <path>
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SNAPSHOT_SCRIPT = SCRIPT_DIR / "snapshot_git_state.py"
BACKUP_SCRIPT = SCRIPT_DIR / "backup_git_metadata.py"
PATH_MAX = 4096
SYMPTOM_MAX = 64

SYMPTOMS = [
    "orphaned-worktree-metadata",
    "phantom-branch-lock",
    "detached-head-state",
    "head-ref-disagreement",
    "missing-or-broken-refs",
    "zero-hash-worktree-entry",
]

PLANS = {
    "orphaned-worktree-metadata": """[orphaned-worktree-metadata]
Run these first:
  git worktree list --porcelain
  git worktree prune -v
  git worktree list --porcelain

If stale entries remain, resolve the common metadata directory with:
  git rev-parse --path-format=absolute --git-common-dir
Do not edit it until scripts/backup_git_metadata.py has produced a verified
archive and receipt in the controller task directory. Require point-of-risk
confirmation for the exact stale metadata path before removal.""",
    "phantom-branch-lock": """[phantom-branch-lock]
Run these first:
  git worktree list --porcelain

Then:
  1) Identify the worktree currently owning the branch.
  2) In that worktree, switch to another branch (or intentionally detach HEAD).
  3) Retry branch delete/switch in the main repository.

If ownership metadata is stale after verification, resolve it through
`git rev-parse --path-format=absolute --git-common-dir`, create a verified
metadata backup, and require point-of-risk confirmation for the exact path
before manual cleanup.""",
    "detached-head-state": """[detached-head-state]
The snapshot corroborated all three detached-HEAD signals: porcelain v2 reports
`# branch.head (detached)`, `git symbolic-ref -q HEAD` returned no ref, and
`git rev-parse --verify HEAD^{commit}` resolved a commit.

Inspect and rescue before switching:
  git reflog --date=iso -n 20 HEAD
  git branch rescue/$(date +%Y%m%d-%H%M%S) HEAD
  git switch <known-good-branch>""",
    "missing-or-broken-refs": """[missing-or-broken-refs]
Inspect local history before moving any branch pointer:
  git reflog --date=iso -n 50 HEAD
  git show <local-only-tip>

Create and verify a rescue ref for every local-only tip:
  git branch rescue/$(date +%Y%m%d-%H%M%S) <local-only-tip>
  git show-ref --verify refs/heads/rescue/<timestamp>

Only after rescue refs exist, refresh and verify the remote target:
  git fetch --all --prune
  git show-ref --verify refs/remotes/origin/<branch>
  git rev-parse --verify refs/remotes/origin/<branch>^{commit}

POINT OF RISK: `git branch -f` moves refs/heads/<branch>. Obtain explicit
confirmation naming the repository, local branch, verified remote ref, and
resolved commit before running either command:
  git branch -f <branch> refs/remotes/origin/<branch>
  git switch <branch>""",
    "zero-hash-worktree-entry": """[zero-hash-worktree-entry]
Run these first:
  git worktree list --porcelain
  git worktree prune -v
  git worktree list --porcelain

If zero-hash entries persist, recreate affected worktree(s) from a verified branch ref.""",
}


def _sanitize(value: str, max_len: int, name: str) -> str:
    if not isinstance(value, str) or len(value) > max_len:
        raise ValueError(f"{name}: invalid length or type (max {max_len})")
    return " ".join(value.split()).strip()


def _sanitize_path(value: str) -> Path:
    value = _sanitize(value, PATH_MAX, "path")
    path = Path(value).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"path does not exist or is not a directory: {path}")
    return path


def _task_output_dir(value: str) -> Path:
    path = Path(_sanitize(value, PATH_MAX, "output directory")).expanduser().resolve()
    parts = path.parts
    try:
        agents_index = parts.index(".agents")
    except ValueError as exc:
        raise ValueError("output directory must be under .agents/state/<task-slug>") from exc
    if len(parts) <= agents_index + 2 or parts[agents_index + 1] != "state":
        raise ValueError("output directory must be under .agents/state/<task-slug>")
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_symptoms() -> None:
    print("Available symptom keys:")
    for symptom in SYMPTOMS:
        print(f"  {symptom}")


def _head_ref_plan(backup_receipt: dict[str, Any] | None = None) -> str:
    if backup_receipt is None:
        return """[head-ref-disagreement]
Confirm the expected branch without editing metadata:
  git branch --show-current
  git symbolic-ref -q HEAD
  git show-ref --verify refs/heads/<expected-branch>

Manual HEAD repair commands are withheld because no verified metadata backup
was created. Rerun with `--repo <path> --output-dir
.agents/state/<task-slug>`; the planner will create and verify the backup before
showing either manual repair command."""

    repo = Path(str(backup_receipt["repo"])).resolve()
    git_dir = Path(str(backup_receipt["git_dir"])).resolve()
    head_path = git_dir / "HEAD"
    receipt_path = Path(str(backup_receipt["receipt"])).resolve()
    archive_path = Path(str(backup_receipt["archive"])).resolve()
    quoted_repo = shlex.quote(str(repo))
    return f"""[head-ref-disagreement]
Confirm the expected branch without editing metadata:
  git -C {quoted_repo} branch --show-current
  git -C {quoted_repo} symbolic-ref -q HEAD
  git -C {quoted_repo} show-ref --verify refs/heads/<expected-branch>

Verified metadata backup:
  archive: {archive_path}
  receipt: {receipt_path}
  sha256: {backup_receipt['archive_sha256']}

Preferred repair after explicit point-of-risk confirmation naming the repository,
expected branch, resolved ref, and verified backup receipt:
  git -C {quoted_repo} symbolic-ref HEAD refs/heads/<expected-branch>

Fallback only when `git symbolic-ref` cannot be used, after a second explicit
confirmation for this resolved per-worktree HEAD path:
  printf '%s\\n' 'ref: refs/heads/<expected-branch>' > {shlex.quote(str(head_path))}"""


def print_plan(symptom: str, backup_receipt: dict[str, Any] | None = None) -> None:
    if symptom not in SYMPTOMS:
        print(f"Error: unknown symptom '{symptom}'", file=sys.stderr)
        raise SystemExit(1)
    if symptom == "head-ref-disagreement":
        print(_head_ref_plan(backup_receipt))
        return
    print(PLANS[symptom])


def _capture(path: Path) -> tuple[int | None, str]:
    if not path.is_file():
        return None, ""
    text = path.read_text(encoding="utf-8", errors="replace")
    header, separator, body = text.partition("\n\n")
    if not separator:
        return None, ""
    match = re.search(r"^# exit_code: (-?\d+)$", header, re.MULTILINE)
    return (int(match.group(1)) if match else None), body.strip()


def _snapshot_manifest(snapshot_dir: Path) -> dict[str, Any]:
    manifest_path = snapshot_dir / "snapshot.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid snapshot manifest: {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported snapshot manifest: {manifest_path}")
    declared_snapshot = Path(str(payload.get("snapshot_dir", ""))).resolve()
    task_output = Path(str(payload.get("task_output_dir", ""))).resolve()
    expected_root = (task_output / "git-state-snapshots").resolve()
    if declared_snapshot != snapshot_dir.resolve():
        raise ValueError("snapshot manifest path does not match the selected directory")
    try:
        declared_snapshot.relative_to(expected_root)
    except ValueError as exc:
        raise ValueError("snapshot directory is outside its task output root") from exc
    _task_output_dir(str(task_output))
    return payload


def _create_verified_backup(repo: Path, task_output_dir: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(BACKUP_SCRIPT),
            str(repo),
            "--output-dir",
            str(task_output_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(SCRIPT_DIR),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "backup failed").strip()
        raise RuntimeError(f"could not create verified metadata backup: {detail}")
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("metadata backup returned invalid JSON") from exc
    if not isinstance(receipt, dict) or receipt.get("verified") is not True:
        raise RuntimeError("metadata backup did not return a verified receipt")
    for key in ("archive", "receipt"):
        path = Path(str(receipt.get(key, ""))).resolve()
        if not path.is_file():
            raise RuntimeError(f"metadata backup {key} is missing: {path}")
        try:
            path.relative_to(task_output_dir.resolve())
        except ValueError as exc:
            raise RuntimeError(f"metadata backup {key} escaped the task output directory") from exc
    return receipt


def _detect(snapshot_dir: Path) -> list[str]:
    worktree_exit, worktree_body = _capture(snapshot_dir / "worktree_list.txt")
    status_exit, status_body = _capture(snapshot_dir / "status.txt")
    branch_exit, branch_body = _capture(snapshot_dir / "branch_current.txt")
    symbolic_exit, symbolic_body = _capture(snapshot_dir / "symbolic_ref_head.txt")
    show_ref_exit, show_ref_body = _capture(snapshot_dir / "show_ref.txt")
    rev_parse_exit, rev_parse_body = _capture(snapshot_dir / "rev_parse_head.txt")

    matches: list[str] = []
    if worktree_exit == 0:
        for line in worktree_body.splitlines():
            if line.startswith("worktree "):
                worktree = line[9:].strip()
                if worktree and not Path(worktree).exists():
                    matches.append("orphaned-worktree-metadata")
                    break
        if re.search(r"^HEAD\s+0{40}$", worktree_body, re.MULTILINE):
            initial = re.search(r"^# branch\.oid \(initial\)$", status_body, re.MULTILINE)
            if status_exit != 0 or not initial:
                matches.append("zero-hash-worktree-entry")

    detached_marker = re.search(r"^# branch\.head \(detached\)$", status_body, re.MULTILINE)
    resolvable_head = rev_parse_exit == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", rev_parse_body)
    if status_exit == 0 and detached_marker and symbolic_exit == 1 and not symbolic_body and resolvable_head:
        matches.append("detached-head-state")

    current_branch = branch_body if branch_exit == 0 else ""
    symbolic_branch = symbolic_body.removeprefix("refs/heads/") if symbolic_exit == 0 else ""
    if current_branch and symbolic_branch and current_branch != symbolic_branch:
        matches.append("head-ref-disagreement")

    broken_pattern = r"unknown revision|not a valid object name|cannot lock ref|fatal:"
    if status_exit not in (None, 0) and re.search(broken_pattern, status_body, re.IGNORECASE):
        matches.append("missing-or-broken-refs")
    if show_ref_exit not in (None, 0) and re.search(broken_pattern, show_ref_body, re.IGNORECASE):
        matches.append("missing-or-broken-refs")
    return list(dict.fromkeys(matches))


def _verify_snapshot_repo(manifest: dict[str, Any], repo: Path) -> None:
    for option, key in (("--git-dir", "git_dir"), ("--git-common-dir", "git_common_dir")):
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", option],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise ValueError(f"could not resolve current repository {option}")
        current_path = Path(result.stdout.strip()).resolve()
        snapshot_path = Path(str(manifest.get(key, ""))).resolve()
        if current_path != snapshot_path:
            raise ValueError(
                f"snapshot does not belong to the selected repository: {key} differs"
            )


def run_detection(
    snapshot_dir: Path,
    *,
    repo: Path | None = None,
    task_output_dir: Path | None = None,
) -> None:
    manifest = _snapshot_manifest(snapshot_dir)
    manifest_task_output = Path(str(manifest.get("task_output_dir", ""))).resolve()
    if task_output_dir is not None and task_output_dir.resolve() != manifest_task_output:
        raise ValueError("--output-dir must match the snapshot task output directory")
    if repo is not None:
        _verify_snapshot_repo(manifest, repo)
    matches = _detect(snapshot_dir)
    if not matches:
        print(f"No deterministic symptom match found in snapshot: {snapshot_dir}", file=sys.stderr)
        print("Use --symptom with one of:", file=sys.stderr)
        list_symptoms()
        return

    backup_receipt: dict[str, Any] | None = None
    if "head-ref-disagreement" in matches and repo is not None and task_output_dir is not None:
        backup_receipt = _create_verified_backup(repo, task_output_dir)

    print(f"Detected symptom(s) from snapshot: {snapshot_dir}", file=sys.stderr)
    for symptom in matches:
        print()
        print_plan(symptom, backup_receipt if symptom == "head-ref-disagreement" else None)


def resolve_snapshot_from_repo(repo: Path, task_output_dir: Path) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SNAPSHOT_SCRIPT),
            str(repo),
            "--output-dir",
            str(task_output_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(SCRIPT_DIR),
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"snapshot command returned invalid JSON for repo '{repo}'") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"snapshot command returned an invalid result for repo '{repo}'")
    snapshot_dir = Path(str(payload.get("snapshot_dir", ""))).resolve()
    expected_root = (task_output_dir / "git-state-snapshots").resolve()
    try:
        snapshot_dir.relative_to(expected_root)
    except ValueError as exc:
        raise RuntimeError("snapshot command returned a path outside the task output directory") from exc
    if not snapshot_dir.is_dir():
        raise RuntimeError(f"snapshot directory does not exist: {snapshot_dir}")
    _snapshot_manifest(snapshot_dir)
    if result.returncode != 0:
        failures = payload.get("partial_failures", [])
        print(f"Warning: using partial snapshot; failed capture(s): {failures}", file=sys.stderr)
    return snapshot_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print staged Git recovery plans by symptom or task-state snapshot."
    )
    parser.add_argument("--list", "-l", action="store_true", help="List symptom keys")
    parser.add_argument("--symptom", metavar="KEY", help="Print plan for symptom key")
    parser.add_argument("--repo", metavar="PATH", help="Repository path; capture a new snapshot")
    parser.add_argument("--snapshot", metavar="PATH", help="Path to a snapshot directory")
    parser.add_argument(
        "--output-dir",
        help="Controller task directory under .agents/state/<task-slug>; required with --repo",
    )
    args = parser.parse_args()

    if args.list:
        list_symptoms()
        return 0
    if args.symptom:
        try:
            key = _sanitize(args.symptom, SYMPTOM_MAX, "symptom")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print_plan(key)
        return 0
    if not args.repo and not args.snapshot:
        print("Error: provide --repo, --snapshot, or both", file=sys.stderr)
        parser.print_help(file=sys.stderr)
        return 2

    try:
        repo = _sanitize_path(args.repo) if args.repo else None
        task_output_dir = _task_output_dir(args.output_dir) if args.output_dir else None
        if repo is not None and task_output_dir is None:
            raise ValueError("--output-dir is required with --repo")
        if repo is None and task_output_dir is not None:
            raise ValueError("--output-dir is only valid with --repo")

        if args.snapshot:
            snapshot_dir = _sanitize_path(args.snapshot)
        else:
            assert repo is not None and task_output_dir is not None
            snapshot_dir = resolve_snapshot_from_repo(repo, task_output_dir)
        run_detection(snapshot_dir, repo=repo, task_output_dir=task_output_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
