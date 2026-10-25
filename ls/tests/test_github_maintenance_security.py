from __future__ import annotations

import pytest
import json
import base64
from urllib.parse import parse_qs, urlsplit

from ls.core.github_repo.maintenance_security import (
    Finding,
    GitHubAPI,
    Package,
    SecurityDataUnknown,
    classify_report,
    findings_from_advisories,
    parse_external_tool_packages,
    parse_sbom_packages,
    parse_security_report,
    validate_workflow_run,
    _runner_report,
    classify_locked_report,
)
from ls.core.github_repo import maintenance_security as security_module
from ls.core.github_repo.maintenance_security_inventory import (
    InventoryUnknown, lock_blobs_from_tree, parse_lockfile,
)
from ls.core.github_repo.maintenance_security_reporting import (
    ReportingUnknown,
    IssueAction,
    issue_payload,
    marker_for_finding,
    paginate_issues,
    plan_issue_actions,
)


_REPO_ID = "12345"
_RUN_ID = "98765"
_SHA = "a" * 40
_GHSA = "GHSA-ABCD-1234-EFGH"
_REAL_GHSA = "GHSA-8q59-q68h-6hv4"


def _report(
    *,
    repository_id: str = _REPO_ID,
    run_id: int = int(_RUN_ID),
    sha: str = _SHA,
    findings: list[dict[str, str]] | None = None,
    complete: bool = True,
    reason_codes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "provenance": {"repository_id": repository_id, "run_id": run_id, "sha": sha},
        "coverage": {
            "complete": complete,
            "reason_codes": reason_codes or ([] if complete else ["sbom_unavailable"]),
        },
        "findings": findings or [],
    }


def _finding(severity: str = "medium", package: str = "safe-package") -> Finding:
    return Finding(_GHSA, "pip", package, severity, ("1.2.3",))


def test_external_tool_inventory_is_bounded_and_rejects_non_data_values() -> None:
    assert parse_external_tool_packages(
        '[{"ecosystem":"actions","name":"owner/action","version":"v1"}]'
    ) == (Package("actions", "owner/action", "v1"),)
    with pytest.raises(SecurityDataUnknown, match="external_tool_record_invalid"):
        parse_external_tool_packages('[{"ecosystem":"pip","name":"$(touch /tmp/x)","version":"1"}]')
    with pytest.raises(SecurityDataUnknown, match="external_tools_invalid"):
        parse_external_tool_packages('{"command":"python arbitrary.py"}')


def test_sbom_parser_reports_unsupported_or_missing_coverage_instead_of_clean() -> None:
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "packages": [
            {
                "name": "safe-package",
                "SPDXID": "SPDXRef-Package-1",
                "versionInfo": "1.2.3",
                "externalRefs": [{
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": "pkg:pypi/safe-package@1.2.3",
                }],
            },
            {
                "name": "unclassified-tool",
                "SPDXID": "SPDXRef-Package-2",
                "versionInfo": "2.0",
                "externalRefs": [{
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": "pkg:docker/unclassified-tool@2.0",
                }],
            },
        ],
        "relationships": [],
    }

    packages, reasons = parse_sbom_packages(sbom)

    assert Package("pip", "safe-package", "1.2.3") in packages
    assert "sbom_package_type_unsupported" in reasons


def test_current_github_sbom_shape_maps_python_and_github_actions_and_skips_repository_root() -> None:
    # Matches GitHub's documented SPDX 2.3 repository export shape.
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "packages": [
            {
                "name": "rails",
                "SPDXID": "SPDXRef-Package",
                "versionInfo": "1.0.0",
                "externalRefs": [{
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": "pkg:pypi/rails@1.0.0",
                }],
            },
            {
                "name": "owner/action",
                "SPDXID": "SPDXRef-Action",
                "versionInfo": "v2",
                "externalRefs": [{
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": "pkg:githubactions/owner/action@v2",
                }],
            },
            {
                "name": "owner/repository",
                "SPDXID": "SPDXRef-Repository",
                "versionInfo": "main",
                "externalRefs": [{
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": "pkg:github/owner/repository@main",
                }],
            },
        ],
        "relationships": [{
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relatedSpdxElement": "SPDXRef-Repository",
            "relationshipType": "DESCRIBES",
        }],
    }

    packages, reasons = parse_sbom_packages(sbom)

    assert packages == (
        Package("actions", "owner/action", "v2"),
        Package("pip", "rails", "1.0.0"),
    )
    assert reasons == ()


