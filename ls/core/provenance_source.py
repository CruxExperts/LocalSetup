from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .git_subprocess import run_git
from .source import source_commit

GENERATED_SOURCE_DIRTY_PATHS = {
    "assets/README.md",
    "ls/docs/SKILLS.md",
    "ls/docs/WORKFLOW_QUICK_REF.md",
    "ls/docs/WORKFLOW_REGISTRY.md",
    "ls/docs/migration/skill-alias-map.md",
}
GENERATED_SOURCE_DIRTY_PREFIXES = (
    "ls/docs/_generated/",
)
GENERATED_RECEIPT_PATHS = {
    "README.md",
    "ls/docs/FEATURES.md",
    "ls/docs/README.md",
}
FACTS_BLOCK_START = "<!-- facts-block:start -->"
FACTS_BLOCK_END = "<!-- facts-block:end -->"
VERSION_SYNC_SUBJECT_PREFIX = "chore: sync release version "
GENERATED_DOCS_SUBJECT_PREFIX = "docs: refresh "
HUNK_HEADER_RE = re.compile(
    r"@@ -(?P<before_start>\d+)(?:,(?P<before_count>\d+))? "
    r"\+(?P<after_start>\d+)(?:,(?P<after_count>\d+))? @@(?: .*)?"
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_tree_sha(repo_root: Path) -> str:
    return tree_sha_for_ref(repo_root, "HEAD")


def tree_sha_for_ref(repo_root: Path, ref: str) -> str:
    completed = run_git(
        repo_root,
        ["rev-parse", f"{ref}^{{tree}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.strip()


def git_text(repo_root: Path, args: list[str]) -> str | None:
    completed = run_git(
        repo_root,
        args,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def source_tag_for_ref(repo_root: Path, ref: str) -> str | None:
    return git_text(repo_root, ["describe", "--tags", "--exact-match", ref])


def status_entry_paths(line: str) -> list[str]:
    if len(line) < 4:
        return []
    path = line[3:].strip()
    if " -> " in path:
        return [part.strip() for part in path.split(" -> ", 1)]
    return [path]


def is_generated_output_path(path: str) -> bool:
    normalized = path.strip().strip('"')
    return normalized in GENERATED_SOURCE_DIRTY_PATHS or any(
        normalized.startswith(prefix) for prefix in GENERATED_SOURCE_DIRTY_PREFIXES
    )


def is_generated_receipt_path(path: str) -> bool:
    return path.strip().strip('"') in GENERATED_RECEIPT_PATHS


def _facts_block_bounds(lines: list[str]) -> tuple[int, int] | None:
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == FACTS_BLOCK_START]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == FACTS_BLOCK_END]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None
    return starts[0], ends[0]


def _git_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def _range_is_within_facts_block(
    start: int,
    count: int,
    bounds: tuple[int, int],
) -> bool:
    block_start, block_end = bounds
    if count == 0:
        insertion_index = start
        return block_start < insertion_index <= block_end
    start_index = start - 1
    end_index = start_index + count
    return block_start < start_index and end_index <= block_end


def _hunk_preserves_eol_style(removed: list[bool], added: list[bool]) -> bool:
    return len(removed) != len(added) or removed == added


def _diff_hunks_are_within_facts_block(
    diff: str,
    before: list[str],
    after: list[str],
) -> bool:
    before_bounds = _facts_block_bounds(before)
    after_bounds = _facts_block_bounds(after)
    if not before_bounds or not after_bounds:
        return False

    lines = diff.split("\n")
    found_hunk = False
    in_hunk = False
    before_count = 0
    after_count = 0
    consumed_before = 0
    consumed_after = 0
    removed_eol_styles: list[bool] = []
    added_eol_styles: list[bool] = []
    for index, line in enumerate(lines):
        if line.startswith("@@"):
            if in_hunk and (
                consumed_before != before_count
                or consumed_after != after_count
                or not _hunk_preserves_eol_style(
                    removed_eol_styles,
                    added_eol_styles,
                )
            ):
                return False
            match = HUNK_HEADER_RE.fullmatch(line)
            if not match:
                return False
            try:
                before_start = int(match.group("before_start"))
                before_count = int(match.group("before_count") or 1)
                after_start = int(match.group("after_start"))
                after_count = int(match.group("after_count") or 1)
            except ValueError:
                return False
            found_hunk = True
            in_hunk = True
            consumed_before = 0
            consumed_after = 0
            removed_eol_styles = []
            added_eol_styles = []
            if not (
                _range_is_within_facts_block(
                    before_start,
                    before_count,
                    before_bounds,
                )
                and _range_is_within_facts_block(
                    after_start,
                    after_count,
                    after_bounds,
                )
            ):
                return False
            continue
        if not in_hunk:
            continue
        if line.startswith("-"):
            consumed_before += 1
            removed_eol_styles.append(line.endswith("\r"))
        elif line.startswith("+"):
            consumed_after += 1
            added_eol_styles.append(line.endswith("\r"))
        elif line.startswith(" "):
            consumed_before += 1
            consumed_after += 1
        elif line == r"\ No newline at end of file":
            continue
        elif line == "" and index == len(lines) - 1:
            continue
        else:
            return False
        if consumed_before > before_count or consumed_after > after_count:
            return False
    return (
        found_hunk
        and consumed_before == before_count
        and consumed_after == after_count
        and _hunk_preserves_eol_style(removed_eol_styles, added_eol_styles)
    )


def _git_file_text(repo_root: Path, file_spec: str) -> str | None:
    try:
        completed = run_git(
            repo_root,
            ["show", file_spec],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _git_diff_text(repo_root: Path, args: list[str]) -> str | None:
    try:
        completed = run_git(
            repo_root,
            args,
            capture_output=True,
            check=False,
        )
    except (OSError, UnicodeDecodeError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _receipt_has_non_content_change(repo_root: Path, path: str) -> bool:
    for args in (
        ["diff", "--no-color", "--no-textconv", "--summary", "--cached", "HEAD", "--", path],
        ["diff", "--no-color", "--no-textconv", "--summary", "HEAD", "--", path],
    ):
        diff = _git_diff_text(repo_root, args)
        if diff is None or diff.strip():
            return True
    return False


def has_only_generated_facts_block_changes(repo_root: Path, path: str) -> bool:
    """Whether index and worktree changes are confined to a receipt facts block."""
    normalized = path.strip().strip('"')
    if normalized not in GENERATED_RECEIPT_PATHS:
        return False

    working_path = repo_root / normalized
    if not working_path.is_file():
        return False
    try:
        working_text = working_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    head_text = _git_file_text(repo_root, f"HEAD:{normalized}")
    index_text = _git_file_text(repo_root, f":{normalized}")
    if head_text is None or index_text is None:
        return False
    if _receipt_has_non_content_change(repo_root, normalized):
        return False


    index_diff = _git_diff_text(
        repo_root,
        [
            "diff",
            "--output-indicator-old=-",
            "--output-indicator-new=+",
            "--output-indicator-context= ",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=0",
            "--cached",
            "HEAD",
            "--",
            normalized,
        ],
    )
    working_diff = _git_diff_text(
        repo_root,
        [
            "diff",
            "--output-indicator-old=-",
            "--output-indicator-new=+",
            "--output-indicator-context= ",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=0",
            "HEAD",
            "--",
            normalized,
        ],
    )
    if index_diff is None or working_diff is None:
        return False

    before = _git_lines(head_text)
    index_after = _git_lines(index_text)
    working_after = _git_lines(working_text)
    return (
        bool(index_diff or working_diff)
        and (
            not index_diff
            or _diff_hunks_are_within_facts_block(index_diff, before, index_after)
        )
        and (
            not working_diff
            or _diff_hunks_are_within_facts_block(working_diff, before, working_after)
        )
    )


def source_dirty(repo_root: Path) -> bool:
    try:
        completed = run_git(
            repo_root,
            ["status", "--porcelain", "--untracked-files=all"],
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, UnicodeDecodeError):
        return True
    if completed.returncode != 0:
        return True
    for line in completed.stdout.splitlines():
        paths = status_entry_paths(line)
        if not paths:
            return True
        if all(is_generated_output_path(path) for path in paths):
            continue
        if len(paths) == 1 and has_only_generated_facts_block_changes(repo_root, paths[0]):
            continue
        return True
    return False


def head_subject(repo_root: Path) -> str:
    return git_text(repo_root, ["log", "-1", "--pretty=%s"]) or ""


def subject_for_ref(repo_root: Path, ref: str) -> str | None:
    return git_text(repo_root, ["log", "-1", "--pretty=%s", ref])


def changed_paths_for_ref(repo_root: Path, ref: str) -> list[str]:
    output = git_text(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", ref])
    return [line for line in (output or "").splitlines() if line]


def _commit_receipt_has_only_generated_facts_block_changes(
    repo_root: Path,
    ref: str,
    path: str,
) -> bool:
    normalized = path.strip().strip('"')
    if normalized not in GENERATED_RECEIPT_PATHS:
        return False
    parent = git_text(repo_root, ["rev-parse", f"{ref}^"])
    if not parent:
        return False
    before = _git_file_text(repo_root, f"{parent}:{normalized}")
    after = _git_file_text(repo_root, f"{ref}:{normalized}")
    if before is None or after is None:
        return False
    summary = _git_diff_text(
        repo_root,
        [
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--summary",
            parent,
            ref,
            "--",
            normalized,
        ],
    )
    if summary is None or summary.strip():
        return False
    diff = _git_diff_text(
        repo_root,
        [
            "diff",
            "--output-indicator-old=-",
            "--output-indicator-new=+",
            "--output-indicator-context= ",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=0",
            parent,
            ref,
            "--",
            normalized,
        ],
    )
    return bool(diff) and _diff_hunks_are_within_facts_block(
        diff,
        _git_lines(before),
        _git_lines(after),
    )


def release_sync_parent_dirty(repo_root: Path) -> bool:
    """Return the pre-commit dirty flag encoded by a generated release-sync commit."""
    if head_subject(repo_root).startswith(VERSION_SYNC_SUBJECT_PREFIX):
        return True
    if generated_docs_terminal_is_release_sync(repo_root, "HEAD"):
        return True
    merge_head_subject = subject_for_ref(repo_root, "HEAD^2")
    if merge_head_subject and merge_head_subject.startswith(VERSION_SYNC_SUBJECT_PREFIX):
        return True
    return generated_docs_terminal_is_release_sync(repo_root, "HEAD^2")


def generated_docs_terminal_ref(repo_root: Path, ref: str) -> str | None:
    current = ref
    subject = subject_for_ref(repo_root, current) or ""
    if not subject.startswith(GENERATED_DOCS_SUBJECT_PREFIX):
        return None

    while subject.startswith(GENERATED_DOCS_SUBJECT_PREFIX):
        changed_paths = changed_paths_for_ref(repo_root, current)
        if not changed_paths or any(
            not is_generated_output_path(path)
            and not _commit_receipt_has_only_generated_facts_block_changes(
                repo_root,
                current,
                path,
            )
            for path in changed_paths
        ):
            return current if current != ref else None
        parent = git_text(repo_root, ["rev-parse", f"{current}^"])
        if not parent:
            return None
        current = parent
        subject = subject_for_ref(repo_root, current) or ""

    return current


def generated_docs_terminal_is_release_sync(repo_root: Path, ref: str) -> bool:
    terminal = generated_docs_terminal_ref(repo_root, ref)
    return bool(
        terminal
        and (subject_for_ref(repo_root, terminal) or "").startswith(VERSION_SYNC_SUBJECT_PREFIX)
    )


def generated_docs_source_ref(repo_root: Path, ref: str) -> str | None:
    terminal = generated_docs_terminal_ref(repo_root, ref)
    if not terminal:
        return None
    subject = subject_for_ref(repo_root, terminal) or ""
    if subject.startswith(VERSION_SYNC_SUBJECT_PREFIX):
        parent = git_text(repo_root, ["rev-parse", f"{terminal}^"])
        return generated_docs_source_ref(repo_root, parent) or parent
    return terminal


def generated_artifact_parent_source_commit(repo_root: Path) -> str | None:
    """
    Generated artifact commits are produced from their parent source state.

    Without this, a clean CI regeneration rewrites committed provenance from the
    parent commit to the generated-docs commit itself, creating unavoidable drift.
    The parent mode is opt-in so package/install provenance still points at the
    actual current checkout.
    """
    subject = head_subject(repo_root)
    if subject.startswith(VERSION_SYNC_SUBJECT_PREFIX):
        parent = git_text(repo_root, ["rev-parse", "HEAD^"])
        return generated_docs_source_ref(repo_root, parent) or parent
    if not subject.startswith(GENERATED_DOCS_SUBJECT_PREFIX):
        merge_head_subject = subject_for_ref(repo_root, "HEAD^2")
        if merge_head_subject is None:
            return None
        if merge_head_subject.startswith(VERSION_SYNC_SUBJECT_PREFIX):
            parent = git_text(repo_root, ["rev-parse", "HEAD^2^"])
            return generated_docs_source_ref(repo_root, parent) or parent
        if generated_source := generated_docs_source_ref(repo_root, "HEAD^2"):
            return generated_source
        return None
    return generated_docs_source_ref(repo_root, "HEAD")


def source_remote_url(repo_root: Path) -> str | None:
    completed = run_git(
        repo_root,
        ["config", "--get", "remote.origin.url"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    if not value:
        return None
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:")
    if value.endswith(".git"):
        value = value[:-4]
    return value.rstrip("/") or None


def _source_root_remote_identity(repo_root: Path) -> str | None:
    remote_url = source_remote_url(repo_root)
    if remote_url is None:
        return None

    parsed = urlsplit(remote_url)
    if parsed.netloc.casefold() != "github.com":
        return remote_url

    parts = parsed.path.split("/")
    if len(parts) != 3 or parts[0] != "" or not parts[1] or not parts[2]:
        return remote_url

    parts[1] = parts[1].casefold()
    parts[2] = parts[2].casefold()
    return urlunsplit(
        parsed._replace(
            scheme=parsed.scheme.casefold(),
            netloc="github.com",
            path="/".join(parts),
        )
    )


def framework_version(repo_root: Path) -> str:
    version_file = repo_root / "VERSION"
    if not version_file.exists():
        return "unknown"
    return version_file.read_text(encoding="utf-8").strip() or "unknown"


def source_root_id(repo_root: Path) -> str:
    seed = {
        "source_commit": source_commit(repo_root),
        "remote_url": _source_root_remote_identity(repo_root),
    }
    return _sha256_bytes(json.dumps(seed, sort_keys=True).encode("utf-8"))


def source_root_id_for_commit(repo_root: Path, commit: str) -> str:
    seed = {
        "source_commit": commit,
        "remote_url": _source_root_remote_identity(repo_root),
    }
    return _sha256_bytes(json.dumps(seed, sort_keys=True).encode("utf-8"))
