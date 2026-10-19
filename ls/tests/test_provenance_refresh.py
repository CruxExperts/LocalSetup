from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from ls.core.provenance_refresh.reporting import build_findings
from ls.core.manifests import load_pack_config
from tools.qc_patrol.github_api import GitHubAPI
from tools.qc_patrol.issue_writer import extract_handoff, issue_body, write_issues


ROOT = Path(__file__).resolve().parents[2]
PINNED = "a" * 40
OBSERVED = "b" * 40
PINNED_HASH = "1" * 64
OBSERVED_HASH = "2" * 64


def _release_evidence() -> dict[str, Any]:
    return {
        "status": "newer_stable",
        "pinned": {
            "tag": "v0.9.5",
            "version": "0.9.5",
            "commit": PINNED,
            "immutable_url": f"https://github.com/herdrdev/herdr/commit/{PINNED}",
            "sha256": {},
        },
        "observed": {
            "tag": "v0.10.0",
            "version": "0.10.0",
            "commit": OBSERVED,
            "immutable_url": f"https://github.com/herdrdev/herdr/commit/{OBSERVED}",
            "sha256": {},
            "checksum_status": "unknown",
        },
    }


def _source_evidence(revision: str = OBSERVED) -> dict[str, Any]:
    return {
        "status": "drift",
        "pinned_revision": PINNED,
        "pinned_immutable_url": f"https://github.com/herdrdev/herdr/commit/{PINNED}",
        "revision": revision,
        "immutable_url": f"https://github.com/herdrdev/herdr/commit/{revision}",
        "files": [
            {
                "source_path": "skills/herdr/SKILL.md",
                "pinned_sha256": PINNED_HASH,
                "observed_sha256": OBSERVED_HASH,
                "immutable_url": f"https://github.com/herdrdev/herdr/blob/{revision}/skills/herdr/SKILL.md",
            }
        ],
        "unknown": ["source observation is unavailable for src/api/schema.rs"],
    }


def _source_fact(path: str, observed_hash: str) -> dict[str, str]:
    return {"path": path, "pinned_sha256": PINNED_HASH, "observed_sha256": observed_hash}


def _provenance_identity(
    *,
    release: tuple[str, str] | None = ("v0.9.5", "v0.10.0"),
    source_files: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "pinned_release_tag": release[0] if release else None,
        "observed_release_tag": release[1] if release else None,
        "source_files": source_files if source_files is not None else [_source_fact("skills/herdr/SKILL.md", OBSERVED_HASH)],
    }


def _finding(*, evidence_identity: dict[str, Any] | None = None, run_ref: str = "run-1") -> dict[str, Any]:
    return {
        "category": "provenance",
        "check_type": "herdr_upstream_provenance",
        "severity": "medium",
        "title": "Herdr upstream evidence needs review",
        "affected_paths": ["ls/skills/ls-herdr/references/upstream/provenance.json"],
        "region": "herdr-upstream",
        "evidence_identity": evidence_identity if evidence_identity is not None else _provenance_identity(),
        "body": f"Evidence details for {run_ref}. No host upgrade authority.",
    }


class FakeAPI:
    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.comments: dict[int, list[dict[str, str]]] = {}
        self.created: list[dict[str, Any]] = []
        self.posted_comments: list[tuple[int, str]] = []

    def enabled(self) -> bool:
        return True

    def list_issues(self, _label: str, *, state: str = "open") -> list[dict[str, Any]]:
        assert state == "all"
        return list(self.issues)

    def list_issue_comments(self, number: int) -> list[dict[str, str]]:
        return list(self.comments.get(number, []))

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        issue = {"number": 100 + len(self.created), "title": title, "body": body, "labels": labels, "state": "open"}
        self.created.append(issue)
        self.issues.append(issue)
        return issue

    def comment_issue(self, number: int, body: str) -> dict[str, Any]:
        self.posted_comments.append((number, body))
        self.comments.setdefault(number, []).append({"body": body})
        return {"body": body}


