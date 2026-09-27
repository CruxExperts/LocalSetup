"""Read-only verification of GitHub Actions evidence for an exact commit."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
_KNOWN_EVENTS = frozenset({"pull_request", "push", "merge_group", "workflow_dispatch"})
_DEFAULT_TIMEOUT_SECONDS = 20.0
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_RECORDS_PER_RESPONSE = 10_000


class _GitHubReadFailure(Exception):
    """An API result was unavailable or could not be safely interpreted."""


def _records(stdout: str) -> list[dict[str, Any]]:
    """Parse gh --jq's one-JSON-record-per-line output without trusting it."""
    if not isinstance(stdout, str):
        raise _GitHubReadFailure
    lines = stdout.splitlines()
    if len(lines) > _MAX_RECORDS_PER_RESPONSE:
        raise _GitHubReadFailure
    result: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            raise _GitHubReadFailure from None
        if not isinstance(value, dict):
            raise _GitHubReadFailure
        result.append(value)
    return result


def _api_records(
    endpoint: str,
    selector: str,
    *,
    deadline: float,
    paginate: bool = True,
) -> list[dict[str, Any]]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _GitHubReadFailure
    command = ["gh", "api"]
    if paginate:
        command.append("--paginate")
    command.extend(["--jq", selector, endpoint])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        raise _GitHubReadFailure from None
    if result.returncode != 0:
        # gh's stderr may include private configuration or response details.
        raise _GitHubReadFailure
    return _records(result.stdout)


def _valid_inputs(repository: str, sha: str, workflows: tuple[str, ...], required_jobs: tuple[str, ...]) -> bool:
    return bool(
        isinstance(repository, str)
        and _REPOSITORY.fullmatch(repository)
        and all(part not in {".", ".."} for part in repository.split("/"))
        and isinstance(sha, str)
        and _OBJECT_ID.fullmatch(sha)
        and workflows
        and all(isinstance(item, str) and _WORKFLOW_FILE.fullmatch(item) for item in workflows)
        and len(set(workflows)) == len(workflows)
        and all(isinstance(item, str) and item and "\x00" not in item for item in required_jobs)
        and len(set(required_jobs)) == len(required_jobs)
    )


def _run_identity_matches(
    run: dict[str, Any], *, repository: str, sha: str, workflow: str, workflow_id: int
) -> bool:
    head_repository = run.get("head_repository")
    base_repository = run.get("repository")
    if not isinstance(head_repository, dict) or not isinstance(base_repository, dict):
        return False
    path = run.get("path")
    event = run.get("event")
    run_workflow_id = run.get("workflow_id")
    run_id = run.get("id")
    return bool(
        isinstance(run_workflow_id, int)
        and not isinstance(run_workflow_id, bool)
        and run_workflow_id == workflow_id
        and run.get("head_sha") == sha
        and head_repository.get("full_name") == repository
        and base_repository.get("full_name") == repository
        and path == f".github/workflows/{workflow}"
        and isinstance(event, str)
        and event in _KNOWN_EVENTS
        and isinstance(run_id, int)
        and not isinstance(run_id, bool)
        and run_id > 0
    )


def _created_at(run: dict[str, Any]) -> datetime | None:
    value = run.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _successful_jobs(
    repository: str,
    run_id: int,
    required_jobs: tuple[str, ...],
    *,
    deadline: float,
) -> dict[str, dict[str, str]] | None:
    if not required_jobs:
        return {}
    endpoint = f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"
    jobs = _api_records(
        endpoint,
        ".jobs[] | {name: .name, status: .status, conclusion: .conclusion}",
        deadline=deadline,
    )
    expected = set(required_jobs)
    counts: Counter[str] = Counter()
    accepted: dict[str, dict[str, str]] = {}
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str) or name not in expected:
            continue
        counts[name] += 1
        status = job.get("status")
        conclusion = job.get("conclusion")
        if status == "completed" and conclusion == "success":
            accepted[name] = {"status": status, "conclusion": conclusion}
    if any(counts[name] != 1 for name in required_jobs):
        return None
    if len(accepted) != len(required_jobs):
        return None
    return accepted