def test_advisory_matching_uses_public_package_ecosystem_and_maps_moderate() -> None:
    packages = (Package("pip", "safe-package", "1.2.3"), Package("npm", "other", "2.0.0"))
    advisory = {
        "ghsa_id": _GHSA,
        "severity": "moderate",
        "withdrawn_at": None,
        "vulnerabilities": [
            {"package": {"ecosystem": "pip", "name": "safe-package"}},
            {"package": {"ecosystem": "npm", "name": "different-name"}},
        ],
        "description": "This free-form source text must never be used in a public issue.",
    }

    findings, reasons = findings_from_advisories(((packages[0], advisory),))

    assert findings == (Finding(_GHSA, "pip", "safe-package", "medium", ("1.2.3",)),)
    assert reasons == ()


def test_real_lowercase_ghsa_survives_report_and_issue_marker() -> None:
    package = Package("pip", "safe-package", "1.2.3")
    advisory = {"ghsa_id": _REAL_GHSA, "severity": "moderate", "withdrawn_at": None,
                "vulnerabilities": [{"package": {"ecosystem": "pip", "name": "safe-package"}}]}
    findings, reasons = findings_from_advisories(((package, advisory),))
    assert reasons == ()
    finding = findings[0]
    report = _report(findings=[{"ghsa_id": finding.ghsa_id, "ecosystem": finding.ecosystem,
                                "package": finding.package, "severity": finding.severity,
                                "versions": list(finding.versions), "source": finding.source}])
    assert parse_security_report(report, expected_repository_id=_REPO_ID,
                                 actual_repository_id=_REPO_ID, run_id=_RUN_ID, sha=_SHA) == findings
    marker = marker_for_finding(_REPO_ID, finding)
    assert _REAL_GHSA in marker
    assert plan_issue_actions(findings, ({"id": 1, "number": 2, "body": marker, "labels": []},), _REPO_ID)[0].operation == "update"


def test_unknown_advisory_severity_is_not_interpreted_as_clean() -> None:
    advisory = {
        "ghsa_id": _GHSA,
        "severity": "unknown",
        "withdrawn_at": None,
        "vulnerabilities": [{"package": {"ecosystem": "pip", "name": "safe-package"}}],
    }
    findings, reasons = findings_from_advisories(((Package("pip", "safe-package", "1.2.3"), advisory),))
    assert findings == ()
    assert reasons == ("advisory_severity_unknown",)


def test_report_requires_exact_bounded_provenance_and_public_fields() -> None:
    finding = {
        "ghsa_id": _GHSA,
        "ecosystem": "pip",
        "package": "safe-package",
        "severity": "medium",
        "versions": ["1.2.3"],
        "source": "github_public_advisory_database",
    }
    valid = _report(findings=[finding])
    assert parse_security_report(
        valid,
        expected_repository_id=_REPO_ID,
        actual_repository_id=int(_REPO_ID),
        run_id=_RUN_ID,
        sha=_SHA,
    ) == (_finding(),)

    with pytest.raises(SecurityDataUnknown, match="report_provenance_mismatch"):
        parse_security_report(
            _report(repository_id="54321", findings=[finding]),
            expected_repository_id=_REPO_ID,
            actual_repository_id=_REPO_ID,
            run_id=_RUN_ID,
            sha=_SHA,
        )
    with pytest.raises(SecurityDataUnknown, match="report_provenance_mismatch"):
        parse_security_report(
            _report(findings=[finding]),
            expected_repository_id=_REPO_ID,
            actual_repository_id="54321",
            run_id=_RUN_ID,
            sha=_SHA,
        )
    with pytest.raises(SecurityDataUnknown, match="report_provenance_mismatch"):
        parse_security_report(
            _report(findings=[finding]),
            expected_repository_id=_REPO_ID,
            actual_repository_id=_REPO_ID,
            run_id="1",
            sha=_SHA,
        )
    with pytest.raises(SecurityDataUnknown, match="report_provenance_mismatch"):
        parse_security_report(
            _report(findings=[finding]),
            expected_repository_id=_REPO_ID,
            actual_repository_id=_REPO_ID,
            run_id=_RUN_ID,
            sha="b" * 40,
        )