def _write(finding: dict[str, Any], api: Any) -> list[dict[str, Any]]:
    return write_issues(
        [finding],
        ["provenance-refresh", "type/maintenance", "status/needs-triage"],
        api=api,
        title_prefix="Provenance",
        discovery_label="provenance-refresh",
        include_closed=True,
        issue_labels=["provenance-refresh", "type/maintenance", "status/needs-triage"],
        compact_handoff=True,
    )


def test_build_findings_reports_only_verified_new_stable_release() -> None:
    report = {
        "upstream": {"status": "update_available", "evidence": _release_evidence()},
        "source_watch": {"status": "current", "evidence": {"status": "current", "files": [], "unknown": []}},
    }
    finding = build_findings(report, run_url="https://github.com/CruxExperts/LocalSetup/actions/runs/123")[0]
    assert finding["evidence_identity"]["observed_release_tag"] == "v0.10.0"
    assert "Pinned revision" in finding["body"]
    assert f"https://github.com/herdrdev/herdr/commit/{PINNED}" in finding["body"]
    assert "checksums were unavailable" in finding["body"]
    assert "notes" not in finding["body"].lower()
    assert "no authority to upgrade a Herdr host" in finding["body"]

    report["upstream"]["evidence"] = {"status": "unstable_or_unknown"}
    assert build_findings(report) == []


def test_current_source_observation_does_not_claim_unknown_source_freshness() -> None:
    report = {
        "upstream": {"status": "update_available", "evidence": _release_evidence()},
        "source_watch": {
            "status": "current",
            "evidence": {
                "status": "current",
                "revision": OBSERVED,
                "immutable_url": f"https://github.com/herdrdev/herdr/commit/{OBSERVED}",
                "files": [
                    {
                        "source_path": "skills/herdr/SKILL.md",
                        "pinned_sha256": PINNED_HASH,
                        "observed_sha256": PINNED_HASH,
                        "immutable_url": f"https://github.com/herdrdev/herdr/blob/{OBSERVED}/skills/herdr/SKILL.md",
                    }
                ],
                "unknown": [],
            },
        },
    }

    finding = build_findings(report)[0]

    assert "Unconfirmed observations" not in finding["body"]
    assert "Watched source-file freshness could not be confirmed" not in finding["body"]


def test_source_only_handoff_has_both_revisions_local_owner_and_validation() -> None:
    report = {
        "upstream": {"status": "unknown", "evidence": {"status": "unknown", "reason": "metadata unavailable"}},
        "source_watch": {"status": "drift", "evidence": _source_evidence()},
    }
    finding = build_findings(report, run_url="https://github.com/CruxExperts/LocalSetup/actions/runs/456")[0]
    assert "Pinned source revision" in finding["body"]
    assert "Observed source revision" in finding["body"]
    assert "Compare these immutable revisions" in finding["body"]
    assert "ls/skills/ls-herdr/references/upstream/provenance.json" in finding["body"]
    assert "ls/skills/ls-herdr-provenance-maintenance/SKILL.md" in finding["body"]
    assert "ls/tests/test_provenance_refresh.py" in finding["body"]
    assert "test-workers" in finding["body"]
    assert 'pytest -n "$workers"' in finding["body"]
    assert "pytest -n8" not in finding["body"]
    assert "other watched source observation(s) were unknown" in finding["body"]
    assert "Release freshness could not be confirmed" in finding["body"]
    assert "Observation run" in finding["body"]


def test_release_only_handoff_discloses_unknown_source_check_without_raw_reason() -> None:
    report = {
        "upstream": {"status": "update_available", "evidence": _release_evidence()},
        "source_watch": {
            "status": "unknown",
            "evidence": {
                "status": "unknown",
                "reason": "official metadata request failed with secret host text",
            },
        },
    }

    finding = build_findings(report)[0]

    assert "Watched source files could not be read from the official repository or timed out" in finding["body"]
    assert "secret host text" not in finding["body"]


