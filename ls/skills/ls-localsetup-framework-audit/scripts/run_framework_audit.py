#!/usr/bin/env python3
# Purpose: Run framework audit (doc, link, skill matrix, version/facts); output to user path only.
# Created: 2026-02-20
# Last updated: 2026-02-20

"""
Single entrypoint for pre-release audit. Phases: doc checks, link checks, skill matrix
(sandbox), version/facts, maintainer refs. Output path from --output or LOCALSETUP_AUDIT_OUTPUT;
no in-repo default. Exit 0 only when zero errors. Follows INPUT_HARDENING_STANDARD.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Limits and patterns (INPUT_HARDENING)
OUTPUT_PATH_MAX = 4096
PATH_COMPONENT_MAX = 256
REPORT_SNIPPET_MAX = 2000
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAINTAINER_PATTERN = re.compile(
    r"\b(?:maintainer-only|maintainer repo|private maintainer|scripts/generate-doc-artifacts)\b",
    re.IGNORECASE,
)
VERSION_LINE = re.compile(r"^\*\*Version:\*\*\s*([\d.]+)", re.MULTILINE)
SMOKE_COMMAND_MAX = 2048
REPO_MARKERS = (".localsetup/lock.json", "ls", "VERSION", "README.md", ".git")
PRIVATE_RUNTIME_PREFIXES = (
    (".agents", "state"),
    (".omp",),
    (".localsetup", "state"),
    (".codex", "runs"),
    (".codex", "sessions"),
    (".codex", "logs"),
    (".codex", "tmp"),
    (".localsetup-maint",),
    ("graphify-out",),
    ("state",),
    ("data",),
)


@dataclass(frozen=True)
class FactsVersionRead:
    state: Literal["absent", "valid", "invalid"]
    version: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RootValidation:
    target_valid: bool
    framework_valid: bool
    errors: tuple[str, ...]


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _preparsed_framework_root(argv: list[str] | None = None) -> Path | None:
    values = argv if argv is not None else sys.argv[1:]
    for index, value in enumerate(values):
        if value == "--framework-root" and index + 1 < len(values):
            return Path(values[index + 1]).expanduser().resolve()
        if value.startswith("--framework-root="):
            return Path(value.split("=", 1)[1]).expanduser().resolve()
    return None


def _candidate_framework_roots(script_dir: Path | None = None) -> list[Path]:
    base = script_dir or _script_dir()
    candidates: list[Path] = []
    explicit = _preparsed_framework_root()
    if explicit is not None:
        candidates.append(explicit)
    # Vendored checkout: ls/skills/ls-localsetup-framework-audit/scripts
    candidates.append(base.parent.parent.parent)
    # Installed package layout: localsetup/packages/ls-localsetup-framework-audit/scripts
    candidates.append(base.parent.parent.parent / "source" / "ls")
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _select_framework_root(script_dir: Path | None = None) -> Path:
    for candidate in _candidate_framework_roots(script_dir):
        if (candidate / "lib" / "deps.py").is_file():
            return candidate
    return _candidate_framework_roots(script_dir)[0]


def _framework_root() -> Path:
    # scripts/ -> skill dir -> skills/ -> ls/
    return _select_framework_root()


def _repo_root() -> Path:
    cwd_root = _detect_repo_root(Path.cwd())
    return cwd_root if cwd_root is not None else _framework_root().parent


def _has_repo_marker(path: Path) -> bool:
    return any((path / marker).exists() for marker in REPO_MARKERS)


def _detect_repo_root(start: Path) -> Path | None:
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if candidate.name == "ls" and _has_repo_marker(candidate.parent):
            return candidate.parent
        if _has_repo_marker(candidate):
            return candidate
    return None


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _is_private_runtime_path(rel: Path) -> bool:
    return any(rel.parts[: len(prefix)] == prefix for prefix in PRIVATE_RUNTIME_PREFIXES)


sys.path.insert(0, str(_select_framework_root().parent))
sys.path.insert(0, str(_select_framework_root() / "lib"))
from deps import require_deps  # noqa: E402

require_deps(["yaml"])

import yaml  # noqa: E402


def _sanitize_output_path(s: str | None) -> Path | None:
    if s is None or (isinstance(s, str) and not s.strip()):
        return None
    s = s.strip()
    if not s:
        return None
    if len(s) > OUTPUT_PATH_MAX:
        raise ValueError("output path too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        raise ValueError("output path contains unsupported control characters")
    p = Path(s).resolve()
    for part in p.parts:
        if len(part) > PATH_COMPONENT_MAX:
            raise ValueError(f"path component too long: {part[:32]}...")
    return p


def _sanitize_path_arg(s: str | None, label: str) -> Path | None:
    if s is None or not s.strip():
        return None
    try:
        return _sanitize_output_path(s)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from exc


def _sanitize_report_text(text: str, limit: int = REPORT_SNIPPET_MAX) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = CONTROL_CHARS.sub(" ", normalized)
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")).strip()
    if len(normalized) <= limit:
        return normalized
    omitted = len(normalized) - limit
    return f"{normalized[:limit].rstrip()}... [truncated {omitted} chars]"


def _format_subprocess_failure(
    context: str,
    result: subprocess.CompletedProcess[str],
) -> str:
    parts = [f"{context} failed (exit {result.returncode})"]
    stdout = _sanitize_report_text(result.stdout or "")
    stderr = _sanitize_report_text(result.stderr or "")
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    return "\n".join(parts)


def _sanitize_smoke_command(command: str) -> list[str]:
    if not isinstance(command, str):
        raise ValueError("command must be a string")
    command = command.strip()
    if not command:
        raise ValueError("command is empty")
    if len(command) > SMOKE_COMMAND_MAX:
        raise ValueError(f"command length exceeds {SMOKE_COMMAND_MAX}")
    if CONTROL_CHARS.search(command):
        raise ValueError("command contains unsupported control characters")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"command could not be parsed: {exc}") from exc
    if not argv:
        raise ValueError("command is empty")
    if argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    return argv


def _normalize_smoke_entry(entry: object) -> tuple[str, str] | None:
    if isinstance(entry, str):
        if entry.strip().upper() == "N/A":
            return None
        return ("skill-sandbox", entry)
    if not isinstance(entry, dict):
        raise ValueError("entry must be a command string, 'N/A', or a mapping")
    command = entry.get("command")
    if isinstance(command, str) and command.strip().upper() == "N/A":
        return None
    if not isinstance(command, str) or not command.strip():
        raise ValueError("mapping entries require a non-empty command")
    cwd = entry.get("cwd", "skill-sandbox")
    if cwd not in {"skill-sandbox", "repo-root"}:
        raise ValueError("mapping cwd must be 'skill-sandbox' or 'repo-root'")
    return (str(cwd), command)


def _append_report_items(report_lines: list[str], items: list[str]) -> None:
    for item in items:
        lines = item.splitlines() or [""]
        report_lines.append(f"- {lines[0]}")
        for line in lines[1:]:
            report_lines.append(f"  {line}")


def _read_version_file(root: Path) -> str | None:
    vf = root / "VERSION"
    if not vf.is_file():
        return None
    try:
        line = (
            vf.read_text(encoding="utf-8", errors="replace")
            .strip()
            .split("\n")[0]
            .strip()
        )
        return line[:64] if line else None
    except OSError:
        return None


def _read_readme_version(root: Path) -> str | None:
    readme = root / "README.md"
    if not readme.is_file():
        return None
    try:
        text = readme.read_text(encoding="utf-8", errors="replace")
        m = VERSION_LINE.search(text)
        return m.group(1).strip() if m else None
    except OSError:
        return None


def _read_facts_version(root: Path) -> FactsVersionRead:
    facts = root / "ls" / "docs" / "_generated" / "facts.json"
    try:
        facts_stat = facts.stat()
    except FileNotFoundError:
        return FactsVersionRead("absent")
    except OSError as exc:
        return FactsVersionRead("invalid", error=f"could not stat: {type(exc).__name__}: {exc}")
    if not facts_stat.st_mode or not facts.is_file():
        return FactsVersionRead("invalid", error="path exists but is not a regular file")
    try:
        text = facts.read_text(encoding="utf-8", errors="replace")
        payload = json.loads(text)
    except OSError as exc:
        return FactsVersionRead("invalid", error=f"could not read: {type(exc).__name__}: {exc}")
    except json.JSONDecodeError as exc:
        return FactsVersionRead(
            "invalid",
            error=f"malformed JSON at line {exc.lineno}, column {exc.colno}",
        )
    if not isinstance(payload, dict):
        return FactsVersionRead("invalid", error="top level must be a JSON object")
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        return FactsVersionRead("invalid", error="top-level version must be a non-empty string")
    return FactsVersionRead("valid", version=version.strip())


def validate_audit_roots(root: Path, fw: Path) -> RootValidation:
    errors: list[str] = []
    target_valid = root.is_dir()
    if not target_valid:
        errors.append(f"Target repository root missing or not a directory: {root}")
    framework_valid = fw.is_dir()
    if not framework_valid:
        errors.append(f"Framework root missing or not a directory: {fw}")
    else:
        for directory in ("lib", "docs", "skills", "tests"):
            path = fw / directory
            if not path.is_dir():
                errors.append(f"Framework root missing required directory: {path}")
                framework_valid = False
        deps = fw / "lib" / "deps.py"
        if not deps.is_file():
            errors.append(f"Framework root missing required file: {deps}")
            framework_valid = False
    return RootValidation(target_valid, framework_valid, tuple(errors))


def phase_doc_checks(root: Path, fw: Path) -> list[str]:
    errors: list[str] = []
    target_required = [
        root / "VERSION",
        root / "README.md",
    ]
    framework_required = [
        fw / "docs" / "VERSIONING.md",
        fw / "README.md",
        fw / "docs" / "README.md",
        fw / "tests" / "skill_smoke_commands.yaml",
    ]
    for p in target_required:
        if not p.exists():
            errors.append(f"Missing target repo doc/path: {_display_path(p, root)}")
    for p in framework_required:
        if not p.exists():
            errors.append(f"Missing framework source doc/path: {_display_path(p, fw)}")
    return errors


def phase_link_checks(root: Path) -> tuple[list[str], list[str]]:
    """Return local Markdown link errors and plain-reference warnings."""
    from audit_links import phase_link_checks as _phase_link_checks

    return _phase_link_checks(root)


def phase_skill_matrix(root: Path, fw: Path) -> tuple[list[str], list[str]]:
    from audit_skill_matrix import phase_skill_matrix as _phase_skill_matrix

    return _phase_skill_matrix(
        root,
        fw,
        yaml,
        _normalize_smoke_entry,
        _sanitize_smoke_command,
        _format_subprocess_failure,
    )


def phase_version_facts(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    v = _read_version_file(root)
    rv = _read_readme_version(root)
    facts_result = _read_facts_version(root)
    if not v:
        errors.append("VERSION file missing or unreadable")
    if not rv:
        errors.append(
            "README.md version line (**Version:** X.Y.Z) missing or unreadable"
        )
    if v and rv and v != rv:
        errors.append(f"VERSION ({v}) != README version ({rv})")
    if facts_result.state == "valid" and v and v != facts_result.version:
        errors.append(f"VERSION ({v}) != facts.json version ({facts_result.version})")
    elif facts_result.state == "invalid":
        errors.append(f"facts.json invalid: {facts_result.error}")
    elif facts_result.state == "absent":
        warnings.append("facts.json missing; version/facts comparison partial")
    return (errors, warnings)


def phase_maintainer_refs(root: Path) -> list[str]:
    from audit_links import is_non_authored_source, source_ownership

    upstream, _ = source_ownership(root)  # Link phase reports verification errors.
    findings: list[str] = []
    for md in root.rglob("*.md"):
        try:
            rel = md.relative_to(root)
            if ("_generated" in rel.parts or _is_private_runtime_path(rel)
                    or is_non_authored_source(rel, upstream)):
                continue
            text = md.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if MAINTAINER_PATTERN.search(line):
                findings.append(f"{rel}:{i}: {line.strip()[:72]}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run framework audit (doc, link, skill matrix, version/facts). Output to --output or LOCALSETUP_AUDIT_OUTPUT; no file written if unset."
    )
    parser.add_argument(
        "--output",
        "-o",
        metavar="PATH",
        help="Write full report to this path (or set LOCALSETUP_AUDIT_OUTPUT)",
    )
    parser.add_argument(
        "--repo-root",
        metavar="PATH",
        help="Target repository to audit; defaults to the caller cwd when repo markers are present",
    )
    parser.add_argument(
        "--framework-root",
        metavar="PATH",
        help="Framework source root containing lib/, docs/, skills/, and tests/",
    )
    args = parser.parse_args()
    out_path = args.output or os.environ.get("LOCALSETUP_AUDIT_OUTPUT")
    try:
        out_resolved = _sanitize_output_path(out_path) if out_path else None
        root = _sanitize_path_arg(args.repo_root, "repo root") or _repo_root()
        fw = _sanitize_path_arg(args.framework_root, "framework root") or _framework_root()
    except ValueError as e:
        print(f"run_framework_audit: {e}", file=sys.stderr)
        return 2
    root_validation = validate_audit_roots(root, fw)
    all_errors: list[str] = list(root_validation.errors)
    all_warnings: list[str] = []
    maintainer_findings: list[str] = []

    # Phase 1: doc checks
    if root_validation.target_valid and root_validation.framework_valid:
        all_errors.extend(phase_doc_checks(root, fw))
    # Phase 2: link checks
    if root_validation.target_valid:
        link_errors, link_warnings = phase_link_checks(root)
        all_errors.extend(link_errors)
        all_warnings.extend(link_warnings)
    # Phase 3: skill matrix
    if root_validation.target_valid and root_validation.framework_valid:
        em, wm = phase_skill_matrix(root, fw)
        all_errors.extend(em)
        all_warnings.extend(wm)
    # Phase 4: version/facts
    if root_validation.target_valid:
        ev, wv = phase_version_facts(root)
        all_errors.extend(ev)
        all_warnings.extend(wv)
    # Phase 5: maintainer refs
    if root_validation.target_valid:
        maintainer_findings = phase_maintainer_refs(root)
        all_warnings.extend([f"Maintainer ref: {x}" for x in maintainer_findings])

    # Report
    report_lines: list[str] = []
    report_lines.append("# Framework audit report")
    report_lines.append("")
    report_lines.append(f"Repo root: {root}")
    report_lines.append(f"Framework root: {fw}")
    report_lines.append("")
    report_lines.append("## Summary")
    report_lines.append(f"- Errors: {len(all_errors)}")
    report_lines.append(f"- Warnings: {len(all_warnings)}")
    report_lines.append("")
    if all_errors:
        report_lines.append("## Errors")
        _append_report_items(report_lines, all_errors)
        report_lines.append("")
    if all_warnings:
        report_lines.append("## Warnings")
        _append_report_items(report_lines, all_warnings)
        report_lines.append("")
    report_lines.append("## requires_review / human_decision")
    report_lines.append(
        "Review errors and warnings above. Fix errors before release; accept or fix warnings."
    )
    report_lines.append(
        "Doc-only skills: agent produces step summary and logic-gap notes per SKILL.md; no script run."
    )
    report_lines.append("")

    summary = f"Errors: {len(all_errors)}, Warnings: {len(all_warnings)}"
    if out_resolved:
        try:
            out_resolved.parent.mkdir(parents=True, exist_ok=True)
            out_resolved.write_text("\n".join(report_lines), encoding="utf-8")
        except OSError as e:
            print(f"run_framework_audit: could not write report: {e}", file=sys.stderr)
            return 1
    print(summary)
    return 0 if not all_errors else 1


if __name__ == "__main__":
    sys.exit(main())