def test_malformed_report_and_unknown_coverage_fail_closed() -> None:
    with pytest.raises(SecurityDataUnknown, match="report_invalid"):
        parse_security_report(
            '{"schema_version":1,"findings":[],"private_alert":"do not echo"}',
            expected_repository_id=_REPO_ID,
            actual_repository_id=_REPO_ID,
            run_id=_RUN_ID,
            sha=_SHA,
        )
    with pytest.raises(SecurityDataUnknown, match="report_coverage_unknown"):
        parse_security_report(
            _report(complete=False, findings=[]),
            expected_repository_id=_REPO_ID,
            actual_repository_id=_REPO_ID,
            run_id=_RUN_ID,
            sha=_SHA,
        )


def test_coverage_unknown_and_high_critical_advisories_block() -> None:
    assert classify_report(_report(complete=False)) == ("unknown", 0, 0, ("sbom_unavailable",))
    high = _report(findings=[{
        "ghsa_id": _GHSA,
        "ecosystem": "pip",
        "package": "safe-package",
        "severity": "critical",
        "versions": ["1.2.3"],
        "source": "github_public_advisory_database",
    }])
    assert classify_report(high) == ("blocked", 1, 0, ())


def test_issue_pagination_reads_every_page_and_deduplicates_records() -> None:
    pages = {
        1: [
            {"id": 1, "number": 10, "body": "one", "labels": []},
            {"id": 2, "number": 20, "body": "two", "labels": []},
        ],
        2: [
            {"id": 2, "number": 20, "body": "two", "labels": []},
            {"id": 3, "number": 30, "body": "three", "labels": []},
        ],
        3: [],
    }
    seen: list[int] = []

    def fetch(page: int) -> list[dict[str, object]]:
        seen.append(page)
        return pages[page]

    issues = paginate_issues(fetch, per_page=2)
    assert seen == [1, 2, 3]
    assert [issue["id"] for issue in issues] == [1, 2, 3]


def test_issue_pagination_unknown_on_failure_limit_and_repeated_full_page() -> None:
    with pytest.raises(ReportingUnknown, match="issue_listing_unavailable"):
        paginate_issues(lambda _page: (_ for _ in ()).throw(RuntimeError("private response")))
    with pytest.raises(ReportingUnknown, match="issue_pagination_limit"):
        paginate_issues(lambda page: [{"id": page, "number": page, "body": "x", "labels": []}], per_page=1, max_pages=1)
    repeated = [{"id": 1, "number": 1, "body": "x", "labels": []}]
    with pytest.raises(ReportingUnknown, match="issue_pagination_no_progress"):
        paginate_issues(lambda _page: repeated, per_page=1, max_pages=2)


def test_issue_actions_update_existing_marker_and_only_report_low_medium() -> None:
    low = _finding("low")
    medium = _finding("medium", "other-package")
    high = _finding("high", "critical-package")
    existing_marker = marker_for_finding(_REPO_ID, low)
    issues = (
        {"id": 2, "number": 9, "body": existing_marker, "labels": [{"name": "triage"}]},
        {"id": 1, "number": 3, "body": existing_marker, "labels": [{"name": "priority"}]},
        {"id": 3, "number": 1, "body": existing_marker, "labels": [], "pull_request": {}},
    )

    actions = plan_issue_actions((low, medium, high), issues, _REPO_ID)

    assert {
        action.finding.package: (action.operation, action.number)
        for action in actions
    } == {
        "other-package": ("create", None),
        "safe-package": ("update", 3),
    }
    by_package = {action.finding.package: action for action in actions}
    assert by_package["other-package"].labels == ("automated-maintenance",)
    assert by_package["safe-package"].labels == ("automated-maintenance", "priority")


