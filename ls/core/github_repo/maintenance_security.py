"""API-only GitHub dependency advisory checks for the shared maintenance workflow.

This module deliberately uses only the Python standard library. The reusable
workflow checks out this file from its own immutable workflow commit; it never
loads or executes files from the repository being checked.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


# Isolated `python -I <trusted-script>` runs omit the source root from sys.path.
# This script is invoked only from the workflow's SHA-pinned LocalSetup checkout.
_SOURCE_ROOT = str(Path(__file__).resolve().parents[3])
if _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

from ls.core.github_repo.maintenance_security_reporting import (  # noqa: E402
    IssueAction,
    ReportingUnknown,
    paginate_issues,
    reconcile_public_advisory_issues,
)
from ls.core.github_repo.maintenance_security_inventory import (  # noqa: E402
    InventoryUnknown,
    lock_blobs_from_tree,
    parse_git_blob,
    parse_lockfile,
)


_API = "https://api.github.com"
_API_VERSION = "2022-11-28"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_SBOM_PACKAGES = 10_000
_MAX_EXTERNAL_PACKAGES = 500
_MAX_FINDINGS = 10_000
_MAX_REPORTED_VERSIONS = 20
_MAX_PAGES = 10_000
_PAGE_SIZE = 100
_REPOSITORY_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RUN_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GHSA = re.compile(r"^GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$")
_PACKAGE = re.compile(r"^[A-Za-z0-9@._/+:-]{1,200}$")
_VERSION = re.compile(r"^[A-Za-z0-9.+_~-]{1,100}$")
_SEVERITIES = {"low", "medium", "high", "critical"}
_ECOSYSTEMS = {
    "rubygems", "npm", "pip", "maven", "nuget", "composer", "go", "rust",
    "erlang", "actions", "pub", "swift", "other",
}
_PURL_ECOSYSTEM = {
    "npm": "npm", "gem": "rubygems", "maven": "maven", "nuget": "nuget",
    "pypi": "pip", "composer": "composer", "golang": "go", "cargo": "rust",
    "github": "actions", "githubactions": "actions", "swift": "swift", "pub": "pub", "hex": "erlang",
}
_UNKNOWN_REASONS = {
    "external_tools_invalid", "external_tool_record_invalid", "sbom_unavailable",
    "sbom_invalid", "sbom_no_packages", "sbom_package_invalid",
    "sbom_package_without_purl", "sbom_package_type_unsupported",
    "sbom_package_version_missing", "advisories_unavailable", "advisory_invalid",
    "advisory_severity_unknown", "repository_identity_mismatch",
    "report_invalid", "report_provenance_mismatch", "report_coverage_unknown",
    "workflow_run_unavailable", "workflow_run_provenance_mismatch",
    "issue_listing_unavailable", "issue_listing_invalid", "issue_pagination_limit",
    "issue_pagination_no_progress", "issue_publication_outcome_unknown",
    "sbom_commit_unbound", "dependency_surface_unverified",
    "lock_inventory_unknown", "lock_inventory_empty", "external_tool_versions_unverified",
}


class SecurityDataUnknown(Exception):
    """A safe, bounded reason why advisory coverage or issue state is unknown."""

    def __init__(self, reason: str):
        if reason not in _UNKNOWN_REASONS:
            reason = "report_invalid"
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Package:
    ecosystem: str
    name: str
    version: str


@dataclass(frozen=True)
class Finding:
    ghsa_id: str
    ecosystem: str
    package: str
    severity: str
    versions: tuple[str, ...]
    source: str = "github_public_advisory_database"


def _positive_id(value: Any, *, reason: str = "report_invalid") -> str:
    if isinstance(value, bool):
        raise SecurityDataUnknown(reason)
    text = str(value) if isinstance(value, (str, int)) else ""
    if not _REPOSITORY_ID.fullmatch(text):
        raise SecurityDataUnknown(reason)
    return text


def _run_id(value: Any, *, reason: str = "report_invalid") -> int:
    if isinstance(value, bool):
        raise SecurityDataUnknown(reason)
    text = str(value) if isinstance(value, (str, int)) else ""
    if not _RUN_ID.fullmatch(text):
        raise SecurityDataUnknown(reason)
    return int(text)


def _sha(value: Any, *, reason: str = "report_invalid") -> str:
    if not isinstance(value, str) or not _OBJECT_ID.fullmatch(value):
        raise SecurityDataUnknown(reason)
    return value


def validate_workflow_run(
    run: Any,
    *,
    repository_id: str,
    repository: str,
    run_id: int,
    expected_head_sha: str,
    expected_event: str,
    default_branch: str,
    require_reporting_context: bool = False,
) -> str:
    """Bind findings to the API record for this exact workflow run and source commit."""
    if not isinstance(run, Mapping):
        raise SecurityDataUnknown("workflow_run_provenance_mismatch")
    run_repository = run.get("repository")
    if not isinstance(run_repository, Mapping):
        raise SecurityDataUnknown("workflow_run_provenance_mismatch")
    run_repository_name = run_repository.get("full_name")
    if not isinstance(run_repository_name, str):
        raise SecurityDataUnknown("workflow_run_provenance_mismatch")
    try:
        actual_repo_id = _positive_id(run_repository.get("id"), reason="workflow_run_provenance_mismatch")
        actual_run_id = _run_id(run.get("id"), reason="workflow_run_provenance_mismatch")
        head_sha = _sha(run.get("head_sha"), reason="workflow_run_provenance_mismatch")
    except SecurityDataUnknown:
        raise
    if (
        actual_repo_id != repository_id
        or actual_run_id != run_id
        or run_repository_name.casefold() != repository.casefold()
        or head_sha != _sha(expected_head_sha, reason="workflow_run_provenance_mismatch")
        or run.get("event") != expected_event
    ):
        raise SecurityDataUnknown("workflow_run_provenance_mismatch")
    if require_reporting_context and (
        expected_event not in {"workflow_dispatch", "schedule"}
        or not isinstance(default_branch, str)
        or not default_branch
        or run.get("head_branch") != default_branch
    ):
        raise SecurityDataUnknown("report_provenance_mismatch")
    return head_sha


def _package_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not _PACKAGE.fullmatch(value)
        or value in {".", ".."}
        or "//" in value
    ):
        raise SecurityDataUnknown("sbom_package_invalid")
    return value


def _package_version(value: Any) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise SecurityDataUnknown("sbom_package_version_missing")
    return value


def parse_external_tool_packages(value: str | bytes | Sequence[Mapping[str, Any]]) -> tuple[Package, ...]:
    """Parse the optional caller-declared tool inventory without accepting code."""
    try:
        records: Any = json.loads(value) if isinstance(value, (str, bytes)) else value
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise SecurityDataUnknown("external_tools_invalid") from None
    if not isinstance(records, list) or len(records) > _MAX_EXTERNAL_PACKAGES:
        raise SecurityDataUnknown("external_tools_invalid")
    parsed: dict[tuple[str, str, str], Package] = {}
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {"ecosystem", "name", "version"}:
            raise SecurityDataUnknown("external_tool_record_invalid")
        ecosystem = record.get("ecosystem")
        if not isinstance(ecosystem, str) or ecosystem not in _ECOSYSTEMS:
            raise SecurityDataUnknown("external_tool_record_invalid")
        try:
            name = _package_name(record.get("name"))
            version = _package_version(record.get("version"))
        except SecurityDataUnknown:
            raise SecurityDataUnknown("external_tool_record_invalid") from None
        package = Package(ecosystem, name, version)
        parsed[(ecosystem, name, version)] = package
    return tuple(parsed[key] for key in sorted(parsed))


def _purl_package(locator: str, version_info: Any) -> Package:
    if not isinstance(locator, str) or not locator.startswith("pkg:") or len(locator) > 2048:
        raise SecurityDataUnknown("sbom_package_invalid")
    raw = locator[4:].split("?", 1)[0].split("#", 1)[0]
    kind, separator, path = raw.partition("/")
    ecosystem = _PURL_ECOSYSTEM.get(kind.lower())
    if not separator or ecosystem is None:
        raise SecurityDataUnknown("sbom_package_type_unsupported")
    if "@" in path:
        path, purl_version = path.rsplit("@", 1)
    else:
        purl_version = ""
    path = unquote(path)
    if kind.lower() == "maven":
        group, slash, artifact = path.partition("/")
        name = f"{group}:{artifact}" if slash else path
    else:
        name = path
    name = _package_name(name)
    version = unquote(purl_version) if purl_version else version_info
    if not isinstance(version, str) or version in {"", "NOASSERTION", "NONE"}:
        raise SecurityDataUnknown("sbom_package_version_missing")
    return Package(ecosystem, name, _package_version(version))


def parse_sbom_packages(
    document: Any,
    external_tool_packages: str | bytes | Sequence[Mapping[str, Any]] = "[]",
) -> tuple[tuple[Package, ...], tuple[str, ...]]:
    """Extract supported package/version rows and explicit coverage gaps from SPDX."""
    reasons: set[str] = set()
    try:
        extra = parse_external_tool_packages(external_tool_packages)
    except SecurityDataUnknown as exc:
        extra = ()
        reasons.add(exc.reason)
    if not isinstance(document, Mapping):
        return extra, tuple(sorted(reasons | {"sbom_invalid"}))
    sbom = document.get("sbom", document)
    if not isinstance(sbom, Mapping) or sbom.get("spdxVersion") != "SPDX-2.3":
        return extra, tuple(sorted(reasons | {"sbom_invalid"}))
    records = sbom.get("packages")
    relationships = sbom.get("relationships", [])
    if not isinstance(records, list) or len(records) > _MAX_SBOM_PACKAGES or not isinstance(relationships, list):
        return extra, tuple(sorted(reasons | {"sbom_invalid"}))
    root_ids = {
        row.get("relatedSpdxElement")
        for row in relationships
        if isinstance(row, Mapping)
        and row.get("spdxElementId") == "SPDXRef-DOCUMENT"
        and row.get("relationshipType") == "DESCRIBES"
    }
    packages: dict[tuple[str, str, str], Package] = {
        (item.ecosystem, item.name, item.version): item for item in extra
    }
    for record in records:
        if not isinstance(record, Mapping):
            reasons.add("sbom_package_invalid")
            continue
        if record.get("SPDXID") in root_ids:
            continue
        refs = record.get("externalRefs", [])
        if not isinstance(refs, list):
            reasons.add("sbom_package_invalid")
            continue
        purls = [
            ref.get("referenceLocator") for ref in refs
            if isinstance(ref, Mapping)
            and ref.get("referenceCategory") == "PACKAGE-MANAGER"
            and ref.get("referenceType") == "purl"
        ]
        if not purls:
            reasons.add("sbom_package_without_purl")
            continue
        parsed_any = False
        for purl in purls:
            try:
                package = _purl_package(purl, record.get("versionInfo"))
            except SecurityDataUnknown as exc:
                reasons.add(exc.reason)
                continue
            packages[(package.ecosystem, package.name, package.version)] = package
            parsed_any = True
        if not parsed_any and not purls:
            reasons.add("sbom_package_without_purl")
    if not packages:
        reasons.add("sbom_no_packages")
    return tuple(packages[key] for key in sorted(packages)), tuple(sorted(reasons))


def _severity(value: Any) -> str:
    if value == "moderate":
        return "medium"
    if isinstance(value, str) and value in _SEVERITIES:
        return value
    raise SecurityDataUnknown("advisory_severity_unknown")


def findings_from_advisories(
    advisory_matches: Sequence[tuple[Package, Mapping[str, Any]]]
) -> tuple[tuple[Finding, ...], tuple[str, ...]]:
    """Match public GHSA records to exact queried package/version rows."""
    findings: dict[tuple[str, str, str], tuple[str, set[str]]] = {}
    reasons: set[str] = set()
    for match in advisory_matches:
        if not isinstance(match, tuple) or len(match) != 2:
            reasons.add("advisory_invalid")
            continue
        package, advisory = match
        if not isinstance(package, Package) or not isinstance(advisory, Mapping):
            reasons.add("advisory_invalid")
            continue
        ghsa_id = advisory.get("ghsa_id")
        vulnerabilities = advisory.get("vulnerabilities")
        if not isinstance(ghsa_id, str) or not _GHSA.fullmatch(ghsa_id) or not isinstance(vulnerabilities, list):
            reasons.add("advisory_invalid")
            continue
        if advisory.get("withdrawn_at") is not None:
            continue
        try:
            severity = _severity(advisory.get("severity"))
        except SecurityDataUnknown as exc:
            reasons.add(exc.reason)
            continue
        matched = any(
            isinstance(vulnerability, Mapping)
            and isinstance(vulnerability.get("package"), Mapping)
            and vulnerability["package"].get("ecosystem") == package.ecosystem
            and vulnerability["package"].get("name") == package.name
            for vulnerability in vulnerabilities
        )
        if not matched:
            reasons.add("advisory_invalid")
            continue
        key = (ghsa_id, package.ecosystem, package.name)
        previous = findings.get(key)
        if previous is not None and previous[0] != severity:
            reasons.add("advisory_invalid")
        versions = previous[1] if previous else set()
        versions.add(package.version)
        if len(versions) > _MAX_REPORTED_VERSIONS:
            reasons.add("advisory_invalid")
            continue
        findings[key] = (severity, versions)
        if len(findings) > _MAX_FINDINGS:
            reasons.add("advisory_invalid")
            return (), tuple(sorted(reasons))
    return tuple(
        Finding(ghsa, ecosystem, name, severity, tuple(sorted(versions)))
        for (ghsa, ecosystem, name), (severity, versions) in sorted(findings.items())
    ), tuple(sorted(reasons))


def parse_security_report(
    payload: Any,
    *,
    expected_repository_id: str | int,
    actual_repository_id: str | int,
    run_id: str | int,
    sha: str,
    allow_locked_scope: bool = False,
) -> tuple[Finding, ...]:
    """Validate bounded finding provenance and allow only fixed public fields."""
    try:
        if isinstance(payload, (str, bytes)):
            if len(payload) > _MAX_RESPONSE_BYTES:
                raise SecurityDataUnknown("report_invalid")
            payload = json.loads(payload)
        if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "provenance", "coverage", "findings"}:
            raise SecurityDataUnknown("report_invalid")
        if payload.get("schema_version") != 1:
            raise SecurityDataUnknown("report_invalid")
        provenance = payload.get("provenance")
        if not isinstance(provenance, Mapping) or set(provenance) != {"repository_id", "run_id", "sha"}:
            raise SecurityDataUnknown("report_invalid")
        expected_id = _positive_id(expected_repository_id)
        actual_id = _positive_id(actual_repository_id)
        report_id = _positive_id(provenance.get("repository_id"))
        report_run = _run_id(provenance.get("run_id"))
        report_sha = _sha(provenance.get("sha"))
        if (
            expected_id != actual_id
            or report_id != expected_id
            or report_run != _run_id(run_id)
            or report_sha != _sha(sha)
        ):
            raise SecurityDataUnknown("report_provenance_mismatch")
        coverage = payload.get("coverage")
        if not isinstance(coverage, Mapping) or set(coverage) not in (
            {"complete", "reason_codes"}, {"complete", "locked_complete", "reason_codes"}
        ):
            raise SecurityDataUnknown("report_invalid")
        complete = coverage.get("complete")
        locked_complete = coverage.get("locked_complete", False)
        reasons = coverage.get("reason_codes")
        if not isinstance(complete, bool) or not isinstance(locked_complete, bool) or not isinstance(reasons, list):
            raise SecurityDataUnknown("report_invalid")
        if any(not isinstance(reason, str) or reason not in _UNKNOWN_REASONS for reason in reasons):
            raise SecurityDataUnknown("report_invalid")
        if complete and reasons or not complete and not reasons:
            raise SecurityDataUnknown("report_invalid")
        if not complete and not (allow_locked_scope and locked_complete):
            raise SecurityDataUnknown("report_coverage_unknown")
        raw_findings = payload.get("findings")
        if not isinstance(raw_findings, list) or len(raw_findings) > _MAX_FINDINGS:
            raise SecurityDataUnknown("report_invalid")
        result: dict[tuple[str, str, str], Finding] = {}
        for raw in raw_findings:
            if not isinstance(raw, Mapping) or set(raw) != {
                "ghsa_id", "ecosystem", "package", "severity", "versions", "source",
            }:
                raise SecurityDataUnknown("report_invalid")
            ghsa_id = raw.get("ghsa_id")
            ecosystem = raw.get("ecosystem")
            package = raw.get("package")
            severity = raw.get("severity")
            versions = raw.get("versions")
            if (
                not isinstance(ghsa_id, str)
                or not _GHSA.fullmatch(ghsa_id)
                or not isinstance(ecosystem, str)
                or ecosystem not in _ECOSYSTEMS
                or raw.get("source") != "github_public_advisory_database"
                or severity not in _SEVERITIES
                or not isinstance(versions, list)
                or not 1 <= len(versions) <= _MAX_REPORTED_VERSIONS
            ):
                raise SecurityDataUnknown("report_invalid")
            package = _package_name(package)
            try:
                valid_versions = tuple(sorted({_package_version(version) for version in versions}))
            except SecurityDataUnknown:
                raise SecurityDataUnknown("report_invalid") from None
            finding = Finding(ghsa_id, ecosystem, package, severity, valid_versions)
            result[(ghsa_id, ecosystem, package)] = finding
        return tuple(result[key] for key in sorted(result))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        raise SecurityDataUnknown("report_invalid") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, message: str, headers: Any, new_url: str) -> None:
        return None


class _SafeUnauthenticatedRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, message: str, headers: Any, new_url: str) -> Request | None:
        parts = urlsplit(new_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        return Request(new_url, headers={"Accept": "application/json"}, method="GET")


def _next_advisory_cursor(headers: Mapping[str, str], *, ecosystem: str, affects: str) -> str | None:
    """Accept only a same-query GitHub next link, never a new request target."""
    link = headers.get("Link") or headers.get("link")
    if link is None:
        return None
    if not isinstance(link, str) or len(link) > 16_384:
        raise SecurityDataUnknown("advisories_unavailable")
    next_links = []
    for part in link.split(","):
        match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="?([a-z]+)"?\s*', part)
        if not match:
            raise SecurityDataUnknown("advisories_unavailable")
        if match.group(2) == "next":
            next_links.append(match.group(1))
    if not next_links:
        return None
    if len(next_links) != 1:
        raise SecurityDataUnknown("advisories_unavailable")
    parsed = urlsplit(next_links[0])
    if (
        parsed.scheme != "https" or parsed.netloc != "api.github.com"
        or parsed.path != "/advisories" or parsed.fragment
    ):
        raise SecurityDataUnknown("advisories_unavailable")
    try:
        values = parse_qs(parsed.query, strict_parsing=True)
    except ValueError:
        raise SecurityDataUnknown("advisories_unavailable") from None
    if set(values) != {"ecosystem", "affects", "per_page", "after"} or any(len(v) != 1 for v in values.values()):
        raise SecurityDataUnknown("advisories_unavailable")
    cursor = values["after"][0]
    if (
        values["ecosystem"] != [ecosystem] or values["affects"] != [affects]
        or values["per_page"] != [str(_PAGE_SIZE)]
        or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,512}", cursor)
    ):
        raise SecurityDataUnknown("advisories_unavailable")
    return cursor


class GitHubAPI:
    """Small, no-retry GitHub API client that never echoes response contents."""

    def __init__(self, token: str):
        if not isinstance(token, str) or not token.strip() or "\n" in token or "\r" in token:
            raise SecurityDataUnknown("sbom_unavailable")
        self._token = token

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        expected: frozenset[int] = frozenset({200}),
    ) -> tuple[int, Mapping[str, str], bytes]:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "api.github.com" or parts.username or parts.password:
            raise SecurityDataUnknown("sbom_unavailable")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": "LocalSetup-GitHub-Maintenance",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=30) as response:
                status = response.status
                headers = dict(response.headers.items())
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items()) if exc.headers else {}
            try:
                body = exc.read(_MAX_RESPONSE_BYTES + 1)
            finally:
                exc.close()
        except (URLError, TimeoutError, OSError, ValueError):
            raise SecurityDataUnknown("advisories_unavailable") from None
        if status not in expected or len(body) > _MAX_RESPONSE_BYTES:
            raise SecurityDataUnknown("advisories_unavailable")
        return status, headers, body

    def _json(self, method: str, path: str, *, payload: Mapping[str, Any] | None = None) -> Any:
        _status, _headers, body = self._request(method, f"{_API}/{path.lstrip('/')}" , payload=payload)
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise SecurityDataUnknown("advisories_unavailable") from None
        return value

    def repository(self, repository: str, expected_repository_id: str) -> tuple[str, str, str]:
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise SecurityDataUnknown("repository_identity_mismatch")
        try:
            result = self._json("GET", f"repos/{quote(repository, safe='/')}")
        except SecurityDataUnknown:
            raise SecurityDataUnknown("repository_identity_mismatch") from None
        if not isinstance(result, Mapping):
            raise SecurityDataUnknown("repository_identity_mismatch")
        actual = _positive_id(result.get("id"), reason="repository_identity_mismatch")
        name = result.get("full_name")
        default_branch = result.get("default_branch")
        if (
            actual != expected_repository_id
            or not isinstance(name, str)
            or not _REPOSITORY.fullmatch(name)
            or name.casefold() != repository.casefold()
            or not isinstance(default_branch, str)
            or not default_branch
            or any(char in default_branch for char in "\r\n\0")
        ):
            raise SecurityDataUnknown("repository_identity_mismatch")
        return actual, name, default_branch

    def workflow_run(self, repository: str, run_id: int) -> Any:
        try:
            return self._json(
                "GET",
                f"repos/{quote(repository, safe='/')}/actions/runs/{_run_id(run_id)}",
            )
        except SecurityDataUnknown:
            raise SecurityDataUnknown("workflow_run_unavailable") from None

    def lock_packages(self, repository: str, sha: str) -> tuple[Package, ...]:
        """Read lock blobs from the immutable Git tree of the validated run commit."""
        prefix = f"repos/{quote(repository, safe='/')}"
        try:
            commit = self._json("GET", f"{prefix}/git/commits/{_sha(sha)}")
            if not isinstance(commit, Mapping) or commit.get("sha") != sha:
                raise InventoryUnknown("git_commit_invalid")
            tree_ref = commit.get("tree")
            tree_sha = tree_ref.get("sha") if isinstance(tree_ref, Mapping) else None
            if not isinstance(tree_sha, str) or not _OBJECT_ID.fullmatch(tree_sha):
                raise InventoryUnknown("git_commit_invalid")
            tree = self._json("GET", f"{prefix}/git/trees/{tree_sha}?recursive=1")
            if not isinstance(tree, Mapping) or tree.get("sha") != tree_sha:
                raise InventoryUnknown("git_tree_incomplete")
            blobs = lock_blobs_from_tree(tree)
            rows: set[Package] = set()
            for name, blob_sha in blobs:
                blob = self._json("GET", f"{prefix}/git/blobs/{blob_sha}")
                for ecosystem, package_name, version in parse_lockfile(name, parse_git_blob(blob, blob_sha)):
                    rows.add(Package(ecosystem, package_name, version))
            if not blobs:
                raise InventoryUnknown("lock_inventory_empty")
            return tuple(sorted(rows, key=lambda item: (item.ecosystem, item.name, item.version)))
        except (SecurityDataUnknown, InventoryUnknown):
            raise SecurityDataUnknown("lock_inventory_unknown") from None

    def sbom(self, repository: str) -> Any:
        path = f"repos/{quote(repository, safe='/')}/dependency-graph/sbom/generate-report"
        try:
            status, _headers, body = self._request("GET", f"{_API}/{path}", expected=frozenset({201}))
            if status != 201:
                raise SecurityDataUnknown("sbom_unavailable")
            response = json.loads(body)
        except (SecurityDataUnknown, json.JSONDecodeError, UnicodeDecodeError):
            raise SecurityDataUnknown("sbom_unavailable") from None
        report_url = response.get("sbom_url") if isinstance(response, Mapping) else None
        parsed = urlsplit(report_url) if isinstance(report_url, str) else None
        if (
            parsed is None or parsed.scheme != "https" or parsed.hostname != "api.github.com"
            or parsed.username or parsed.password
            or not parsed.path.startswith(f"/repos/{repository}/dependency-graph/sbom/fetch-report/")
        ):
            raise SecurityDataUnknown("sbom_unavailable")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status, headers, body = self._request("GET", report_url, expected=frozenset({202, 302}))
            if status == 202:
                time.sleep(5)
                continue
            if status == 302:
                location = headers.get("Location") or headers.get("location")
                if not isinstance(location, str):
                    raise SecurityDataUnknown("sbom_unavailable")
                location_parts = urlsplit(location)
                if (
                    location_parts.scheme != "https" or not location_parts.hostname
                    or location_parts.username or location_parts.password
                ):
                    raise SecurityDataUnknown("sbom_unavailable")
                try:
                    request = Request(location, headers={"Accept": "application/json"}, method="GET")
                    opener = build_opener(_SafeUnauthenticatedRedirect())
                    with opener.open(request, timeout=30) as response:
                        body = response.read(_MAX_RESPONSE_BYTES + 1)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                    raise SecurityDataUnknown("sbom_unavailable") from None
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise SecurityDataUnknown("sbom_unavailable")
            try:
                return json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise SecurityDataUnknown("sbom_unavailable") from None
        raise SecurityDataUnknown("sbom_unavailable")

    def advisories(self, packages: Sequence[Package]) -> tuple[tuple[Package, Mapping[str, Any]], ...]:
        unique = tuple({(item.ecosystem, item.name, item.version): item for item in packages}.values())
        matches: list[tuple[Package, Mapping[str, Any]]] = []
        by_ecosystem: dict[str, list[Package]] = {}
        for item in unique:
            by_ecosystem.setdefault(item.ecosystem, []).append(item)
        for ecosystem, ecosystem_packages in sorted(by_ecosystem.items()):
            # The API applies `affects` as an OR filter. A returned advisory can
            # mention another queried package whose selected version is patched.
            # Query one resolved identity at a time so GitHub owns range matching.
            queries = [[item] for item in sorted(ecosystem_packages, key=lambda row: (row.name, row.version))]
            for batch in queries:
                affects = ",".join(f"{item.name}@{item.version}" for item in batch)
                candidates = {(item.ecosystem, item.name): item for item in batch}
                query = {"ecosystem": ecosystem, "affects": affects, "per_page": str(_PAGE_SIZE)}
                seen_cursors: set[str] = set()
                for _ in range(_MAX_PAGES):
                    page_query = urlencode(query)
                    _status, headers, body = self._request("GET", f"{_API}/advisories?{page_query}")
                    try:
                        result = json.loads(body)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise SecurityDataUnknown("advisories_unavailable") from None
                    if not isinstance(result, list) or len(result) > _PAGE_SIZE:
                        raise SecurityDataUnknown("advisories_unavailable")
                    if any(not isinstance(item, Mapping) for item in result):
                        raise SecurityDataUnknown("advisories_unavailable")
                    for advisory in result:
                        vulnerabilities = advisory.get("vulnerabilities")
                        if not isinstance(vulnerabilities, list):
                            raise SecurityDataUnknown("advisories_unavailable")
                        matched_names: set[tuple[str, str]] = set()
                        for vulnerability in vulnerabilities:
                            package_info = vulnerability.get("package") if isinstance(vulnerability, Mapping) else None
                            if (
                                not isinstance(package_info, Mapping)
                                or not isinstance(package_info.get("ecosystem"), str)
                                or not isinstance(package_info.get("name"), str)
                            ):
                                raise SecurityDataUnknown("advisories_unavailable")
                            key = (package_info["ecosystem"], package_info["name"])
                            if key in candidates:
                                matched_names.add(key)
                        if not matched_names:
                            raise SecurityDataUnknown("advisories_unavailable")
                        for key in matched_names:
                            matches.append((candidates[key], advisory))
                    cursor = _next_advisory_cursor(headers, ecosystem=ecosystem, affects=affects)
                    if cursor is None:
                        if len(result) == _PAGE_SIZE:
                            raise SecurityDataUnknown("advisories_unavailable")
                        break
                    if cursor in seen_cursors or not result:
                        raise SecurityDataUnknown("advisories_unavailable")
                    seen_cursors.add(cursor)
                    query["after"] = cursor
                else:
                    raise SecurityDataUnknown("advisories_unavailable")
        return tuple(matches)

    def all_issues(self, repository: str) -> tuple[Mapping[str, Any], ...]:
        def fetch_page(page_number: int) -> Sequence[Mapping[str, Any]]:
            query = urlencode({"state": "all", "sort": "created", "direction": "asc", "per_page": _PAGE_SIZE, "page": page_number})
            try:
                result = self._json("GET", f"repos/{quote(repository, safe='/')}/issues?{query}")
            except SecurityDataUnknown:
                raise SecurityDataUnknown("issue_listing_unavailable") from None
            if not isinstance(result, list):
                raise SecurityDataUnknown("issue_listing_invalid")
            return result

        try:
            return paginate_issues(fetch_page)
        except ReportingUnknown as exc:
            raise SecurityDataUnknown(exc.reason) from None

    def write_issue(self, repository: str, action: IssueAction, payload: Mapping[str, Any]) -> None:
        if action.operation == "create":
            method = "POST"
            path = f"repos/{quote(repository, safe='/')}/issues"
            body = dict(payload)
        elif action.operation == "update" and action.number is not None:
            method = "PATCH"
            path = f"repos/{quote(repository, safe='/')}/issues/{action.number}"
            body = {**payload, "state": "open"}
        else:
            raise SecurityDataUnknown("issue_publication_outcome_unknown")
        try:
            self._request(method, f"{_API}/{path}", payload=body, expected=frozenset({200, 201}))
        except SecurityDataUnknown:
            # No automatic retry: the write may have succeeded before its ack was lost.
            raise SecurityDataUnknown("issue_publication_outcome_unknown") from None


def _provenance(repository_id: str, run_id: str, sha: str) -> dict[str, str | int]:
    return {
        "repository_id": _positive_id(repository_id),
        "run_id": _run_id(run_id),
        "sha": _sha(sha),
    }


def collect_report(
    api: GitHubAPI,
    *,
    repository_id: str,
    repository: str,
    run_id: str,
    sha: str,
    external_tool_packages: str,
) -> dict[str, Any]:
    reasons: set[str] = set()
    findings: tuple[Finding, ...] = ()
    locked_complete = False
    try:
        # GitHub's asynchronous dependency graph SBOM has no ref or commit
        # parameter. It cannot establish the package set for this workflow SHA.
        packages = api.lock_packages(repository, sha)
        try:
            extra = parse_external_tool_packages(external_tool_packages)
        except SecurityDataUnknown as exc:
            extra = ()
            reasons.add(exc.reason)
        reasons.add("dependency_surface_unverified")
        if extra:
            reasons.add("external_tool_versions_unverified")
        if packages:
            matches = api.advisories(packages)
            findings, advisory_reasons = findings_from_advisories(matches)
            reasons.update(advisory_reasons)
            locked_complete = not advisory_reasons
        elif not reasons:
            reasons.add("lock_inventory_empty")
    except SecurityDataUnknown as exc:
        reasons.add(exc.reason)
    return {
        "schema_version": 1,
        "provenance": _provenance(repository_id, run_id, sha),
        "coverage": {"complete": False, "locked_complete": locked_complete, "reason_codes": sorted(reasons)},
        "findings": [
            {"ghsa_id": item.ghsa_id, "ecosystem": item.ecosystem, "package": item.package,
             "severity": item.severity, "versions": list(item.versions), "source": item.source}
            for item in findings
        ],
    }


def classify_report(report: Mapping[str, Any]) -> tuple[str, int, int, tuple[str, ...]]:
    coverage = report.get("coverage")
    if not isinstance(coverage, Mapping) or not isinstance(coverage.get("complete"), bool):
        return "unknown", 0, 0, ("report_invalid",)
    reasons = coverage.get("reason_codes", [])
    if not coverage["complete"]:
        return "unknown", 0, 0, tuple(reason for reason in reasons if isinstance(reason, str))
    findings = report.get("findings")
    if not isinstance(findings, list):
        return "unknown", 0, 0, ("report_invalid",)
    high_critical = 0
    low_medium = 0
    for finding in findings:
        if not isinstance(finding, Mapping):
            return "unknown", 0, 0, ("report_invalid",)
        severity = finding.get("severity")
        if severity in {"high", "critical"}:
            high_critical += 1
        elif severity in {"low", "medium"}:
            low_medium += 1
        else:
            return "unknown", 0, 0, ("report_invalid",)
    return ("blocked" if high_critical else "ready"), high_critical, low_medium, ()


def classify_locked_report(report: Mapping[str, Any]) -> tuple[str, int, int]:
    coverage = report.get("coverage")
    if not isinstance(coverage, Mapping) or coverage.get("locked_complete") is not True:
        return "unknown", 0, 0
    scoped = {**report, "coverage": {"complete": True, "reason_codes": []}}
    status, high, low, _reasons = classify_report(scoped)
    return status, high, low


def _write_outputs(path: str, report: Mapping[str, Any]) -> None:
    status, _overall_high, _overall_low, reasons = classify_report(report)
    locked_status, high_critical, low_medium = classify_locked_report(report)
    coverage = report.get("coverage")
    complete = bool(isinstance(coverage, Mapping) and coverage.get("complete") is True)
    lines = [
        f"status={status}",
        f"locked_status={locked_status}",
        f"locked_coverage={'complete' if locked_status != 'unknown' else 'unknown'}",
        f"coverage={'complete' if complete else 'unknown'}",
        f"reportable={'true' if locked_status != 'unknown' else 'false'}",
        f"high_critical_count={high_critical}",
        f"low_medium_count={low_medium}",
        f"reason_codes={','.join(reasons)}",
    ]
    destination = Path(path)
    with destination.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def _runner_report(
    *, require_reporting_context: bool = False,
) -> tuple[dict[str, Any], GitHubAPI, str, str, int, str]:
    requested_repository_id = _positive_id(
        os.environ.get("EXPECTED_REPOSITORY_ID"), reason="repository_identity_mismatch"
    )
    workflow_repository_id = _positive_id(
        os.environ.get("GITHUB_REPOSITORY_ID"), reason="repository_identity_mismatch"
    )
    if requested_repository_id != workflow_repository_id:
        raise SecurityDataUnknown("repository_identity_mismatch")
    repository_input = os.environ.get("GITHUB_REPOSITORY", "")
    if not _REPOSITORY.fullmatch(repository_input):
        raise SecurityDataUnknown("repository_identity_mismatch")
    run_id = _run_id(os.environ.get("GITHUB_RUN_ID"))
    expected_head_sha = _sha(os.environ.get("GITHUB_RUN_HEAD_SHA"), reason="workflow_run_provenance_mismatch")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    api = GitHubAPI(os.environ.get("GITHUB_TOKEN", ""))
    actual_id, repository, default_branch = api.repository(repository_input, workflow_repository_id)
    if actual_id != requested_repository_id:
        raise SecurityDataUnknown("repository_identity_mismatch")
    run_record = api.workflow_run(repository, run_id)
    sha = validate_workflow_run(
        run_record,
        repository_id=actual_id,
        repository=repository,
        run_id=run_id,
        expected_head_sha=expected_head_sha,
        expected_event=event_name,
        default_branch=default_branch,
        require_reporting_context=require_reporting_context,
    )
    report = collect_report(
        api,
        repository_id=actual_id,
        repository=repository,
        run_id=str(run_id),
        sha=sha,
        external_tool_packages=os.environ.get("EXTERNAL_TOOL_PACKAGES_JSON", "[]"),
    )
    return report, api, actual_id, repository, run_id, sha


def _run_scan(output_path: str) -> int:
    report, _api, actual_id, _repository, run_id, sha = _runner_report()
    parse_security_report(
        report,
        expected_repository_id=os.environ.get("EXPECTED_REPOSITORY_ID", ""),
        actual_repository_id=actual_id,
        run_id=run_id,
        sha=sha,
        allow_locked_scope=True,
    ) if report["coverage"]["locked_complete"] else None
    _write_outputs(output_path, report)
    return 0


def _run_report() -> int:
    report, api, actual_id, repository, run_id, sha = _runner_report(require_reporting_context=True)
    findings = parse_security_report(
        report,
        expected_repository_id=os.environ.get("EXPECTED_REPOSITORY_ID", ""),
        actual_repository_id=actual_id,
        run_id=run_id,
        sha=sha,
        allow_locked_scope=True,
    )
    action_count = reconcile_public_advisory_issues(
        api,
        findings,
        repository_id=actual_id,
        repository=repository,
        run_id=run_id,
        sha=sha,
    )
    # Emit counts only; package names and SBOM rows never reach logs or artifacts.
    print(f"Public low/medium advisory tasks reconciled: {action_count}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="collect current-version advisory status without publishing findings")
    scan.add_argument("--github-output", required=True)
    commands.add_parser("report", help="reconcile disclosure-safe public low/medium advisory tasks")
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _run_scan(args.github_output)
        return _run_report()
    except SecurityDataUnknown as exc:
        # Fixed reason codes are safe to expose; raw API details and advisory records are not.
        print(f"GitHub maintenance security data unknown: {exc.reason}", file=sys.stderr)
        if args.command == "scan":
            fallback = {
                "coverage": {"complete": False, "reason_codes": [exc.reason]},
                "findings": [],
            }
            _write_outputs(args.github_output, fallback)
            return 0
        return 2
    except Exception:
        print("GitHub maintenance security data unknown: report_invalid", file=sys.stderr)
        if args.command == "scan":
            _write_outputs(args.github_output, {
                "coverage": {"complete": False, "reason_codes": ["report_invalid"]},
                "findings": [],
            })
            return 0
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