def test_changed_branch_sha_with_identical_bytes_does_not_change_issue_identity() -> None:
    first = build_findings({"source_watch": {"evidence": _source_evidence(OBSERVED)}})[0]
    later_sha = "c" * 40
    later = build_findings({"source_watch": {"evidence": _source_evidence(later_sha)}})[0]
    assert first["evidence_identity"] == later["evidence_identity"]
    assert first["body"] != later["body"]


def test_compact_handoff_keeps_only_dedup_identity_out_of_sight() -> None:
    finding = _finding()
    body = issue_body(finding, compact=True)
    assert body.count(finding["body"]) == 1
    assert "<!--\n```qc.issue-handoff.v1" in body
    handoff = extract_handoff(body)
    assert handoff == {
        "schema_version": "qc.issue-handoff.v1",
        "fingerprint": handoff["fingerprint"],
        "finding": {"evidence_identity": finding["evidence_identity"]},
    }
    assert body.count(finding["body"]) == 1


def test_malformed_pins_or_paths_never_render_upstream_urls() -> None:
    evidence = _source_evidence()
    evidence["files"][0]["source_path"] = "../../evil](https://evil.example"
    evidence["files"][0]["immutable_url"] = "https://evil.example/instructions"
    finding = build_findings({"source_watch": {"evidence": evidence}})
    assert finding == []

    release = _release_evidence()
    release["observed"]["commit"] = "not-a-sha"
    release["observed"]["immutable_url"] = "https://evil.example/instructions"
    assert build_findings({"upstream": {"evidence": release}}) == []


def test_open_issue_dedup_ignores_run_url_but_comments_on_material_evidence_change() -> None:
    first = _finding(run_ref="run-1")
    api = FakeAPI([{ "number": 7, "title": "old", "body": issue_body(first), "state": "open"}])
    unchanged = _write(_finding(run_ref="run-2"), api)
    assert unchanged[0]["action"] == "unchanged"
    assert api.posted_comments == []

    changed = _finding(
        evidence_identity=_provenance_identity(
            release=("v0.9.5", "v0.11.0"),
            source_files=[_source_fact("skills/herdr/SKILL.md", "3" * 64)],
        ),
        run_ref="run-3",
    )
    updated = _write(changed, api)
    assert updated[0]["action"] == "comment"
    assert len(api.posted_comments) == 1


@pytest.mark.parametrize("state", ["open", "closed"])
def test_partial_unknown_evidence_does_not_repeat_full_confirmed_finding(state: str) -> None:
    full_identity = _provenance_identity(
        source_files=[
            _source_fact("src/api/schema.rs", "3" * 64),
            _source_fact("skills/herdr/SKILL.md", OBSERVED_HASH),
        ]
    )
    full = _finding(evidence_identity=full_identity)
    api = FakeAPI([{"number": 20, "title": "existing", "body": issue_body(full, compact=True), "state": state}])
    release_unknown = _finding(
        evidence_identity=_provenance_identity(release=None, source_files=full_identity["source_files"])
    )
    one_file_unknown = _finding(
        evidence_identity=_provenance_identity(source_files=[full_identity["source_files"][0]])
    )

    assert _write(release_unknown, api)[0]["action"] == "unchanged"
    assert _write(one_file_unknown, api)[0]["action"] == "unchanged"
    assert api.posted_comments == []
    assert api.created == []