def test_issue_payload_uses_allowlisted_versions_and_fixed_remediation_criteria() -> None:
    finding = _finding()
    action = IssueAction("create", None, finding, marker_for_finding(_REPO_ID, finding), ("automated-maintenance",))

    payload = issue_payload(
        action,
        repository_id=_REPO_ID,
        repository="CruxExperts/LocalSetup",
        run_id=int(_RUN_ID),
        sha=_SHA,
    )

    assert payload["labels"] == ["automated-maintenance"]
    assert "Affected resolved version(s): `1.2.3`" in payload["body"]
    assert "Upgrade, remove, or mitigate" in payload["body"]
    assert "Close this task only after the current-version advisory check no longer matches" in payload["body"]
    assert "description" not in payload["body"]


def test_workflow_run_provenance_binds_repo_run_event_and_head_sha() -> None:
    run = {
        "id": int(_RUN_ID),
        "head_sha": _SHA,
        "event": "schedule",
        "head_branch": "main",
        "repository": {"id": int(_REPO_ID), "full_name": "CruxExperts/LocalSetup"},
    }
    assert validate_workflow_run(
        run,
        repository_id=_REPO_ID,
        repository="CruxExperts/LocalSetup",
        run_id=int(_RUN_ID),
        expected_head_sha=_SHA,
        expected_event="schedule",
        default_branch="main",
        require_reporting_context=True,
    ) == _SHA
    for changed in (
        {**run, "id": 1},
        {**run, "head_sha": "b" * 40},
        {**run, "event": "pull_request"},
        {**run, "repository": {"id": 999, "full_name": "CruxExperts/LocalSetup"}},
    ):
        with pytest.raises(SecurityDataUnknown, match="workflow_run_provenance_mismatch"):
            validate_workflow_run(
                changed,
                repository_id=_REPO_ID,
                repository="CruxExperts/LocalSetup",
                run_id=int(_RUN_ID),
                expected_head_sha=_SHA,
                expected_event="schedule",
                default_branch="main",
            )
    with pytest.raises(SecurityDataUnknown, match="report_provenance_mismatch"):
        validate_workflow_run(
            {**run, "head_branch": "feature"},
            repository_id=_REPO_ID,
            repository="CruxExperts/LocalSetup",
            run_id=int(_RUN_ID),
            expected_head_sha=_SHA,
            expected_event="schedule",
            default_branch="main",
            require_reporting_context=True,
        )


def test_caller_cannot_redirect_expected_repository_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPECTED_REPOSITORY_ID", "54321")
    monkeypatch.setenv("GITHUB_REPOSITORY_ID", _REPO_ID)
    with pytest.raises(SecurityDataUnknown, match="repository_identity_mismatch"):
        _runner_report()


class _HTTPResponse:
    def __init__(self, status: int, body: object, headers: dict[str, str] | None = None):
        self.status = status
        self.headers = headers or {}
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self) -> "_HTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self, _size: int) -> bytes:
        return self._body


def test_sbom_http_generation_poll_and_download_never_forward_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = "https://api.github.com/repos/CruxExperts/BestBackup/dependency-graph/sbom/fetch-report/uuid"
    download = "https://objects.example.test/report.json"
    calls: list[tuple[str, str, str | None]] = []
    responses = iter([
        _HTTPResponse(201, {"sbom_url": fetch}),
        _HTTPResponse(202, b""),
        _HTTPResponse(302, b"", {"Location": download}),
        _HTTPResponse(200, {"sbom": {"spdxVersion": "SPDX-2.3", "packages": []}}),
    ])

    class Opener:
        def open(self, request: object, timeout: int) -> _HTTPResponse:
            assert timeout == 30
            calls.append((request.get_method(), request.full_url, request.get_header("Authorization")))
            return next(responses)

    monkeypatch.setattr(security_module, "build_opener", lambda *_handlers: Opener())
    monkeypatch.setattr(security_module.time, "sleep", lambda _seconds: None)
    assert GitHubAPI("secret").sbom("CruxExperts/BestBackup")["sbom"]["spdxVersion"] == "SPDX-2.3"
    assert calls == [
        ("GET", "https://api.github.com/repos/CruxExperts/BestBackup/dependency-graph/sbom/generate-report", "Bearer secret"),
        ("GET", fetch, "Bearer secret"), ("GET", fetch, "Bearer secret"),
        ("GET", download, None),
    ]


