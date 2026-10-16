"""Deterministic integration order and sequential logical release slices."""
from __future__ import annotations

import re
from pathlib import Path

from .git_subprocess import run_git
from .provenance_source import is_generated_output_path, _commit_receipt_has_only_generated_receipt_changes
from .versioning_constants import VERSION_SYNC_PREFIX, BREAKING_SUBJECT_RE, BREAKING_CHANGE_RE

from .versioning_models import CommitInfo, SemVer

RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}
SLICE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
REVERT = re.compile(r"^This reverts commit ([0-9a-f]{40})\.$", re.MULTILINE)


def metadata(body: str) -> tuple[str | None, str | None]:
    """Reject ambiguous release metadata rather than selecting one occurrence."""
    values: dict[str, str] = {}
    for line in body.splitlines():
        match = re.match(r"^\s*(Release-Type|Release-Slice)\s*:(.*)$", line, re.IGNORECASE)
        if not match:
            continue
        key, value = match[1].lower(), match[2].strip()
        if key in values:
            raise ValueError(f"Duplicate {key} metadata")
        if key == "release-type":
            value = value.lower()
            if value not in RANK:
                raise ValueError("Release-Type requires major|minor|patch|none")
        elif not SLICE.fullmatch(value):
            raise ValueError("Release-Slice requires a lowercase identifier of at most 128 characters")
        values[key] = value
    return values.get("release-type"), values.get("release-slice")


def integration_order(parents: dict[str, list[str]], head: str) -> list[str]:
    """Visit first-parent history, then newly integrated side ancestry, then merge.

    Parent order comes from the commit object, never author/committer timestamps.
    Parents outside the selected ancestry range are already published boundaries.
    """
    result: list[str] = []
    seen: set[str] = set()
    stack = [(head, False)]
    while stack:
        sha, ready = stack.pop()
        if sha not in parents or sha in seen:
            continue
        if ready:
            seen.add(sha)
            result.append(sha)
        else:
            stack.append((sha, True))
            stack.extend((parent, False) for parent in reversed(parents[sha]))
    return result


def cancel_reverts(commits: list[CommitInfo], excluded: set[str], *, repo_root: Path, overrides: dict | None = None) -> tuple[list[CommitInfo], list[dict[str, str]]]:
    """Cancel exact native Git reverts; partial grouped outcomes need review."""
    by_sha = {commit.sha: commit for commit in commits}
    canceled: set[str] = set()
    pairs = []
    integrated: set[str] = set()
    for commit in commits:
        integrated.add(commit.sha)
        targets = REVERT.findall(commit.body)
        if len(targets) > 1:
            raise ValueError(f"Ambiguous revert targets in {commit.sha}")
        if commit.subject.startswith('Revert "') and not targets:
            raise ValueError(f"Revert {commit.sha} requires its exact native Git reverted SHA")
        if not targets or targets[0] not in by_sha:
            continue
        target = targets[0]
        if target in excluded or target in canceled or REVERT.search(by_sha[target].body):
            raise ValueError(f"Revert {commit.sha} requires an explicit logical outcome for {target}")
        if target not in integrated or target == commit.sha:
            raise ValueError(f"Revert {commit.sha} names a commit not yet integrated")
        verify_inverse(repo_root, target, commit.sha)
        canceled.update((target, commit.sha))
        pairs.append({"revert_sha": commit.sha, "original_sha": target, "subject": by_sha[target].subject})
    groups: dict[str, set[str]] = {}
    for commit in commits:
        if commit.sha not in excluded and not REVERT.search(commit.body):
            _, identity = metadata(commit.body)
            groups.setdefault((overrides or {}).get(commit.sha, {}).get("slice", identity or commit.sha), set()).add(commit.sha)
    for identity, members in groups.items():
        if members & canceled and not members <= canceled:
            raise ValueError(f"Partially reverted Release-Slice {identity} requires an explicit accepted outcome")
    return [commit for commit in commits if commit.sha not in canceled], pairs


def fold(commits: list[CommitInfo], classifications: dict[str, str], base: SemVer, *, overrides: dict | None = None) -> tuple[SemVer, list[dict]]:
    groups: dict[str, dict] = {}
    for commit in commits:
        _, identity = metadata(commit.body)
        classification = classifications[commit.sha]
        if classification == "none":
            continue
        identity = (overrides or {}).get(commit.sha, {}).get("slice", identity or commit.sha)
        group = groups.setdefault(identity, {"slice": identity, "anchor": commit.sha,
                                             "source_shas": [], "classification": "none"})
        group["source_shas"].append(commit.sha)
        if RANK[classification] > RANK[group["classification"]]:
            group["classification"] = classification
    current = base
    rows = []
    for group in groups.values():
        following = current.bump(group["classification"])
        rows.append({**group, "before_version": str(current), "after_version": str(following)})
        current = following
    return current, rows