def test_partial_issue_history_retains_confirmed_facts_and_material_changes_update() -> None:
    fact_a = _source_fact("src/api/schema.rs", OBSERVED_HASH)
    fact_b = _source_fact("skills/herdr/SKILL.md", "3" * 64)
    partial = _finding(evidence_identity=_provenance_identity(release=None, source_files=[fact_a]))
    api = FakeAPI([{"number": 21, "title": "partial", "body": issue_body(partial, compact=True), "state": "open"}])

    assert _write(_finding(evidence_identity=_provenance_identity(release=None, source_files=[fact_a])), api)[0]["action"] == "unchanged"
    assert _write(_finding(evidence_identity=_provenance_identity(release=None, source_files=[fact_a])), api)[0]["action"] == "unchanged"

    recovered = _finding(evidence_identity=_provenance_identity(source_files=[fact_a, fact_b]))
    assert _write(recovered, api)[0]["action"] == "comment"
    recovered_handoff = extract_handoff(api.posted_comments[-1][1])
    recovered_identity = recovered_handoff["finding"]["evidence_identity"]
    assert recovered_identity["observed_release_tag"] == "v0.10.0"
    assert {item["path"] for item in recovered_identity["source_files"]} == {"src/api/schema.rs", "skills/herdr/SKILL.md"}

    partial_again = _finding(evidence_identity=_provenance_identity(release=None, source_files=[fact_a]))
    assert _write(partial_again, api)[0]["action"] == "unchanged"

    new_hash = _source_fact("src/api/schema.rs", "4" * 64)
    changed = _finding(evidence_identity=_provenance_identity(release=None, source_files=[new_hash]))
    assert _write(changed, api)[0]["action"] == "comment"
    changed_handoff = extract_handoff(api.posted_comments[-1][1])
    changed_identity = changed_handoff["finding"]["evidence_identity"]
    assert changed_identity["observed_release_tag"] == "v0.10.0"
    assert {item["path"] for item in changed_identity["source_files"]} == {"src/api/schema.rs", "skills/herdr/SKILL.md"}
    assert next(item for item in changed_identity["source_files"] if item["path"] == "src/api/schema.rs")["observed_sha256"] == "4" * 64


def test_confirmed_matching_file_retires_prior_drift_without_repeated_comments() -> None:
    initial_source = _source_evidence()
    initial_source["unknown"] = []
    initial_source["files"].append({
        "source_path": "src/api/schema.rs",
        "pinned_sha256": PINNED_HASH,
        "observed_sha256": "3" * 64,
        "immutable_url": f"https://github.com/herdrdev/herdr/blob/{OBSERVED}/src/api/schema.rs",
    })
    initial = build_findings({"source_watch": {"evidence": initial_source}})[0]
    api = FakeAPI([{"number": 23, "title": "existing", "body": issue_body(initial, compact=True), "state": "open"}])
    partial = build_findings({"source_watch": {"evidence": _source_evidence()}})[0]
    assert _write(partial, api)[0]["action"] == "unchanged"

    recovered_source = _source_evidence()
    recovered_source["unknown"] = []
    recovered_source["files"].append({
        **initial_source["files"][1],
        "observed_sha256": PINNED_HASH,
    })
    recovered = build_findings({"source_watch": {"evidence": recovered_source}})[0]
    assert "Watched file `src/api/schema.rs`" not in recovered["body"]
    assert _write(recovered, api)[0]["action"] == "comment"
    assert "Watched file `src/api/schema.rs` now matches its pinned hash" in api.posted_comments[-1][1]
    stored = extract_handoff(api.posted_comments[-1][1])["finding"]["evidence_identity"]
    assert next(item for item in stored["source_files"] if item["path"] == "src/api/schema.rs")["observed_sha256"] == PINNED_HASH

    assert _write(partial, api)[0]["action"] == "unchanged"
    assert _write(recovered, api)[0]["action"] == "unchanged"
    assert len(api.posted_comments) == 1


def test_new_matching_paths_do_not_comment_on_existing_three_field_issue() -> None:
    initial = _finding(evidence_identity=_provenance_identity(release=None))
    api = FakeAPI([{"number": 133, "title": "existing", "body": issue_body(initial, compact=True), "state": "open"}])
    expanded = _finding(evidence_identity=_provenance_identity(
        release=None,
        source_files=[
            _source_fact("skills/herdr/SKILL.md", OBSERVED_HASH),
            _source_fact("src/api/schema.rs", PINNED_HASH),
        ],
    ))

    assert _write(expanded, api)[0]["action"] == "unchanged"
    assert api.posted_comments == []


