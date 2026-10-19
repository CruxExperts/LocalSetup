"""Orchestrate a bounded Herdr provenance observation and issue handoff."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable

from ls.core.herdr_ops.provenance import DEFAULT_TIMEOUT, check_provenance

from .reporting import build_findings


def run_refresh(
    root: Path,
    *,
    api: Any,
    issue_writer: Callable[..., list[dict[str, Any]]],
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Observe official Herdr metadata, then write only confirmed findings."""
    report = check_provenance(root, fetch_latest=True, timeout=timeout)
    findings = build_findings(report, run_url=_workflow_run_url())
    if findings and (api is None or not api.enabled()):
        issue_error = "GitHub issue writing is not configured"
        issues: list[dict[str, Any]] = []
    else:
        try:
            issues = issue_writer(
                findings,
                ["provenance-refresh", "type/maintenance", "status/needs-triage"],
                api=api,
                title_prefix="Provenance",
                discovery_label="provenance-refresh",
                include_closed=True,
                issue_labels=["provenance-refresh", "type/maintenance", "status/needs-triage"],
                compact_handoff=True,
            ) if findings else []
            issue_error = None
        except Exception:
            # Keep the already collected evidence available to the always-uploaded
            # report. The caller fails the job and does not retry this mutation.
            issues = []
            issue_error = "GitHub issue discovery or write failed; reconcile the run before any retry"
    release_evidence = report.get("upstream", {}).get("evidence", {})
    upstream_status = report.get("upstream", {}).get("status", "unknown")
    if isinstance(release_evidence, dict):
        evidence_status = release_evidence.get("status")
        if evidence_status in {"unstable_or_unknown", "unknown"}:
            upstream_status = "unknown"
        elif evidence_status == "newer_stable":
            upstream_status = "update_available"
        elif evidence_status == "current":
            upstream_status = "current"
    return {
        "status": "issue_error" if issue_error else "complete",
        "coverage": "Herdr canonical provenance manifest only",
        "manifest": report.get("manifest"),
        "upstream": upstream_status,
        "source_watch": report.get("source_watch", {}).get("status", "unknown"),
        "evidence": {
            "local_source": report.get("source", {}),
            "local_schema": report.get("schema", {}),
            "release": report.get("upstream", {}).get("evidence", {}),
            "source_watch": report.get("source_watch", {}).get("evidence", {}),
        },
        "issue_actions": [item.get("action", "unknown") for item in issues],
        "issue_error": issue_error,
    }


def main(
    root: Path,
    *,
    api: Any,
    issue_writer: Callable[..., list[dict[str, Any]]],
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Compare canonical Herdr pins with official upstream evidence")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 60:
        parser.error("--timeout must be between 0 and 60 seconds")
    result = run_refresh(root, api=api, issue_writer=issue_writer, timeout=args.timeout)
    print(json.dumps(result, sort_keys=True))
    return 1 if result["status"] != "complete" else 0


def _workflow_run_url() -> str | None:
    base = os.environ.get("GITHUB_SERVER_URL", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if base.rstrip("/") != "https://github.com" or repository != "CruxExperts/LocalSetup" or not run_id.isdigit():
        return None
    return f"{base.rstrip('/')}/{repository}/actions/runs/{run_id}"
