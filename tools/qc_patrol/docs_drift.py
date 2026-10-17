from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from .redaction import redact_text
from .deterministic_checks import finding


CHECK = "uv run --frozen python ls/tools/docs_alignment.py --repo-root . check --ci"
EXPECTED_ACTUAL_LIMIT = 300
ISSUE_FINDING_LIMIT = 8


def _display(value: Any) -> str:
    if value is None:
        return "unavailable"
    rendered = redact_text(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str))
    if len(rendered) > EXPECTED_ACTUAL_LIMIT:
        return f"sha256:{hashlib.sha256(rendered.encode('utf-8')).hexdigest()} ({len(rendered.encode('utf-8'))} bytes)"
    return rendered


def _alignment_issue_finding(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    rows = payload.get("findings", []) if isinstance(payload, dict) else []
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if not rows:
        check_type = "docs_alignment_unavailable"
        title = "Canonical documentation alignment check returned no finding details"
        body = (
            f"Canonical check: `{CHECK}`.\n"
            f"Observed exit status: {result.returncode}.\n"
            "Expected and observed finding values were unavailable because the check returned no parseable finding rows."
        )
        paths: list[str] = ["ls/tools/docs_alignment.py"]
        severity = "low"
    else:
        check_type = "docs_alignment"
        title = "Canonical documentation alignment check reports findings"
        lines = [
            f"Canonical check: `{CHECK}`.",
            f"Observed finding count: {len(rows)}.",
        ]
        for row in rows[:ISSUE_FINDING_LIMIT]:
            path = str(row.get("path") or "unavailable")
            lines.extend(
                [
                    f"Finding: `{row.get('id', 'unavailable')}` ({row.get('category', 'unavailable')}).",
                    f"Affected file: `{path}`.",
                    f"Check result: {redact_text(str(row.get('message', 'unavailable')))}",
                    f"Expected: {_display(row.get('expected'))}",
                    f"Observed: {_display(row.get('actual'))}",
                ]
            )
        if len(rows) > ISSUE_FINDING_LIMIT:
            lines.append(f"Additional findings omitted from this issue body: {len(rows) - ISSUE_FINDING_LIMIT}.")
        body = "\n".join(lines)
        paths = sorted({str(row.get("path")) for row in rows if row.get("path")})[:ISSUE_FINDING_LIMIT]
        severities = {str(row.get("severity", "")).lower() for row in rows}
        severity = "high" if "critical" in severities else "medium" if "major" in severities else "low"
    return finding(
        "docs",
        severity,
        title,
        body[:6000],
        paths[0] if paths else "ls/tools/docs_alignment.py",
        region="canonical-docs-alignment",
        check_type=check_type,
    ) | {"affected_paths": paths}


def docs_alignment_findings(repo: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        CHECK.split(),
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return [_alignment_issue_finding(result)]
        if isinstance(payload, dict) and payload.get("ok", True):
            return []
    return [_alignment_issue_finding(result)]
