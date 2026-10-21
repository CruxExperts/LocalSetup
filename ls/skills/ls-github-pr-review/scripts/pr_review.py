#!/usr/bin/env python3
# Purpose: Automated GitHub PR code review (check, review, post, status, list-unreviewed). Replaces pr-review.sh.
# Created: 2026-02-20
# Last updated: 2026-05-09

"""
PR review CLI. Requires gh CLI. Uses PR_REVIEW_REPO, PR_REVIEW_DIR, PR_REVIEW_STATE, PR_REVIEW_OUTDIR.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pr_review_analysis import analyze_diff, categorize_files, check_test_coverage, run_local_lint
from pr_review_report import compose_report

PATH_MAX = 4096
PR_NUM_MAX = 999999
REPO_MAX = 256
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ReviewInputError(RuntimeError):
    """Raised when required review input cannot be loaded reliably."""


def _sanitize(s: str, max_len: int, name: str) -> str:
    if not isinstance(s, str):
        raise ValueError(f"{name}: expected string")
    s = " ".join(s.split()).strip()
    if len(s) > max_len:
        raise ValueError(f"{name}: length exceeds {max_len}")
    return s


def _sanitize_repo(repo: str, *, allow_dot: bool = False) -> str:
    repo = _sanitize(repo, REPO_MAX, "repo")
    if allow_dot and repo == ".":
        return repo
    if not REPO_PATTERN.fullmatch(repo):
        raise ValueError("repo must be in owner/repo format")
    return repo


def _sanitize_pr_num(pr_num: int) -> int:
    if not isinstance(pr_num, int) or pr_num < 1 or pr_num > PR_NUM_MAX:
        raise ValueError(f"PR number must be between 1 and {PR_NUM_MAX}")
    return pr_num


def _sanitize_path(path: Path, name: str) -> Path:
    resolved = path.expanduser().resolve()
    if len(str(resolved)) > PATH_MAX:
        raise ValueError(f"{name}: path length exceeds {PATH_MAX}")
    return resolved


def _log(msg: str) -> None:
    print(f"[pr-review] {msg}", file=sys.stderr)


def _gh(repo: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    repo = _sanitize_repo(repo, allow_dot=True)
    cmd = ["gh"] + list(args) + ["--repo", repo]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            input=stdin,
        )
    except FileNotFoundError as exc:
        raise ReviewInputError("gh CLI is required but was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewInputError(f"gh command timed out: {' '.join(cmd[:4])} ...") from exc


def _gh_error(label: str, r: subprocess.CompletedProcess) -> ReviewInputError:
    detail = (r.stderr or r.stdout or "").strip()
    if detail:
        detail = f": {detail[:500]}"
    return ReviewInputError(f"Could not load {label}; gh exited {r.returncode}{detail}")


def _require_gh_text(repo: str, label: str, *args: str) -> str:
    r = _gh(repo, *args)
    if r.returncode != 0:
        raise _gh_error(label, r)
    return r.stdout or ""


def _require_json(label: str, text: str, expected_type: type) -> object:
    if not text.strip():
        raise ReviewInputError(f"Could not load {label}; gh returned empty JSON output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewInputError(f"Could not parse {label} JSON: {exc.msg}") from exc
    if not isinstance(data, expected_type):
        raise ReviewInputError(f"Could not parse {label} JSON: expected {expected_type.__name__}")
    return data


def get_repo_and_dirs() -> tuple[str, Path | None, Path, Path]:
    repo = os.environ.get("PR_REVIEW_REPO", "").strip()
    if not repo:
        r = _gh(".", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        if r.returncode == 0 and r.stdout:
            repo = r.stdout.strip()
        if not repo:
            print("Error: Could not detect repo. Set PR_REVIEW_REPO=owner/repo", file=sys.stderr)
            sys.exit(1)
    try:
        repo = _sanitize_repo(repo)
    except ValueError as exc:
        print(f"Error: invalid PR_REVIEW_REPO: {exc}", file=sys.stderr)
        sys.exit(2)
    local_dir = os.environ.get("PR_REVIEW_DIR", "").strip()
    local_path = _sanitize_path(Path(local_dir), "PR_REVIEW_DIR") if local_dir else None
    if local_dir and (not local_path.exists() or not local_path.is_dir()):
        local_path = None
    if not local_path:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout:
            local_path = _sanitize_path(Path(r.stdout.strip()), "git root")
    state_path = _sanitize_path(Path(os.environ.get("PR_REVIEW_STATE", "./data/pr-reviews.json")), "PR_REVIEW_STATE")
    outdir = _sanitize_path(Path(os.environ.get("PR_REVIEW_OUTDIR", "./data/pr-reviews")), "PR_REVIEW_OUTDIR")
    outdir.mkdir(parents=True, exist_ok=True)
    if not state_path.exists():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{}", encoding="utf-8")
    return repo, local_path, state_path, outdir


def get_open_prs(repo: str) -> list[dict]:
    text = _require_gh_text(
        repo,
        "open PR list",
        "pr",
        "list",
        "--state",
        "open",
        "--json",
        "number,title,author,createdAt,headRefName,additions,deletions,changedFiles,labels,baseRefName,headRefOid",
    )
    return _require_json("open PR list", text, list)  # type: ignore[return-value]


def get_pr_diff(repo: str, pr_num: int) -> str:
    pr_num = _sanitize_pr_num(pr_num)
    return _require_gh_text(repo, f"PR #{pr_num} diff", "pr", "diff", str(pr_num))


def get_pr_files(repo: str, pr_num: int) -> list[str]:
    pr_num = _sanitize_pr_num(pr_num)
    text = _require_gh_text(
        repo,
        f"PR #{pr_num} files",
        "pr",
        "view",
        str(pr_num),
        "--json",
        "files",
        "-q",
        ".files[].path",
    )
    return [f.strip() for f in text.splitlines() if f.strip()]


def get_pr_commits(repo: str, pr_num: int) -> str:
    pr_num = _sanitize_pr_num(pr_num)
    text = _require_gh_text(repo, f"PR #{pr_num} commits", "pr", "view", str(pr_num), "--json", "commits")
    data = _require_json(f"PR #{pr_num} commits", text, dict)
    commits = data.get("commits", [])
    if not isinstance(commits, list):
        raise ReviewInputError(f"Could not parse PR #{pr_num} commits JSON: commits must be a list")
    return "\n".join(f"{c.get('oid', '')[:8]} {c.get('messageHeadline', '')}" for c in commits if isinstance(c, dict))


def get_pr_view(repo: str, pr_num: int) -> dict:
    pr_num = _sanitize_pr_num(pr_num)
    text = _require_gh_text(
        repo,
        f"PR #{pr_num} metadata",
        "pr",
        "view",
        str(pr_num),
        "--json",
        "title,author,headRefName,headRefOid,baseRefName,additions,deletions,body,createdAt,labels",
    )
    data = _require_json(f"PR #{pr_num} metadata", text, dict)
    return data  # type: ignore[return-value]


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewInputError(f"Could not parse review state JSON at {state_path}: {exc.msg}") from exc
    except OSError as exc:
        raise ReviewInputError(f"Could not read review state at {state_path}: {exc}") from exc
    if not isinstance(state, dict):
        raise ReviewInputError(f"Could not parse review state JSON at {state_path}: expected object")
    return state


def save_state(state_path: Path, state: dict) -> None:
    try:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ReviewInputError(f"Could not write review state at {state_path}: {exc}") from exc


def is_reviewed(repo: str, pr_num: int, state_path: Path) -> bool:
    pr_num = _sanitize_pr_num(pr_num)
    head_sha = _require_gh_text(
        repo,
        f"PR #{pr_num} head SHA",
        "pr",
        "view",
        str(pr_num),
        "--json",
        "headRefOid",
        "-q",
        ".headRefOid",
    ).strip()
    if not head_sha:
        raise ReviewInputError(f"Could not load PR #{pr_num} head SHA; gh returned empty output")
    state = load_state(state_path)
    pr = state.get(str(pr_num), {})
    return pr.get("head_sha") == head_sha and pr.get("status") == "reviewed"


def generate_report(repo: str, pr_num: int, state_path: Path, outdir: Path, local_dir: Path | None, force: bool) -> Path | None:
    if not force and is_reviewed(repo, pr_num, state_path):
        _log(f"PR #{pr_num} already reviewed at current HEAD. Use 'review' to force.")
        return None
    _log(f"Reviewing PR #{pr_num}...")
    view = get_pr_view(repo, pr_num)
    if not view:
        print(f"Error: Could not load PR #{pr_num}", file=sys.stderr)
        return None
    files = get_pr_files(repo, pr_num)
    diff = get_pr_diff(repo, pr_num)
    commits = get_pr_commits(repo, pr_num)
    categories = categorize_files(files)
    findings = analyze_diff(diff)
    test_cov = check_test_coverage(files)
    lint_results = run_local_lint(files, local_dir)
    report = compose_report(
        repo=repo,
        pr_num=pr_num,
        view=view,
        commits=commits,
        categories=categories,
        findings=findings,
        test_cov=test_cov,
        lint_results=lint_results,
    )

    report_file = outdir / f"{pr_num}.md"
    report_file.write_text(report, encoding="utf-8", errors="replace")
    state = load_state(state_path)
    state[str(pr_num)] = {
        "head_sha": view.get("headRefOid", ""),
        "status": "reviewed",
        "reviewed_at": int(time.time()),
        "report": str(report_file),
    }
    save_state(state_path, state)
    _log(f"Report saved to {report_file}")
    return report_file


def cmd_check(repo: str, state_path: Path, outdir: Path, local_dir: Path | None) -> int:
    prs = get_open_prs(repo)
    if not prs:
        _log("No open PRs.")
        print('{"reviewed": 0, "skipped": 0, "total": 0}')
        return 0
    reviewed = 0
    skipped = 0
    for pr in prs:
        num = pr["number"]
        if is_reviewed(repo, num, state_path):
            skipped += 1
            _log(f"PR #{num}: already reviewed, skipping.")
        else:
            generate_report(repo, num, state_path, outdir, local_dir, False)
            reviewed += 1
    print(json.dumps({"reviewed": reviewed, "skipped": skipped, "total": len(prs)}))
    return 0


def cmd_review(repo: str, pr_num: int, state_path: Path, outdir: Path, local_dir: Path | None) -> int:
    try:
        pr_num = _sanitize_pr_num(pr_num)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0 if generate_report(repo, pr_num, state_path, outdir, local_dir, True) else 1


def cmd_post(repo: str, pr_num: int, outdir: Path) -> int:
    report_file = outdir / f"{pr_num}.md"
    if not report_file.is_file():
        _log(f"No review report for PR #{pr_num}. Run 'review {pr_num}' first.")
        return 1
    r = _gh(repo, "pr", "comment", str(pr_num), "--body-file", str(report_file))
    if r.returncode != 0:
        print(r.stderr or r.stdout or "gh pr comment failed", file=sys.stderr)
        return 1
    _log(f"Review posted to PR #{pr_num}")
    return 0


def cmd_status(repo: str, state_path: Path) -> int:
    prs = get_open_prs(repo)
    state = load_state(state_path)
    if not prs:
        print("No open PRs.")
        return 0
    print(f"Open PRs: {len(prs)}\n")
    for pr in prs:
        num = str(pr["number"])
        s = state.get(num, {})
        status = s.get("status", "unreviewed")
        icon = "[OK]" if status == "reviewed" else "[PENDING]"
        print(f"{icon} PR #{num}: {pr['title']} ({pr['author']['login']})")
        print(f"   +{pr['additions']}/-{pr['deletions']} | {pr['headRefName']}")
        if s:
            age = int(time.time()) - s.get("reviewed_at", 0)
            print(f"   Reviewed {age // 3600}h ago | SHA: {(s.get('head_sha') or '?')[:8]}")
        print()
    return 0


def cmd_list_unreviewed(repo: str, state_path: Path) -> int:
    prs = get_open_prs(repo)
    for pr in prs:
        num = pr["number"]
        if not is_reviewed(repo, num, state_path):
            print(num)
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1].strip().lower() in ("--help", "-h"):
        print("Usage: pr_review.py {check|review <PR#>|post <PR#>|status|list-unreviewed}", file=sys.stderr)
        print("Environment: PR_REVIEW_REPO, PR_REVIEW_DIR, PR_REVIEW_STATE, PR_REVIEW_OUTDIR", file=sys.stderr)
        return 0 if (len(sys.argv) >= 2 and sys.argv[1].strip().lower() in ("--help", "-h")) else 1
    sub = sys.argv[1].strip().lower()
    try:
        repo, local_dir, state_path, outdir = get_repo_and_dirs()
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    try:
        if sub == "check":
            return cmd_check(repo, state_path, outdir, local_dir)
        if sub == "review":
            if len(sys.argv) < 3:
                print("Error: PR number required", file=sys.stderr)
                return 2
            try:
                pr_num = int(sys.argv[2])
            except ValueError:
                print("Error: PR number must be an integer", file=sys.stderr)
                return 2
            return cmd_review(repo, pr_num, state_path, outdir, local_dir)
        if sub == "post":
            if len(sys.argv) < 3:
                print("Error: PR number required", file=sys.stderr)
                return 2
            try:
                pr_num = int(sys.argv[2])
            except ValueError:
                print("Error: PR number must be an integer", file=sys.stderr)
                return 2
            return cmd_post(repo, pr_num, outdir)
        if sub == "status":
            return cmd_status(repo, state_path)
        if sub == "list-unreviewed":
            return cmd_list_unreviewed(repo, state_path)
    except ReviewInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print("Usage: pr_review.py {check|review <PR#>|post <PR#>|status|list-unreviewed}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
