from __future__ import annotations

from pathlib import Path

from .config import baseline_metadata_findings
from .models import Baseline, FileMetric, Finding


WARNING_THRESHOLD = 500
BASELINE_REQUIRED_THRESHOLD = 700
REQUIRED_DOC_ANCHORS = (
    "# Scope And Authority",
    "# Environment Standard",
    "# Package Layout",
    "# Module Responsibilities",
    "# File Size Rules",
    "# Tooling And Validation",
    "# Refactoring Rules",
    "# Source Evidence",
)
POINTER_TEXT = (
    "Python architecture: new and substantially refactored Python tooling follows "
    "ls/docs/PYTHON_ARCHITECTURE_STANDARD.md; keep entrypoints thin, package "
    "responsibilities explicit, and existing debt baseline-managed."
)
RESOLVER_POINTER_TEXT = (
    "Python architecture: new and substantially refactored Python tooling follows "
    "`localsetup://doc/PYTHON_ARCHITECTURE_STANDARD.md`; keep entrypoints thin, package "
    "responsibilities explicit, and existing debt baseline-managed."
)
RESOLVER_POINTER_PATHS = (
    "ls/skills/ls-context/SKILL.md",
)
REQUIRED_POINTER_PATHS = (
    "AGENTS.md",
    "ls/docs/TOOLING_POLICY.md",
    "ls/docs/README.md",
    "ls/docs/AGENTIC_DESIGN_INDEX.md",
    "ls/skills/ls-context/SKILL.md",
    "ls/templates/codex/AGENTS.md",
    "ls/templates/opencode/AGENTS.md",
    "ls/templates/claude-code/CLAUDE.md",
    "ls/templates/kilo/AGENTS.md",
    "ls/templates/kilo/instructions.md",
    "ls/templates/openclaw/OPENCLAW_CONTEXT.md",
    "ls/templates/cursor/ls-context.mdc",
    "ls/templates/cursor/ls-context-index.md",
)
PUBLIC_WRAPPER_PATHS = (
    "ls/tools/python_architecture_check.py",
)


def evaluate_files(repo_root: Path, metrics: list[FileMetric], baseline: Baseline, include_scope: str) -> list[Finding]:
    findings: list[Finding] = []
    baseline_by_path = {entry.path: entry for entry in baseline.entries}
    scanned_paths = {metric.path for metric in metrics}

    findings.extend(baseline_metadata_findings(baseline))

    for entry in baseline.entries:
        if entry.path not in scanned_paths and not (repo_root / entry.path).is_file():
            findings.append(
                Finding(
                    code="PYA102_STALE_BASELINE_ENTRY",
                    severity="warning",
                    path=entry.path,
                    message="Baseline path is no longer present in the selected tracked scan scope.",
                    metric=entry.metric,
                    current_value=entry.current_value,
                    threshold=entry.threshold,
                )
            )

    for metric in metrics:
        entry = baseline_by_path.get(metric.path)
        if entry is not None and metric.line_count > entry.current_value:
            findings.append(
                Finding(
                    code="PYA002_OVERSIZED_WORSENED",
                    severity="warning",
                    path=metric.path,
                    message="Baselined oversized file exceeds recorded current_value.",
                    metric="lines",
                    current_value=metric.line_count,
                    threshold=entry.current_value,
                )
            )
            continue

        if entry is None and metric.line_count >= BASELINE_REQUIRED_THRESHOLD:
            code = "PYA103_SKILL_SCRIPT_DEBT" if metric.path.startswith("ls/skills/") else "PYA001_OVERSIZED_NEW"
            findings.append(
                Finding(
                    code=code,
                    severity="warning",
                    path=metric.path,
                    message="Tracked Python file exceeds the advisory line threshold; review responsibility boundaries.",
                    metric="lines",
                    current_value=metric.line_count,
                    threshold=BASELINE_REQUIRED_THRESHOLD,
                )
            )
            continue

        if entry is None and metric.line_count >= WARNING_THRESHOLD:
            findings.append(
                Finding(
                    code="PYA101_APPROACHING_LIMIT",
                    severity="warning",
                    path=metric.path,
                    message="Tracked Python file is approaching the file-size limit.",
                    metric="lines",
                    current_value=metric.line_count,
                    threshold=WARNING_THRESHOLD,
                )
            )

        if metric.path.endswith("/utils.py"):
            findings.append(
                Finding(
                    code="PYA105_GENERIC_UTILS_REVIEW",
                    severity="warning",
                    path=metric.path,
                    message="Package-local utils.py should be reviewed for mixed responsibilities.",
                )
            )

    return findings


def evaluate_contract_files(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    standard_path = repo_root / "ls/docs/PYTHON_ARCHITECTURE_STANDARD.md"
    if not standard_path.is_file():
        findings.append(
            Finding(
                code="PYA005_REQUIRED_DOC_ANCHOR_MISSING",
                severity="error",
                path="ls/docs/PYTHON_ARCHITECTURE_STANDARD.md",
                message="Canonical Python architecture standard is missing.",
            )
        )
    else:
        text = standard_path.read_text(encoding="utf-8", errors="replace")
        for anchor in REQUIRED_DOC_ANCHORS:
            if anchor not in text:
                findings.append(
                    Finding(
                        code="PYA005_REQUIRED_DOC_ANCHOR_MISSING",
                        severity="error",
                        path="ls/docs/PYTHON_ARCHITECTURE_STANDARD.md",
                        message=f"Required anchor is missing: {anchor}",
                    )
                )

    for rel_path in REQUIRED_POINTER_PATHS:
        path = repo_root / rel_path
        pointer = RESOLVER_POINTER_TEXT if rel_path in RESOLVER_POINTER_PATHS else POINTER_TEXT
        if not path.is_file() or pointer not in path.read_text(encoding="utf-8", errors="replace"):
            findings.append(
                Finding(
                    code="PYA006_REQUIRED_TEMPLATE_POINTER_MISSING",
                    severity="error",
                    path=rel_path,
                    message="Required Python architecture pointer is missing.",
                )
            )

    for rel_path in PUBLIC_WRAPPER_PATHS:
        if not (repo_root / rel_path).is_file():
            findings.append(
                Finding(
                    code="PYA007_PUBLIC_WRAPPER_MISSING",
                    severity="error",
                    path=rel_path,
                    message="Configured public wrapper path is missing.",
                )
            )

    return findings


def evaluate(repo_root: Path, metrics: list[FileMetric], baseline: Baseline, include_scope: str) -> list[Finding]:
    findings = evaluate_files(repo_root, metrics, baseline, include_scope)
    findings.extend(evaluate_contract_files(repo_root))
    return sorted(findings, key=lambda item: (item.severity != "error", item.path, item.code))
