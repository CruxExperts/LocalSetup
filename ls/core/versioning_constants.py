"""Constants for LocalSetup version planning and sync."""

from __future__ import annotations

import re

ZERO_SHA = "0" * 40
VERSION_SYNC_PREFIX = "chore: sync release version"
RELEASE_TYPE_RE = re.compile(r"^Release-Type:\s*(major|minor|patch|none)\s*$", re.MULTILINE | re.IGNORECASE)
BREAKING_CHANGE_RE = re.compile(r"^BREAKING CHANGE:", re.MULTILINE)
BREAKING_SUBJECT_RE = re.compile(r"^[a-zA-Z]+(?:\([^)]+\))?!:")
KNOWN_PATCH_TYPES = {
    "fix",
    "docs",
    "chore",
    "style",
    "refactor",
    "perf",
    "test",
    "ci",
    "build",
    "revert",
}
VERSIONED_DOC_GLOBS = ("ls/docs/**/*.md",)
VERSIONED_DOC_EXCLUDED_PARTS = {"_generated", "audits", "local-context", "releases"}
INTERNAL_PATCH_PATHS = (
    ".gitignore",
    ".githooks/",
    ".github/",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "README.md",
    "install",
    "ls/README.md",
    "ls/config/",
    "ls/docs/",
    "ls/skills/",
    "ls/skills/ls-automatic-versioning/",
    "ls/templates/",
    "ls/tests/",
    "ls/core/",
)
RELEASE_TOOLING_PATHS = (
    "ls/core/cli.py",
    "ls/core/versioning.py",
)
