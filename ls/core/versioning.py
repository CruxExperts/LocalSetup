from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Iterable

from .git_subprocess import run_git
from . import versioning_sync as _sync
from .provenance_source import is_generated_output_path
from . import versioning_sequence as _sequence
from . import versioning_policy as _policy
from .versioning_constants import (
    BREAKING_CHANGE_RE,
    BREAKING_SUBJECT_RE,
    INTERNAL_PATCH_PATHS,
    KNOWN_PATCH_TYPES,
    RELEASE_TOOLING_PATHS,
    RELEASE_TYPE_RE,
    VERSION_SYNC_PREFIX,
    ZERO_SHA,
)
from .versioning_models import CommitInfo, SemVer


def _run_git(repo_root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_git(
        repo_root,
        args,
        text=True,
        capture_output=True,
        check=check,
    )


def _git_text(repo_root: Path, args: list[str], *, check: bool = True) -> str:
    return _run_git(repo_root, args, check=check).stdout.strip()


def read_version(repo_root: Path, ref: str | None = None) -> SemVer:
    if ref:
        completed = _run_git(repo_root, ["show", f"{ref}:VERSION"], check=False)
        if completed.returncode == 0 and completed.stdout.strip():
            return SemVer.parse(completed.stdout.strip())
    return SemVer.parse((repo_root / "VERSION").read_text(encoding="utf-8").strip())


def _rev_parse(repo_root: Path, ref: str) -> str | None:
    completed = _run_git(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return None


def _base_result(*, status: str, strategy: str, ref: str | None, sha: str, attempts: list[dict[str, str]]) -> dict[str, object]:
    return {
        "status": status,
        "strategy": strategy,
        "ref": ref,
        "sha": sha,
        "attempts": attempts,
    }


def _try_base_ref(repo_root: Path, *, strategy: str, ref: str, attempts: list[dict[str, str]]) -> tuple[str, dict[str, object]] | None:
    sha = _rev_parse(repo_root, ref)
    attempts.append({"strategy": strategy, "ref": ref, "status": "resolved" if sha else "unresolved"})
    if sha:
        return sha, _base_result(status="resolved", strategy=strategy, ref=ref, sha=sha, attempts=attempts)
    return None


def _base_or_merge_base(
    repo_root: Path,
    *,
    base: str | None,
    head: str,
    strategy: str,
    ref: str,
    sha: str,
    attempts: list[dict[str, str]],
) -> dict[str, object]:
    if base == ZERO_SHA:
        merge_base = _run_git(repo_root, ["merge-base", sha, head], check=False)
        if merge_base.returncode == 0 and merge_base.stdout.strip():
            attempts.append({"strategy": "merge_base", "ref": ref, "status": "resolved"})
            merge_sha = merge_base.stdout.strip()
            return {
                "base": merge_sha,
                "base_resolution": _base_result(
                    status="resolved",
                    strategy="merge_base",
                    ref=ref,
                    sha=merge_sha,
                    attempts=attempts,
                ),
            }
    return {
        "base": sha,
        "base_resolution": _base_result(status="resolved", strategy=strategy, ref=ref, sha=sha, attempts=attempts),
    }


def _symbolic_remote_head(repo_root: Path, remote: str) -> str | None:
    completed = _run_git(repo_root, ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"], check=False)
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    show = _run_git(repo_root, ["remote", "show", "-n", remote], check=False)
    if show.returncode != 0:
        return None
    for line in show.stdout.splitlines():
        line = line.strip()
        if line.startswith("HEAD branch:"):
            branch = line.split(":", 1)[1].strip()
            if branch and branch != "(unknown)":
                return f"{remote}/{branch}"
    return None


def default_base_ref(repo_root: Path) -> str:
    resolution = resolve_base_with_metadata(repo_root)
    ref = resolution["base_resolution"].get("ref")
    return str(ref or "HEAD")


def resolve_head(repo_root: Path, head: str | None = None) -> str:
    return _git_text(repo_root, ["rev-parse", head or "HEAD"])


def resolve_base_with_metadata(repo_root: Path, base: str | None = None, head: str | None = None) -> dict[str, object]:
    resolved_head = resolve_head(repo_root, head)
    attempts: list[dict[str, str]] = []

    if base and base != ZERO_SHA:
        resolved = _try_base_ref(repo_root, strategy="explicit", ref=base, attempts=attempts)
        if resolved:
            return {"base": resolved[0], "base_resolution": resolved[1]}
        raise ValueError(f"explicit base ref did not resolve: {base}")

    upstream = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], check=False)
    if upstream.returncode == 0 and upstream.stdout.strip():
        ref = upstream.stdout.strip()
        resolved = _try_base_ref(repo_root, strategy="upstream", ref=ref, attempts=attempts)
        if resolved:
            return _base_or_merge_base(repo_root, base=base, head=resolved_head, strategy="upstream", ref=ref, sha=resolved[0], attempts=attempts)

    remotes = _run_git(repo_root, ["remote"], check=False)
    remote_names = [line.strip() for line in remotes.stdout.splitlines() if line.strip()] if remotes.returncode == 0 else []
    remote_set = set(remote_names)
    ordered_remotes = [remote for remote in ["origin", *remote_names] if remote in remote_set]
    ordered_remotes = list(dict.fromkeys(ordered_remotes))
    for remote in ordered_remotes:
        remote_head = _symbolic_remote_head(repo_root, remote)
        if not remote_head:
            attempts.append({"strategy": "remote_default", "ref": f"{remote}/HEAD", "status": "unresolved"})
            continue
        resolved = _try_base_ref(repo_root, strategy="remote_default", ref=remote_head, attempts=attempts)
        if resolved:
            return _base_or_merge_base(repo_root, base=base, head=resolved_head, strategy="remote_default", ref=remote_head, sha=resolved[0], attempts=attempts)

    for branch in ("main", "master"):
        resolved = _try_base_ref(repo_root, strategy=f"local_{branch}", ref=branch, attempts=attempts)
        if resolved:
            return _base_or_merge_base(repo_root, base=base, head=resolved_head, strategy=f"local_{branch}", ref=branch, sha=resolved[0], attempts=attempts)

    attempts.append({"strategy": "head", "ref": "HEAD", "status": "no_comparison_base"})
    return {
        "base": resolved_head,
        "base_resolution": _base_result(
            status="no_comparison_base",
            strategy="head",
            ref="HEAD",
            sha=resolved_head,
            attempts=attempts,
        )
        | {"reason": "no explicit, upstream, remote default, local main, or local master comparison base resolved"},
    }


def resolve_base(repo_root: Path, base: str | None = None, head: str | None = None) -> str:
    return str(resolve_base_with_metadata(repo_root, base=base, head=head)["base"])


def list_commits(repo_root: Path, base: str, head: str) -> list[CommitInfo]:
    if base == head:
        return []
    raw = _run_git(repo_root, ["log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", f"{base}..{head}"]).stdout
    commits: list[CommitInfo] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f", 2)
        if len(parts) != 3:
            continue
        commits.append(CommitInfo(sha=parts[0], subject=parts[1], body=parts[2]))
    return commits



def list_integrated_commits(repo_root: Path, base: str, head: str) -> list[CommitInfo]:
    if base == head:
        return []
    raw = _run_git(repo_root, ["log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", f"{base}..{head}"]).stdout
    commits: list[CommitInfo] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f", 2)
        if len(parts) != 3:
            continue
        commits.append(CommitInfo(sha=parts[0], subject=parts[1], body=parts[2]))
    graph = _git_text(repo_root, ["rev-list", "--parents", head, "^" + base])
    parents = {parts[0]: parts[1:] for line in graph.splitlines() if (parts := line.split())}
    by_sha = {commit.sha: commit for commit in commits}
    return [by_sha[sha] for sha in _sequence.integration_order(parents, head)]


def classify_commit(subject: str, body: str = "") -> str:
    if subject.startswith("Merge "):
        return "none"
    if subject.startswith("Revert "):
        return "none"
    if subject.startswith(VERSION_SYNC_PREFIX):
        return "none"
    release_type = RELEASE_TYPE_RE.search(body)
    if release_type:
        return release_type.group(1).lower()
    if BREAKING_SUBJECT_RE.match(subject):
        return "major"
    if BREAKING_CHANGE_RE.search(body):
        return "major"
    match = re.match(r"^([a-zA-Z]+)(?:\([^)]+\))?:", subject)
    if not match:
        return "patch"
    commit_type = match.group(1).lower()
    if commit_type == "feat":
        return "minor"
    if commit_type in KNOWN_PATCH_TYPES:
        return "patch"
    return "patch"


def release_type_override(body: str) -> str | None:
    release_type = RELEASE_TYPE_RE.search(body)
    if release_type:
        return release_type.group(1).lower()
    return None


def requires_release_type(commit: CommitInfo) -> bool:
    if release_type_override(commit.body):
        return False
    return bool(BREAKING_SUBJECT_RE.match(commit.subject) or BREAKING_CHANGE_RE.search(commit.body))


def changed_files(repo_root: Path, sha: str) -> list[str]:
    output = _git_text(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", sha])
    return [line for line in output.splitlines() if line]


def _is_internal_patch_file(path: str) -> bool:
    return path in RELEASE_TOOLING_PATHS or any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in INTERNAL_PATCH_PATHS)


def classify_commit_for_release(repo_root: Path, commit: CommitInfo) -> str:
    if commit.subject.startswith("Merge "):
        return "none"
    if commit.subject.startswith("Revert "):
        return "none"
    if commit.subject.startswith(VERSION_SYNC_PREFIX):
        return "none"
    if release_type := release_type_override(commit.body):
        return release_type
    return "patch"


def _source_classification(commit: CommitInfo) -> str:
    override = _sequence.metadata(commit.body)[0]
    breaking = BREAKING_SUBJECT_RE.match(commit.subject) or BREAKING_CHANGE_RE.search(commit.body)
    if breaking:
        if override is not None and override != "major":
            raise ValueError(f"Breaking commit {commit.sha} requires Release-Type: major")
        return "major"
    if override is not None:
        return override
    # A published revert is a new maintenance outcome; merge-like subjects alone
    # do not hide ordinary single-parent source changes.
    return "minor" if re.match(r"^feat(?:\([^)]+\))?:", commit.subject) else "patch"


def release_type_required_diagnostics(commits: Iterable[CommitInfo]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    for commit in commits:
        if requires_release_type(commit):
            diagnostics.append(
                {
                    "sha": commit.sha,
                    "subject": commit.subject,
                    "message": "Breaking release markers require an explicit Release-Type: major|minor|patch|none trailer under patch-default versioning.",
                }
            )
    return diagnostics


def bump_rank(bump_type: str) -> int:
    return {"none": 0, "patch": 1, "minor": 2, "major": 3}[bump_type]


def max_bump(bumps: Iterable[str]) -> str:
    result = "none"
    for bump in bumps:
        if bump_rank(bump) > bump_rank(result):
            result = bump
    return result


def _reverted_subject(subject: str) -> str | None:
    match = re.fullmatch(r'Revert "(.+)"', subject)
    if match:
        return match.group(1)
    return None



def net_unreleased_commits(commits: list[CommitInfo]) -> tuple[list[CommitInfo], list[dict[str, str]]]:
    remaining: list[CommitInfo] = []
    canceled: list[dict[str, str]] = []
    for commit in commits:
        reverted = _reverted_subject(commit.subject)
        if reverted:
            for index in range(len(remaining) - 1, -1, -1):
                original = remaining[index]
                if original.subject == reverted:
                    canceled.append(
                        {
                            "revert_sha": commit.sha,
                            "original_sha": original.sha,
                            "subject": reverted,
                        }
                    )
                    del remaining[index]
                    break
            else:
                remaining.append(commit)
            continue
        remaining.append(commit)
    return remaining, canceled


def version_from_sync_commit(subject: str) -> SemVer | None:
    if not subject.startswith(VERSION_SYNC_PREFIX):
        return None
    raw = subject.removeprefix(VERSION_SYNC_PREFIX).strip()
    if not raw:
        return None
    try:
        return SemVer.parse(raw)
    except ValueError:
        return None


def plan_version(repo_root: Path, *, base: str | None = None, head: str | None = None,
                 ref: str | None = None, policy: str | None = None) -> dict:
    from .versioning_policy import plan
    return plan(repo_root, base=base, head=head, ref=ref, policy=policy)


def _plan_version_legacy(repo_root: Path, *, base: str | None = None, head: str | None = None, ref: str | None = None) -> dict:
    resolved_head = resolve_head(repo_root, head)
    base_payload = resolve_base_with_metadata(repo_root, base, resolved_head)
    resolved_base = str(base_payload["base"])
    base_resolution = base_payload["base_resolution"]
    commits = list_commits(repo_root, resolved_base, resolved_head)
    net_commits, canceled = net_unreleased_commits(commits)
    release_type_required = release_type_required_diagnostics(net_commits)
    bump = max_bump(classify_commit_for_release(repo_root, commit) for commit in net_commits)
    base_version = read_version(repo_root, resolved_base)
    target = base_version.bump(bump)
    current = read_version(repo_root, resolved_head)
    worktree = read_version(repo_root)
    sync_commits = [commit for commit in commits if commit.subject.startswith(VERSION_SYNC_PREFIX)]
    version_sync_present = bool(sync_commits)
    version_sync_matches_target = all(
        version_from_sync_commit(commit.subject) == target for commit in sync_commits
    )
    head_version_matches_target = current == target
    head_version_required = version_sync_present or bump != "none"
    ok = (
        (not head_version_required or head_version_matches_target)
        and version_sync_matches_target
        and not release_type_required
    )
    return {
        "ok": ok,
        "policy": "patch-default",
        "ref": ref,
        "base": resolved_base,
        "head": resolved_head,
        "base_resolution": base_resolution,
        "base_version": str(base_version),
        "current_version": str(current),
        "worktree_version": str(worktree),
        "target_version": str(target),
        "major_minor": target.major_minor,
        "bump": bump,
        "release_type_required": bool(release_type_required),
        "release_type_required_commits": release_type_required,
        "version_sync_present": version_sync_present,
        "version_sync_matches_target": version_sync_matches_target,
        "commit_count": len(commits),
        "net_commit_count": len(net_commits),
        "canceled_reverts": canceled,
        "commits": [
            {
                "sha": commit.sha,
                "subject": commit.subject,
                "bump": classify_commit_for_release(repo_root, commit),
                "raw_bump": classify_commit(commit.subject, commit.body),
                "release_type_required": requires_release_type(commit),
                "files": changed_files(repo_root, commit.sha),
            }
            for commit in net_commits
        ],
    }



def sync_version_files(repo_root: Path, target_version: str) -> dict:
    _policy.guard_target(repo_root, target_version)
    return _sync.sync_version_files(repo_root, target_version)


def prepare_version_sync_candidate(repo_root: Path, target_version: str) -> dict:
    """Synchronize direct version surfaces without staging, committing, or generating docs.

    ``publish-preflight`` uses this preparation phase when invoked without
    ``--fix``.  The resulting candidate is deliberately left for maintainer
    review and a separate generated-document receipt.
    """
    _policy.guard_target(repo_root, target_version)
    target = SemVer.parse(target_version)
    changed: list[str] = []

    direct_updates = [
        ("VERSION", None, f"{target}\n"),
        ("pyproject.toml", r'(?m)^version = "[0-9]+\.[0-9]+\.[0-9]+"$', f'version = "{target}"'),
        ("README.md", r"(?m)^\*\*Version:\*\* [0-9]+\.[0-9]+\.[0-9]+<br>$", f"**Version:** {target}<br>"),
        ("ls/README.md", r"(?m)^\*\*Version:\*\* [0-9]+\.[0-9]+\.[0-9]+<br>$", f"**Version:** {target}<br>"),
        (
            "ls/docs/VERSIONING.md",
            r"(?m)^- Current value: `[0-9]+\.[0-9]+\.[0-9]+`$",
            f"- Current value: `{target}`",
        ),
        (
            "uv.lock",
            r'(?m)(^\[\[package\]\]\nname = "localsetup"\nversion = ")[0-9]+\.[0-9]+\.[0-9]+(")',
            rf"\g<1>{target}\2",
        ),
    ]
    for relative_path, pattern, replacement in direct_updates:
        path = repo_root / relative_path
        before = path.read_text(encoding="utf-8")
        after = replacement if pattern is None else re.sub(pattern, replacement, before)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed.append(relative_path)

    changed.extend(
        _sync.update_doc_frontmatter_versions(
            repo_root,
            target,
            include_path=lambda relative_path: not is_generated_output_path(relative_path),
        )
    )
    return {
        "version": str(target),
        "major_minor": target.major_minor,
        "changed_candidates": sorted(set(changed)),
    }


def check_version_files(repo_root: Path, target_version: str) -> dict:
    return _sync.check_version_files(
        repo_root,
        target_version,
        git_text=_git_text,
        run_git=_run_git,
        sync=sync_version_files,
    )


def stage_version_files(repo_root: Path) -> None:
    _sync.stage_version_files(repo_root, run_git=_run_git)


def commit_version_sync(repo_root: Path, target_version: str) -> str | None:
    _policy.guard_target(repo_root, target_version)
    return _sync.commit_version_sync(
        repo_root,
        target_version,
        git_text=_git_text,
        run_git=_run_git,
        resolve_head=resolve_head,
        stage=stage_version_files,
    )


def commit_generated_docs_refresh(repo_root: Path, *, message: str = "docs: refresh generated artifacts") -> str | None:
    return _sync.commit_generated_docs_refresh(
        repo_root,
        git_text=_git_text,
        run_git=_run_git,
        resolve_head=resolve_head,
        stage=stage_version_files,
        message=message,
    )


def publish_preflight(repo_root: Path, *, base: str | None = None, head: str | None = None, fix: bool = False) -> dict:
    """
    Run the publish-time version/docs gate agents should satisfy before pushing.

    Without ``--fix``, a clean worktree is prepared as an unstaged direct
    version-sync candidate for review.  The fix mode intentionally mirrors the
    CI order: sync the release version first, then refresh generated artifacts
    from that version-sync parent and commit both accepted slices.
    """
    plan = plan_version(repo_root, base=base, head=head)
    result: dict = {"ok": False, "fixed": False, "commits": [], "plan": plan}
    if not plan.get("repairable", True):
        result["reason"] = "invalid_release_history"
        return result
    if not plan["ok"] and plan.get("release_type_required"):
        result["reason"] = "release_type_required"
        return result

    target = str(plan["target_version"])
    if not fix:
        dirty = _git_text(repo_root, ["status", "--porcelain"])
        if dirty:
            result["reason"] = "dirty_worktree"
            result["dirty_worktree"] = dirty
            return result
        prepared = prepare_version_sync_candidate(repo_root, target)
        prepared_paths = prepared["changed_candidates"]
        result["prepared"] = bool(prepared_paths)
        result["prepared_paths"] = prepared_paths
        if prepared_paths:
            result["version_check"] = {
                "ok": False,
                "mode": "direct_sync_candidate",
                "target_version": target,
            }
            result["reason"] = "prepared_not_ready"
            return result
        check = check_version_files(repo_root, target)
        result["version_check"] = check
        result["ok"] = bool(plan["ok"] and check["ok"])
        _check_release_documentation(repo_root, target, result)
        return result

    if fix:
        dirty = _git_text(repo_root, ["status", "--porcelain"])
        if dirty:
            result["reason"] = "dirty_worktree"
            result["dirty_worktree"] = dirty
            return result
        if not plan["ok"]:
            sync_version_files(repo_root, target)
            commit = commit_version_sync(repo_root, target)
            if commit:
                result["commits"].append({"type": "version_sync", "sha": commit})
                result["fixed"] = True
        sync_version_files(repo_root, target)
        commit = commit_generated_docs_refresh(repo_root, message="docs: refresh release version artifacts")
        if commit:
            result["commits"].append({"type": "generated_docs", "sha": commit})
            result["fixed"] = True
        plan = plan_version(repo_root, base=base, head="HEAD")
        result["plan"] = plan
        target = str(plan["target_version"])

    check = check_version_files(repo_root, target)
    result["version_check"] = check
    result["ok"] = bool(plan["ok"] and check["ok"])
    _check_release_documentation(repo_root, target, result)
    return result


def _check_release_documentation(repo_root: Path, target: str, result: dict) -> None:
    """Opted-in source repositories must validate prose as well as versions."""
    readme = repo_root / "README.md"
    if not (repo_root / ".localsetup-release.json").is_file() or not readme.is_file() or "<!-- release-summary:start -->" not in readme.read_text(encoding="utf-8"):
        return
    from .release_docs.content import load_record
    from .release_docs.checks import check
    try:
        report = check(repo_root, load_record(repo_root, target))
    except (ValueError, OSError) as exc:
        report = {"ok": False, "findings": [{"code": "release_documentation", "message": str(exc)}]}
    result["release_documentation"] = report
    result["ok"] = bool(result["ok"] and report["ok"])


def push_lines_to_plans(repo_root: Path, lines: str) -> list[dict]:
    plans: list[dict] = []
    for raw_line in lines.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if local_sha == ZERO_SHA:
            continue
        plans.append(plan_version(repo_root, base=remote_sha, head=local_sha, ref=f"{local_ref}->{remote_ref}"))
    return plans


def print_json(payload: dict | list[dict]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