def check_ci_evidence(
    repository: str,
    sha: str,
    workflows: tuple[str, ...] = ("pr-validation.yml",),
    *,
    exclude_run_id: int | None = None,
    required_jobs: tuple[str, ...] = (),
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return successful run evidence for every requested workflow, or ``ok=False``.

    All GitHub access goes through read-only ``gh api`` calls. The timeout is one
    shared wall-clock budget for workflow discovery, paginated run metadata, and
    paginated job metadata. API errors and malformed responses are intentionally
    reduced to a false probe result; their output may contain private details.
    """
    if (
        not _valid_inputs(repository, sha, workflows, required_jobs)
        or (exclude_run_id is not None and (not isinstance(exclude_run_id, int) or isinstance(exclude_run_id, bool)))
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > _MAX_TIMEOUT_SECONDS
    ):
        return {"ok": False, "runs": []}

    deadline = time.monotonic() + float(timeout_seconds)
    runs: list[dict[str, Any]] = []
    try:
        for workflow in workflows:
            workflow_endpoint = f"repos/{repository}/actions/workflows/{workflow}"
            workflow_rows = _api_records(
                workflow_endpoint,
                "{id: .id, path: .path}",
                deadline=deadline,
                paginate=False,
            )
            if len(workflow_rows) != 1:
                return {"ok": False, "runs": runs}
            workflow_record = workflow_rows[0]
            workflow_id = workflow_record.get("id")
            if (
                not isinstance(workflow_id, int)
                or isinstance(workflow_id, bool)
                or workflow_id <= 0
                or workflow_record.get("path") != f".github/workflows/{workflow}"
            ):
                return {"ok": False, "runs": runs}

            runs_endpoint = (
                f"repos/{repository}/actions/workflows/{workflow}/runs"
                f"?head_sha={sha}&per_page=100"
            )
            candidates = _api_records(
                runs_endpoint,
                ".workflow_runs[] | {id: .id, workflow_id: .workflow_id, head_sha: .head_sha, "
                "repository: {full_name: .repository.full_name}, "
                "head_repository: {full_name: .head_repository.full_name}, "
                "event: .event, path: .path, created_at: .created_at, "
                "status: .status, conclusion: .conclusion}",
                deadline=deadline,
            )
            matching = [
                run for run in candidates
                if _run_identity_matches(
                    run, repository=repository, sha=sha, workflow=workflow, workflow_id=workflow_id
                )
                and run["id"] != exclude_run_id
            ]
            dated: list[tuple[datetime, dict[str, Any]]] = []
            for run in matching:
                created = _created_at(run)
                if created is None:
                    return {"ok": False, "runs": runs}
                dated.append((created, run))
            if not dated:
                return {"ok": False, "runs": runs}
            latest = max(dated, key=lambda item: (item[0], item[1]["id"]))[1]
            if latest.get("status") != "completed" or latest.get("conclusion") != "success":
                return {"ok": False, "runs": runs}
            run_id = latest["id"]
            jobs = _successful_jobs(
                repository,
                run_id,
                required_jobs,
                deadline=deadline,
            )
            if jobs is None:
                return {"ok": False, "runs": runs}
            evidence = {
                "workflow": workflow,
                "run_id": run_id,
                "head_sha": sha,
                "event": latest["event"],
                "status": "completed",
                "conclusion": "success",
                "jobs": jobs,
            }
            runs.append(evidence)
    except _GitHubReadFailure:
        return {"ok": False, "runs": runs}
    return {"ok": True, "runs": runs}


def _positive_run_id(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("run id must be a positive integer") from None
    if result <= 0:
        raise argparse.ArgumentTypeError("run id must be a positive integer")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="exact OWNER/REPO identity")
    parser.add_argument("--sha", required=True, help="full 40-character commit SHA")
    parser.add_argument(
        "--workflow",
        action="append",
        dest="workflows",
        metavar="FILE",
        help="workflow filename to inspect (repeatable; default: pr-validation.yml)",
    )
    parser.add_argument("--exclude-run-id", type=_positive_run_id)
    parser.add_argument("--require", action="store_true", help="exit 1 when successful evidence is missing")
    parser.add_argument("--job", action="append", dest="required_jobs", default=[], metavar="NAME")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = check_ci_evidence(
        args.repository,
        args.sha,
        tuple(args.workflows or ("pr-validation.yml",)),
        exclude_run_id=args.exclude_run_id,
        required_jobs=tuple(args.required_jobs),
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] or not args.require else 1


if __name__ == "__main__":
    sys.exit(main())
