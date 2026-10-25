"""Disclosure-safe issue reconciliation for GitHub's public advisory database."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Protocol


_REPOSITORY_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_GHSA = re.compile(r"^GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$")
_PACKAGE = re.compile(r"^[A-Za-z0-9@._/+:-]{1,200}$")
_VERSION = re.compile(r"^[A-Za-z0-9.+_~-]{1,100}$")
_MARKER = re.compile(
    r"<!-- github-maintenance:v1 repo=[1-9][0-9]{0,19} "
    r"ghsa=GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4} package=[0-9a-f]{64} -->"
)


class ReportingUnknown(Exception):
    """A safe reason why issue state or pagination could not be reconciled."""

    def __init__(self, reason: str):
        if reason not in {
            "issue_listing_unavailable", "issue_listing_invalid",
            "issue_pagination_limit", "issue_pagination_no_progress",
        }:
            reason = "issue_listing_invalid"
        self.reason = reason
        super().__init__(reason)


class PublicFinding(Protocol):
    ghsa_id: str
    ecosystem: str
    package: str
    severity: str
    versions: tuple[str, ...]


class IssueAPI(Protocol):
    def all_issues(self, repository: str) -> tuple[Mapping[str, Any], ...]: ...
    def write_issue(self, repository: str, action: "IssueAction", payload: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class IssueAction:
    operation: str
    number: int | None
    finding: PublicFinding
    marker: str
    labels: tuple[str, ...]


def _repo_id(value: str | int) -> str:
    if isinstance(value, bool):
        raise ReportingUnknown("issue_listing_invalid")
    result = str(value)
    if not _REPOSITORY_ID.fullmatch(result):
        raise ReportingUnknown("issue_listing_invalid")
    return result


def marker_for_finding(repository_id: str | int, finding: PublicFinding) -> str:
    repo_id = _repo_id(repository_id)
    if not _GHSA.fullmatch(finding.ghsa_id) or not _PACKAGE.fullmatch(finding.package):
        raise ReportingUnknown("issue_listing_invalid")
    package_digest = hashlib.sha256(f"{finding.ecosystem}\0{finding.package}".encode("utf-8")).hexdigest()
    return f"<!-- github-maintenance:v1 repo={repo_id} ghsa={finding.ghsa_id} package={package_digest} -->"


def paginate_issues(
    fetch_page: Any,
    *,
    per_page: int = 100,
    max_pages: int = 1_000,
) -> tuple[Mapping[str, Any], ...]:
    """Fetch every page, deduplicate repeated IDs, and fail unknown at bounds."""
    if not isinstance(per_page, int) or isinstance(per_page, bool) or not 1 <= per_page <= 100:
        raise ReportingUnknown("issue_listing_invalid")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise ReportingUnknown("issue_listing_invalid")
    all_issues: dict[int, Mapping[str, Any]] = {}
    for page_number in range(1, max_pages + 1):
        try:
            page = fetch_page(page_number)
        except ReportingUnknown:
            raise
        except Exception:
            raise ReportingUnknown("issue_listing_unavailable") from None
        if not isinstance(page, Sequence) or isinstance(page, (str, bytes)) or len(page) > per_page:
            raise ReportingUnknown("issue_listing_invalid")
        added = 0
        for issue in page:
            if not isinstance(issue, Mapping):
                raise ReportingUnknown("issue_listing_invalid")
            issue_id, number, body = issue.get("id"), issue.get("number"), issue.get("body")
            labels = issue.get("labels")
            if (
                not isinstance(issue_id, int) or isinstance(issue_id, bool) or issue_id <= 0
                or not isinstance(number, int) or isinstance(number, bool) or number <= 0
                or (body is not None and (not isinstance(body, str) or len(body) > 65_536))
                or (labels is not None and (not isinstance(labels, list) or len(labels) > 1000))
            ):
                raise ReportingUnknown("issue_listing_invalid")
            if isinstance(labels, list) and any(
                not isinstance(label, Mapping)
                or not isinstance(label.get("name"), str)
                or not label.get("name")
                or len(label["name"]) > 100
                or any(ord(char) < 32 for char in label["name"])
                for label in labels
            ):
                raise ReportingUnknown("issue_listing_invalid")
            previous = all_issues.get(issue_id)
            if previous is not None:
                if previous.get("number") != number:
                    raise ReportingUnknown("issue_listing_invalid")
                continue
            all_issues[issue_id] = issue
            added += 1
        if len(page) < per_page:
            return tuple(all_issues[key] for key in sorted(all_issues))
        if not added:
            raise ReportingUnknown("issue_pagination_no_progress")
    raise ReportingUnknown("issue_pagination_limit")


def plan_issue_actions(
    findings: Sequence[PublicFinding], issues: Sequence[Mapping[str, Any]], repository_id: str | int
) -> tuple[IssueAction, ...]:
    """Plan one create/update per public low/moderate repo/GHSA/package key."""
    repo_id = _repo_id(repository_id)
    markers = {
        marker_for_finding(repo_id, finding)
        for finding in findings
        if finding.severity in {"low", "medium"}
    }
    by_marker: dict[str, list[tuple[int, tuple[str, ...]]]] = {}
    for issue in issues:
        if "pull_request" in issue:
            continue
        body, number = issue.get("body"), issue.get("number")
        if not isinstance(body, str) or not isinstance(number, int) or isinstance(number, bool) or number < 1:
            continue
        raw_labels = issue.get("labels")
        if not isinstance(raw_labels, list):
            raise ReportingUnknown("issue_listing_invalid")
        labels = tuple(sorted({label["name"] for label in raw_labels if isinstance(label, Mapping)}))
        if len(labels) != len(raw_labels):
            raise ReportingUnknown("issue_listing_invalid")
        for match in _MARKER.finditer(body):
            marker = match.group(0)
            if marker in markers:
                by_marker.setdefault(marker, []).append((number, labels))
    actions: dict[str, IssueAction] = {}
    for finding in findings:
        if finding.severity not in {"low", "medium"}:
            continue
        marker = marker_for_finding(repo_id, finding)
        if marker in actions:
            continue
        existing = by_marker.get(marker, [])
        existing_number = min((item[0] for item in existing), default=None)
        existing_labels = next((labels for number, labels in existing if number == existing_number), ())
        labels = tuple(sorted(set(existing_labels) | {"automated-maintenance"}))
        actions[marker] = IssueAction(
            "update" if existing else "create", existing_number, finding, marker, labels,
        )
    return tuple(actions[key] for key in sorted(actions))


def issue_payload(
    action: IssueAction, *, repository_id: str | int, repository: str, run_id: int, sha: str
) -> dict[str, Any]:
    repo_id = _repo_id(repository_id)
    if not _REPOSITORY.fullmatch(repository) or run_id < 1 or not _OBJECT_ID.fullmatch(sha):
        raise ReportingUnknown("issue_listing_invalid")
    finding = action.finding
    versions = finding.versions
    if (
        not isinstance(versions, tuple)
        or not 1 <= len(versions) <= 20
        or any(not isinstance(version, str) or not _VERSION.fullmatch(version) for version in versions)
    ):
        raise ReportingUnknown("issue_listing_invalid")
    if finding.severity not in {"low", "medium"}:
        raise ReportingUnknown("issue_listing_invalid")
    title = f"Review public {finding.severity} advisory {finding.ghsa_id}: {finding.package}"
    advisory_url = f"https://github.com/advisories/{finding.ghsa_id}"
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    commit_url = f"https://github.com/{repository}/commit/{sha}"
    body = "\n".join([
        action.marker,
        "",
        "Review this publicly disclosed dependency advisory, confirm the affected path, and track root-cause remediation.",
        "",
        f"- Repository ID: `{repo_id}`",
        f"- Package: `{finding.ecosystem}/{finding.package}`",
        f"- Affected resolved version(s): `{', '.join(versions)}`",
        f"- Severity: `{finding.severity}`",
        f"- Public advisory: [{finding.ghsa_id}]({advisory_url})",
        f"- Detected in commit: [{sha}]({commit_url})",
        f"- Workflow run: [#{run_id}]({run_url})",
        "",
        "Acceptance criteria:",
        "- Upgrade, remove, or mitigate the affected package after reviewing the public advisory.",
        "- Validate the affected consumer and relevant runtime paths.",
        "- Close this task only after the current-version advisory check no longer matches an affected resolved version.",
        "",
        "This task contains only allowlisted public package and advisory fields. Confirm current reachability and disclosure scope before adding repository-specific details.",
    ])
    return {"title": title[:256], "body": body, "labels": list(action.labels)}


def reconcile_public_advisory_issues(
    api: IssueAPI,
    findings: Sequence[PublicFinding],
    *,
    repository_id: str | int,
    repository: str,
    run_id: int,
    sha: str,
) -> int:
    """Read all issues before any writes, then create/update without retrying writes."""
    issues = api.all_issues(repository)
    actions = plan_issue_actions(findings, issues, repository_id)
    for action in actions:
        api.write_issue(
            repository,
            action,
            issue_payload(action, repository_id=repository_id, repository=repository, run_id=run_id, sha=sha),
        )
    return len(actions)