def test_sbom_http_failure_is_unknown_and_redirect_handler_removes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class Opener:
        def open(self, _request: object, timeout: int) -> _HTTPResponse:
            return _HTTPResponse(403, {"message": "private error"})

    monkeypatch.setattr(security_module, "build_opener", lambda *_handlers: Opener())
    with pytest.raises(SecurityDataUnknown, match="sbom_unavailable"):
        GitHubAPI("secret").sbom("CruxExperts/BestBackup")
    original = security_module.Request("https://api.github.com/report", headers={"Authorization": "Bearer secret"})
    redirected = security_module._SafeUnauthenticatedRedirect().redirect_request(
        original, None, 302, "Found", {}, "https://objects.example.test/report.json"
    )
    assert redirected is not None and redirected.get_header("Authorization") is None
    assert security_module._SafeUnauthenticatedRedirect().redirect_request(
        original, None, 302, "Found", {}, "http://objects.example.test/report.json"
    ) is None


def test_global_advisories_use_verified_cursor_after_full_page(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    advisory = {"ghsa_id": _REAL_GHSA, "severity": "low", "withdrawn_at": None,
                "vulnerabilities": [{"package": {"ecosystem": "pip", "name": "safe-package"}}]}
    def response(_self: GitHubAPI, method: str, url: str, **_kwargs: object) -> tuple[int, dict[str, str], bytes]:
        assert method == "GET"
        urls.append(url)
        query = parse_qs(urlsplit(url).query)
        if "after" not in query:
            next_url = url + "&after=cursor-one"
            return 200, {"Link": f'<{next_url}>; rel="next"'}, json.dumps([advisory] * 100).encode()
        assert query["after"] == ["cursor-one"]
        return 200, {}, json.dumps([advisory]).encode()
    monkeypatch.setattr(GitHubAPI, "_request", response)
    matches = GitHubAPI("token").advisories((Package("pip", "safe-package", "1.2.3"),))
    assert len(matches) == 101 and len(urls) == 2
    assert "page" not in parse_qs(urlsplit(urls[0]).query) and "after=cursor-one" in urls[1]


def test_global_advisory_repeated_or_foreign_cursor_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    advisory = {"vulnerabilities": [{"package": {"ecosystem": "pip", "name": "safe-package"}}]}
    def repeat(_self: GitHubAPI, _method: str, url: str, **_kwargs: object) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Link": f'<{url.split("&after=")[0]}&after=repeat>; rel="next"'}, json.dumps([advisory] * 100).encode()
    monkeypatch.setattr(GitHubAPI, "_request", repeat)
    with pytest.raises(SecurityDataUnknown, match="advisories_unavailable"):
        GitHubAPI("token").advisories((Package("pip", "safe-package", "1.2.3"),))


def test_exact_lock_parsers_cover_native_uv_cargo_bun_and_reject_other_sources() -> None:
    uv = b'version = 1\n[[package]]\nname="safe-package"\nversion="1.2.3"\nsource={registry="https://pypi.org/simple"}\n'
    cargo = b'version = 4\n[[package]]\nname="safe-crate"\nversion="2.0.0"\nsource="registry+https://github.com/rust-lang/crates.io-index"\n'
    bun = b'{"lockfileVersion":1,"packages":{"safe-package":["safe-package@1.2.3","",{}],},}'
    assert parse_lockfile("uv.lock", uv) == (("pip", "safe-package", "1.2.3"),)
    assert parse_lockfile("Cargo.lock", cargo) == (("rust", "safe-crate", "2.0.0"),)
    assert parse_lockfile("bun.lock", bun) == (("npm", "safe-package", "1.2.3"),)
    with pytest.raises(InventoryUnknown):
        parse_lockfile("uv.lock", uv.replace(b"https://pypi.org/simple", b"https://evil.test/simple"))
    with pytest.raises(InventoryUnknown):
        lock_blobs_from_tree({"truncated": True, "tree": []})


def test_lock_inventory_binds_commit_tree_and_blob_before_scanning(monkeypatch: pytest.MonkeyPatch) -> None:
    blob_sha = "b" * 40
    tree_sha = "c" * 40
    lock = b'version = 1\n[[package]]\nname="safe-package"\nversion="1.2.3"\nsource={registry="https://pypi.org/simple"}\n'
    calls: list[str] = []
    def fetch(_self: GitHubAPI, _method: str, path: str, **_kwargs: object) -> object:
        calls.append(path)
        if path.endswith(f"/git/commits/{_SHA}"):
            return {"sha": _SHA, "tree": {"sha": tree_sha}}
        if path.endswith(f"/git/trees/{tree_sha}?recursive=1"):
            return {"sha": tree_sha, "truncated": False,
                    "tree": [{"path": "uv.lock", "type": "blob", "sha": blob_sha}]}
        if path.endswith(f"/git/blobs/{blob_sha}"):
            return {"sha": blob_sha, "encoding": "base64", "size": len(lock),
                    "content": base64.b64encode(lock).decode()}
        raise AssertionError(path)
    monkeypatch.setattr(GitHubAPI, "_json", fetch)
    api = GitHubAPI("token")
    assert api.lock_packages("CruxExperts/BestBackup", _SHA) == (Package("pip", "safe-package", "1.2.3"),)
    assert len(calls) == 3
    def wrong_commit(_self: GitHubAPI, _method: str, _path: str, **_kwargs: object) -> object:
        return {"sha": "f" * 40, "tree": {"sha": tree_sha}}
    monkeypatch.setattr(GitHubAPI, "_json", wrong_commit)
    with pytest.raises(SecurityDataUnknown, match="lock_inventory_unknown"):
        api.lock_packages("CruxExperts/BestBackup", _SHA)


def test_partial_lock_scope_never_claims_overall_complete() -> None:
    report = _report(complete=False, reason_codes=["dependency_surface_unverified"])
    report["coverage"]["locked_complete"] = True
    assert classify_locked_report(report) == ("ready", 0, 0)
    assert classify_report(report)[0] == "unknown"
    with pytest.raises(SecurityDataUnknown, match="report_coverage_unknown"):
        parse_security_report(report, expected_repository_id=_REPO_ID,
                              actual_repository_id=_REPO_ID, run_id=_RUN_ID, sha=_SHA)
    assert parse_security_report(report, expected_repository_id=_REPO_ID,
                                 actual_repository_id=_REPO_ID, run_id=_RUN_ID, sha=_SHA,
                                 allow_locked_scope=True) == ()


def test_advisory_query_does_not_attribute_cross_package_version_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    api = GitHubAPI("synthetic-test-token")
    seen = []
    advisory = {
        "ghsa_id": "GHSA-8q59-q68h-6hv4", "severity": "high",
        "vulnerabilities": [
            {"package": {"ecosystem": "pip", "name": "affected"}},
            {"package": {"ecosystem": "pip", "name": "patched"}},
        ],
    }
    def request(_method, url, **_kwargs):
        affects = parse_qs(urlsplit(url).query)["affects"][0]
        seen.append(affects)
        # The advisory affects affected@1 but only earlier versions of patched.
        result = [advisory] if affects == "affected@1.0" else []
        return 200, {}, json.dumps(result).encode()
    monkeypatch.setattr(api, "_request", request)
    packages = [Package("pip", "affected", "1.0"), Package("pip", "patched", "3.0")]
    matches = api.advisories(packages)
    assert seen == ["affected@1.0", "patched@3.0"]
    assert [(package.name, package.version) for package, _ in matches] == [("affected", "1.0")]
