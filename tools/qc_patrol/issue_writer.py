"""Compatibility imports for the shipped issue planning owner."""

from ls.core.github_repo.issue_writer import (
    duplicate_is_unchanged,
    extract_handoff,
    find_duplicate,
    fingerprint_for,
    issue_body,
    issue_policy,
    labels_for,
    normalize_title,
    write_issues,
)

__all__ = [
    "duplicate_is_unchanged", "extract_handoff", "find_duplicate", "fingerprint_for",
    "issue_body", "issue_policy", "labels_for", "normalize_title", "write_issues",
]