def test_confirmed_current_release_retires_stale_release_drift() -> None:
    initial = build_findings({
        "upstream": {"evidence": _release_evidence()},
        "source_watch": {"evidence": _source_evidence()},
    })[0]
    api = FakeAPI([{"number": 24, "title": "existing", "body": issue_body(initial, compact=True), "state": "open"}])
    current_release = _release_evidence()
    current_release["status"] = "current"
    current_release["observed"]["tag"] = "v0.9.5"
    current_release["observed"]["version"] = "0.9.5"
    current = build_findings({
        "upstream": {"evidence": current_release},
        "source_watch": {"evidence": _source_evidence()},
    })[0]

    assert "Stable release:" not in current["body"]
    assert current["evidence_identity"]["observed_release_tag"] == "v0.9.5"
    assert _write(current, api)[0]["action"] == "comment"
    assert "observed `v0.9.5` no longer exceeds pinned `v0.9.5`" in api.posted_comments[-1][1]
    stored = extract_handoff(api.posted_comments[-1][1])["finding"]["evidence_identity"]
    assert stored["observed_release_tag"] == "v0.9.5"
    partial = build_findings({"source_watch": {"evidence": _source_evidence()}})[0]
    assert _write(partial, api)[0]["action"] == "unchanged"
    assert _write(current, api)[0]["action"] == "unchanged"
    assert len(api.posted_comments) == 1
    all_clear_source = _source_evidence()
    all_clear_source["status"] = "current"
    all_clear_source["unknown"] = []
    all_clear_source["files"][0]["observed_sha256"] = PINNED_HASH
    assert build_findings({
        "upstream": {"evidence": current_release},
        "source_watch": {"evidence": all_clear_source},
    }) == []


def test_malformed_provenance_identity_is_not_silently_suppressed() -> None:
    malformed = {"pinned_release_tag": None, "observed_release_tag": None, "source_files": [{"path": "../unsafe", "pinned_sha256": PINNED_HASH, "observed_sha256": OBSERVED_HASH}]}
    old = _finding(evidence_identity=malformed)
    api = FakeAPI([{"number": 22, "title": "malformed", "body": issue_body(old, compact=True), "state": "open"}])

    result = _write(_finding(evidence_identity=malformed), api)

    assert result[0]["action"] == "comment"
    assert len(api.posted_comments) == 1


def test_closed_issue_suppresses_identical_evidence_but_new_evidence_creates_issue() -> None:
    original = _finding(
        evidence_identity=_provenance_identity(
            source_files=[
                _source_fact("src/api/schema.rs", "3" * 64),
                _source_fact("skills/herdr/SKILL.md", OBSERVED_HASH),
            ]
        )
    )
    api = FakeAPI([{ "number": 8, "title": "closed", "body": issue_body(original, compact=True), "state": "closed"}])
    assert _write(_finding(run_ref="run-2"), api)[0]["action"] == "unchanged"
    changed = _finding(
        evidence_identity=_provenance_identity(
            release=None,
            source_files=[_source_fact("src/api/schema.rs", "4" * 64)],
        )
    )
    result = _write(changed, api)
    assert result[0]["action"] == "create"
    assert len(api.created) == 1
    created_handoff = extract_handoff(api.created[0]["body"])
    created_identity = created_handoff["finding"]["evidence_identity"]
    assert created_identity["observed_release_tag"] == "v0.10.0"
    assert {item["path"] for item in created_identity["source_files"]} == {"src/api/schema.rs", "skills/herdr/SKILL.md"}
    assert next(item for item in created_identity["source_files"] if item["path"] == "src/api/schema.rs")["observed_sha256"] == "4" * 64


def test_closed_history_checks_latest_comments_across_all_matching_issues() -> None:
    evidence_a = _finding()
    evidence_b = _finding(
        evidence_identity=_provenance_identity(
            release=("v0.9.5", "v0.11.0"),
            source_files=[_source_fact("skills/herdr/SKILL.md", "3" * 64)],
        )
    )
    newer_issue = {"number": 10, "title": "newer", "body": issue_body(evidence_a), "state": "closed"}
    older_issue = {"number": 9, "title": "older", "body": issue_body(evidence_b), "state": "closed"}
    api = FakeAPI([newer_issue, older_issue])
    api.comments[10] = [{"body": issue_body(evidence_b)}]
    api.comments[9] = [{"body": issue_body(evidence_a)}]

    result = _write(_finding(run_ref="repeat-a"), api)

    assert result[0]["action"] == "unchanged"
    assert api.created == []
    assert api.posted_comments == []


