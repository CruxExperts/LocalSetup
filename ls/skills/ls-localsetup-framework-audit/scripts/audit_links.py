"""Markdown link scan helpers for run_framework_audit."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

PLAIN_SEE_DOCS = re.compile(r"\b[Ss]ee\s+docs/[^\s\]\)\"']+")
PLAIN_SEE_LOCALSETUP = re.compile(r"\b[Ss]ee\s+ls/[^\s\]\)\"']+")
INLINE_LINK = re.compile(
    r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s\)]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^\)]*\)))?\)"
)
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?P<target><[^>]+>|\S+)")
ATX_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<heading>.+?)\s*#*\s*$")
SKIP_PARTS = {"_generated", "node_modules", ".git", ".venv"}
VENV_PART_PREFIXES = (".venv-",)
PRIVATE_ROOT_PREFIXES = (
    (".agents", "state"),
    (".codex", "runs"),
    (".codex", "sessions"),
    (".codex", "logs"),
    (".codex", "tmp"),
    (".localsetup",),
    (".localsetup-maint",),
    (".omp",),
    ("graphify-out",),
    ("state",),
    ("data",),
)
ARCHIVE_PART_SEQUENCES = (("references", "upstream"),)


def _is_excluded(rel: Path) -> bool:
    if SKIP_PARTS.intersection(rel.parts):
        return True
    if any(part.startswith(VENV_PART_PREFIXES) for part in rel.parts):
        return True
    if any(
        rel.parts[: len(prefix)] == prefix for prefix in PRIVATE_ROOT_PREFIXES
    ):
        return True
    return any(
        rel.parts[index : index + len(sequence)] == sequence
        for sequence in ARCHIVE_PART_SEQUENCES
        for index in range(len(rel.parts) - len(sequence) + 1)
    )


def source_ownership(root: Path) -> tuple[frozenset[str], list[str]]:
    """Exempt only unchanged upstream documents verified by the SDK owner."""
    vendor = root / "vendor" / "lscli"
    if not vendor.exists() and not vendor.is_symlink():
        return frozenset(), []
    try:
        from ls.core.sdk_payload.ownership import upstream_documents

        return frozenset(upstream_documents(root)), []
    except (ImportError, OSError, ValueError) as exc:
        return frozenset(), [f"Could not verify upstream document ownership: {exc}"]


def is_non_authored_source(rel: Path, upstream: frozenset[str]) -> bool:
    """Root build outputs and exact verified upstream files are separate owners."""
    return rel.parts[:1] in (("build",), ("dist",)) or rel.as_posix() in upstream


def _is_external_target(raw_target: str) -> bool:
    target = (
        raw_target[1:-1]
        if raw_target.startswith("<") and raw_target.endswith(">")
        else raw_target
    )
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    return bool(parsed.scheme) or target.startswith("//")


def _heading_slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[`*_~]", "", value).strip().lower()
    value = re.sub(r"[^\w\- ]", "", value)
    return re.sub(r"[\s-]+", "-", value).strip("-")


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    anchors: set[str] = set()
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        match = ATX_HEADING.match(line)
        if match:
            slug = _heading_slug(match.group("heading"))
            if slug:
                anchors.add(slug)
    return anchors


def _iter_targets(text: str) -> list[tuple[int, str]]:
    targets: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        for match in INLINE_LINK.finditer(line):
            targets.append((line_number, match.group("target")))
        match = REFERENCE_LINK.match(line)
        if match:
            targets.append((line_number, match.group("target")))
    return targets


def _resolve_target(root: Path, source: Path, raw_target: str) -> tuple[Path, str]:
    target = (
        raw_target[1:-1]
        if raw_target.startswith("<") and raw_target.endswith(">")
        else raw_target
    )
    decoded = unquote(target)
    path_text, separator, anchor = decoded.partition("#")
    anchor = anchor if separator else ""
    candidate = source if not path_text else source.parent / path_text
    resolved = candidate.resolve()
    if path_text.startswith(("docs/", "ls/")) and not resolved.exists():
        resolved = (root / path_text).resolve()
    resolved.relative_to(root)
    return resolved, anchor


def phase_link_checks(root: Path) -> tuple[list[str], list[str]]:
    """Return missing local-link errors and plain-reference warnings."""
    errors: list[str] = []
    warnings: list[str] = []
    root = root.resolve()
    upstream, ownership_errors = source_ownership(root)
    errors.extend(ownership_errors)
    for md in root.rglob("*.md"):
        try:
            rel = md.relative_to(root)
            if _is_excluded(rel) or is_non_authored_source(rel, upstream):
                continue
            text = md.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            errors.append(f"Could not read Markdown source {md}: {type(exc).__name__}: {exc}")
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if PLAIN_SEE_DOCS.search(line) or PLAIN_SEE_LOCALSETUP.search(line):
                warnings.append(f"Plain link candidate {rel}:{i}: {line.strip()[:80]}")
        for line_number, raw_target in _iter_targets(text):
            if _is_external_target(raw_target):
                continue
            if not raw_target or any(token in raw_target for token in ("<root>", "{repo_root}", "${")):
                continue
            try:
                target, anchor = _resolve_target(root, md, raw_target)
            except ValueError:
                errors.append(f"Markdown link escapes repo {rel}:{line_number}: {raw_target}")
                continue
            if not target.exists():
                errors.append(f"Missing Markdown link target {rel}:{line_number}: {raw_target}")
                continue
            if anchor:
                if not target.is_file():
                    errors.append(f"Markdown anchor target is not a file {rel}:{line_number}: {raw_target}")
                    continue
                try:
                    anchors = _anchors(target)
                except OSError as exc:
                    errors.append(
                        f"Could not read Markdown link target {rel}:{line_number}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                normalized_anchor = _heading_slug(anchor)
                if normalized_anchor not in anchors:
                    errors.append(f"Missing Markdown anchor {rel}:{line_number}: {raw_target}")
    return errors, warnings