def normalize_version_surface(path: str, text: str) -> str:
    """Erase only canonical sync-owned values when comparing authored files."""
    if path == "VERSION":
        return "<version>\n" if re.fullmatch(r"\d+\.\d+\.\d+\n?", text) else text
    patterns = {
        "pyproject.toml": r'(?m)^version = "[0-9]+\.[0-9]+\.[0-9]+"$',
        "README.md": r"(?m)^\*\*Version:\*\* [0-9]+\.[0-9]+\.[0-9]+<br>$",
        "ls/README.md": r"(?m)^\*\*Version:\*\* [0-9]+\.[0-9]+\.[0-9]+<br>$",
        "ls/docs/VERSIONING.md": r"(?m)^- Current value: `[0-9]+\.[0-9]+\.[0-9]+`$",
    }
    if path in patterns:
        text = re.sub(patterns[path], "<version>", text)
    if path == "uv.lock":
        text = re.sub(r'(?m)(^\[\[package\]\]\nname = "localsetup"\nversion = ")[0-9]+\.[0-9]+\.[0-9]+(")',
                      r'\g<1><version>\2', text)
    if path.startswith("ls/docs/") and path.endswith(".md") and text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = re.sub(r'(?m)^version:\s*["\']?[0-9]+(?:\.[0-9]+){1,2}["\']?\s*$',
                          'version: <version>', text[:end]) + text[end:]
    if path in {"README.md", "ls/docs/README.md", "ls/docs/FEATURES.md"}:
        if text.count("<!-- facts-block:start -->") == text.count("<!-- facts-block:end -->") == 1:
            text = re.sub(r"(?s)(<!-- facts-block:start -->).*?(<!-- facts-block:end -->)", r"\1<facts>\2", text)
    return text


def _run_git(repo_root, arguments, *, check=True):
    return run_git(repo_root, arguments, text=True, capture_output=True, check=check)


def _git_text(repo_root, arguments):
    return _run_git(repo_root, arguments).stdout.strip()


def exclusion(repo_root: Path, commit: CommitInfo) -> str | None:
    metadata(commit.body)
    parents = _git_text(repo_root, ["rev-list", "--parents", "-n", "1", commit.sha]).split()[1:]
    if len(parents) > 1:
        return "merge"
    files = _git_text(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", commit.sha]).splitlines()
    if commit.subject.startswith(VERSION_SYNC_PREFIX):
        fixed = {"VERSION", "pyproject.toml", "uv.lock", "README.md", "ls/README.md"}
        if any(path not in fixed and not path.startswith("ls/docs/")
               and not is_generated_output_path(path) for path in files):
            raise ValueError(f"Version-sync commit {commit.sha} changes non-version paths")
        for path in files:
            if is_generated_output_path(path):
                continue
            before = _run_git(repo_root, ["show", f"{commit.sha}^:{path}"], check=False)
            after = _run_git(repo_root, ["show", f"{commit.sha}:{path}"], check=False)
            structural = _git_text(repo_root, ["diff", "--summary", commit.sha + "^", commit.sha, "--", path])
            if (before.returncode or after.returncode or structural
                    or normalize_version_surface(path, before.stdout)
                    != normalize_version_surface(path, after.stdout)):
                raise ValueError(f"Version-sync commit {commit.sha} changes authored content in {path}")
        return "version_sync"
    if files and all(is_generated_output_path(path)
                     or _commit_receipt_has_only_generated_receipt_changes(repo_root, commit.sha, path)
                     for path in files):
        return "generated_receipt"
    return None



def committed_version(repo_root: Path, ref: str) -> SemVer:
    """A selected release tree cannot borrow VERSION from the loose worktree."""
    result = _run_git(repo_root, ["show", f"{ref}:VERSION"], check=False)
    if result.returncode:
        raise ValueError(f"Release commit {ref} has no committed VERSION")
    return SemVer.parse(result.stdout)


def verify_inverse(repo_root: Path, original: str, reverted: str) -> None:
    """Exclude only an exact single-parent path/blob/mode inverse, not a claim."""
    for sha in (original, reverted):
        parents = _git_text(repo_root, ["rev-list", "--parents", "-n", "1", sha]).split()[1:]
        if len(parents) != 1:
            raise ValueError(f"Revert {reverted} requires single-parent inverse evidence for {original}")
    options = ["diff-tree", "--raw", "--no-commit-id", "--no-renames", "--no-abbrev",
               "--no-ext-diff", "--no-textconv", "-r", "-z"]
    forward = run_git(repo_root, [*options, original + "^", original],
                      text=False, capture_output=True, check=True).stdout
    reverse = run_git(repo_root, [*options, reverted, reverted + "^"],
                      text=False, capture_output=True, check=True).stdout
    if not forward or forward != reverse:
        raise ValueError(f"Revert {reverted} is not an exact inverse of {original}; reconcile its accepted outcome")


def requires_major_decision(commit: CommitInfo) -> bool:
    override, _ = metadata(commit.body)
    return bool((BREAKING_SUBJECT_RE.match(commit.subject) or BREAKING_CHANGE_RE.search(commit.body))
                and override != "major")