def test_issue_discovery_failure_stops_before_any_write() -> None:
    class FailingAPI(FakeAPI):
        def list_issues(self, _label: str, *, state: str = "open") -> list[dict[str, Any]]:
            raise RuntimeError("issue inventory unavailable")

    api = FailingAPI()
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        _write(_finding(), api)
    assert api.created == []
    assert api.posted_comments == []


def test_comment_discovery_failure_stops_before_any_write() -> None:
    original = _finding()

    class FailingAPI(FakeAPI):
        def list_issue_comments(self, _number: int) -> list[dict[str, str]]:
            raise RuntimeError("comment inventory unavailable")

    api = FailingAPI([{ "number": 9, "title": "existing", "body": issue_body(original), "state": "open"}])
    with pytest.raises(RuntimeError, match="comment inventory unavailable"):
        _write(_finding(evidence_identity=_provenance_identity(release=("v0.9.5", "v0.11.0"))), api)
    assert api.created == []
    assert api.posted_comments == []


def test_github_issue_listing_excludes_pull_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    api = GitHubAPI(repository="CruxExperts/LocalSetup", token="token")
    monkeypatch.setattr(
        api,
        "_list_pages",
        lambda *_args, **_kwargs: [
            {"number": 1, "title": "issue"},
            {"number": 2, "title": "pull request", "pull_request": {"url": "example"}},
        ],
    )
    assert [item["number"] for item in api.list_issues("provenance-refresh", state="all")] == [1]


def test_refresh_returns_safe_unknown_evidence_without_inventing_an_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core.provenance_refresh import cli

    monkeypatch.setattr(
        cli,
        "check_provenance",
        lambda *_args, **_kwargs: {
            "manifest": "ls/skills/ls-herdr/references/upstream/provenance.json",
            "upstream": {"status": "unknown", "evidence": {"status": "unknown", "reason": "official metadata request failed"}},
            "source_watch": {"status": "unknown", "evidence": {"status": "unknown", "reason": "official metadata request failed"}},
        },
    )

    class DisabledAPI:
        def enabled(self) -> bool:
            return False

    def fail_if_called(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("no speculative finding should be written")

    result = cli.run_refresh(ROOT, api=DisabledAPI(), issue_writer=fail_if_called)
    assert result["coverage"] == "Herdr canonical provenance manifest only"
    assert result["upstream"] == "unknown"
    assert result["evidence"]["source_watch"]["reason"] == "official metadata request failed"
    assert result["issue_actions"] == []


def test_workflow_uses_central_daily_schedule_and_least_permissions() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/provenance-refresh.yml").read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"] == [{"cron": "0 6 * * *", "timezone": "America/Chicago"}]
    assert "workflow_dispatch" in triggers
    assert "push" not in triggers
    job = workflow["jobs"]["herdr-watch"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    assert "issues" not in workflow.get("permissions", {})
    assert any("python tools/provenance_refresh.py" in step.get("run", "") for step in job["steps"])
    assert not any("ls/tools/provenance_refresh.py" in step.get("run", "") for step in job["steps"])
    qc_workflow = yaml.safe_load((ROOT / ".github/workflows/qc-patrol.yml").read_text(encoding="utf-8"))
    qc_triggers = qc_workflow.get("on", qc_workflow.get(True))
    assert "schedule" in qc_triggers


def test_repository_wrapper_runs_and_workflow_stays_out_of_the_public_pack() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/provenance_refresh.py"), "--help"],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "Compare canonical Herdr pins" in result.stdout

    pack = load_pack_config(ROOT)
    assert ".github/workflows/provenance-refresh.yml" in pack.private_paths
    assert not any(path == "tools" or path.startswith("tools/") for path in pack.public_paths)
