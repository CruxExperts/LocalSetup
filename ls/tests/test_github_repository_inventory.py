from __future__ import annotations

import json

import pytest

import ls.core.github_repo.adapter as adapter_module
from ls.core.github_repo.adapter import GitHubError, GhCliAdapter
from ls.core.github_repo.inventory import REMOTE_CONTROL_IDS, build_snapshot
from ls.core.github_repo.model import RepositoryTarget


TARGET = RepositoryTarget(hostname="github.com", owner="Owner", repository="Repo")


class FakeInventoryAdapter:
    def __init__(self) -> None:
        self.repo = {
            "id": 90123,
            "full_name": "Owner/Repo",
            "description": "fixture description",
            "homepage": "https://example.invalid",
            "private": False,
            "visibility": "public",
            "default_branch": "main",
            "is_template": False,
            "has_issues": True,
            "has_projects": True,
            "has_wiki": False,
            "has_downloads": True,
            "has_pages": True,
            "has_discussions": False,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": True,
            "allow_auto_merge": True,
            "delete_branch_on_merge": True,
            "squash_merge_commit_title": "PR_TITLE",
            "squash_merge_commit_message": "COMMIT_MESSAGES",
            "web_commit_signoff_required": True,
            "issues_url": "https://api.github.com/repos/Owner/Repo/issues{/number}",
            "pulls_url": "https://api.github.com/repos/Owner/Repo/pulls{/number}",
            "permissions": {"admin": True},
            "security_and_analysis": {
                "dependency_graph": {"status": "enabled"},
                "code_security": {"status": "enabled"},
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "disabled"},
                "private_vulnerability_reporting": {"status": "disabled"},
            },
        }
        self.branch_detail = {
            "id": 101,
            "name": "Owner branch",
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "required_linear_history"},
                {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci"}]}},
            ],
            "bypass_actors": [{"actor_id": 333, "actor_type": "User", "bypass_mode": "always"}],
        }
        self.tag_detail = {
            "id": 102,
            "name": "Release tags",
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": "creation"}, {"type": "update"}],
            "bypass_actors": [{"actor_id": 444, "actor_type": "Team", "bypass_mode": "pull_request"}],
        }
        self.ruleset_rows = [
            {**{key: value for key, value in self.branch_detail.items() if key not in {"rules", "bypass_actors", "conditions"}}, "source_type": "Repository", "source": "Owner/Repo"},
            {**{key: value for key, value in self.tag_detail.items() if key not in {"rules", "bypass_actors", "conditions"}}, "source_type": "Repository", "source": "Owner/Repo"},
            {
                "id": 103,
                "name": "Org inherited",
                "target": "branch",
                "source_type": "Organization",
                "source": "Organization",
                "enforcement": "active",
                "rules": [{"type": "pull_request"}],
            },
        ]

    def auth_capabilities(self):
        return {"authenticated": True, "scope_visibility": "not_reported", "scopes": None}

    def actor(self):
        return {"id": 7001, "login": "fixture-actor"}

    def repository(self):
        return self.repo

    def topics(self):
        return ["agent-tools", "localsetup"]

    def rulesets(self):
        return self.ruleset_rows

    def get_ruleset(self, ruleset_id):
        return {101: self.branch_detail, 102: self.tag_detail}[ruleset_id]

    def actions_permissions(self):
        return {"enabled": True, "allowed_actions": "selected", "sha_pinning_required": True}

    def workflow_permissions(self):
        return {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}

    def selected_actions(self):
        return {"github_owned_allowed": True, "verified_allowed": True, "patterns_allowed": []}

    def pages(self):
        return None

    def vulnerability_alerts_enabled(self):
        return True

    def automated_security_fixes_enabled(self):
        return True

    def private_vulnerability_reporting_enabled(self):
        return False

    def dependabot_alert_count(self):
        return 2

    def code_scanning_alert_count(self):
        return 1

    def immutable_releases_enabled(self):
        return True

    def releases_summary(self):
        return {"release_count": 2, "published_release_count": 2, "latest": {"tag_name": "v1"}}

    def social_preview_custom(self):
        return False

    def effective_branch_rules(self, branch):
        assert branch == "main"
        return [
            {
                "type": "required_status_checks",
                "source_type": "Repository",
                "source": "Owner/Repo",
                "enforcement": "active",
                "parameters": {"required_status_checks": [{"context": "ci"}]},
            }
        ]

    def legacy_branch_protection(self, branch):
        assert branch == "main"
        return {
            "required_status_checks": {"contexts": ["ci"], "strict": True},
            "required_pull_request_reviews": {
                "required_approving_review_count": 2,
                "require_code_owner_reviews": True,
            },
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_conversation_resolution": {"enabled": True},
            "restrictions": {"users": [{"login": "private-user"}], "teams": [], "apps": []},
        }

    def required_signatures(self, branch):
        assert branch == "main"
        return {"enabled": True}

    def branch_head_sha(self, branch):
        assert branch == "main"
        return "a" * 40

    def workflow_inventory(self):
        return {"value": {"count": 2, "state_counts": {"active": 2}}, "pagination": {"complete": True, "pages": 1}}

    def workflow_runs_summary(self, branch):
        assert branch == "main"
        return {
            "value": {"observed_run_count": 1, "reported_total": 1, "status_counts": {"completed": 1}, "conclusion_counts": {"success": 1}},
            "pagination": {"complete": True, "pages": 1},
        }

    def check_runs_summary(self, ref):
        assert ref == "a" * 40
        return {
            "value": {"observed_check_run_count": 1, "reported_total": 1, "status_counts": {"completed": 1}, "conclusion_counts": {"success": 1}},
            "pagination": {"complete": True, "pages": 1},
        }

    def commit_status_summary(self, ref):
        assert ref == "a" * 40
        return {
            "value": {"aggregate_state": "success", "total_count": 1, "observed_status_count": 1, "status_counts": {"success": 1}},
            "pagination": {"complete": True, "pages": 1},
        }

    def environments_summary(self):
        return {"value": {"count": 1, "names": ["production"], "required_reviewer_rule_counts": [1]}, "pagination": {"complete": True, "pages": 1}}

    def deployments_summary(self):
        return {"value": {"count": 1, "environment_counts": {"production": 1}, "latest": {"status": "success"}}, "pagination": {"complete": True, "pages": 1}}

    def pages_read(self):
        raise GitHubError(404, operation="pages_read")

    def pages_health(self):
        raise GitHubError(403, operation="pages_health")

    def secret_scanning_alert_count(self):
        return 3

    def custom_secret_pattern_count(self):
        return {"value": {"count": 1}, "pagination": {"complete": True, "pages": 1}}

    def sbom_summary(self):
        return {"format": "SPDX-2.3", "package_count": 4}

    def latest_release_summary(self):
        return {"id": 51, "tag_name": "v1", "published_at": "2026-01-01T00:00:00Z", "prerelease": False}

    def release_inventory(self):
        return {
            "value": {
                "release_count": 2,
                "published_release_count": 2,
                "newest_by_published_at": {"tag_name": "v2"},
                "latest_endpoint_ordering_may_differ": True,
            },
            "pagination": {"complete": True, "pages": 1},
        }

    def release_assets_summary(self):
        return {
            "value": {"release_count": 2, "asset_count": 2, "asset_digest_count": 1, "unavailable_release_count": 0},
            "pagination": {"complete": True, "release_pages": 1, "asset_pages": 2},
        }


def _rows_by_id(snapshot):
    return {row["control_id"]: row for row in snapshot["control_observations"]}


def test_snapshot_emits_exact_remote_registry_with_safe_individual_evidence():
    snapshot = build_snapshot(FakeInventoryAdapter(), TARGET)
    expected = {
        f"{group}.{name}"
        for group, names in REMOTE_CONTROL_IDS.items()
        for name in names
    }
    rows = _rows_by_id(snapshot)

    assert len(snapshot["control_observations"]) == len(expected)
    assert set(rows) == expected
    assert all(not str(row["reason"] or "").startswith("documented_read_collector_not_implemented") for row in rows.values())
    assert set(snapshot["groups"]) == set(REMOTE_CONTROL_IDS)
    assert all(rows[f"{group}.{name}"]["group"] == group for group, names in REMOTE_CONTROL_IDS.items() for name in names)
    assert rows["identity_discovery.default_branch"]["value"] == "main"
    assert rows["identity_discovery.social_preview_state"]["value"] is False
    assert rows["identity_discovery.feature_links"]["value"]["validity_checked"] is False
    assert rows["git_governance.branch_rulesets"]["authority"] == "unknown"
    assert rows["git_governance.required_signatures"]["value"] == {"enabled": True}
    assert rows["git_governance.required_check_health"]["value"]["assessment"] == "not_assessed_without_correlation_to_effective_required_contexts"
    assert rows["actions_deployment.pages_build_source"]["observation"] == "unavailable"
    assert rows["actions_deployment.pages_build_source"]["value"] is None
    assert rows["actions_deployment.pages_health"]["http_status"] == 403
    assert rows["security_supply_chain.private_vulnerability_reporting"]["value"] is False
    assert rows["security_supply_chain.custom_secret_patterns"]["value"] == {"count": 1}
    assert rows["releases.latest_release_endpoint"]["value"]["tag_name"] == "v1"
    assert rows["releases.newest_published_release"]["value"]["newest_by_published_at"]["tag_name"] == "v2"
    assert rows["releases.checksums"]["value"]["verification"] == "digest_presence_only_not_checksum_verification"
    assert "repository_content.social_preview_image_handoff" not in rows

    rendered = json.dumps(snapshot["control_observations"], sort_keys=True)
    for private_value in ("private-user", "333", "444", "pattern-secret", "private-alert-details"):
        assert private_value not in rendered


def test_unresolved_controls_have_specific_report_only_reasons():
    rows = _rows_by_id(build_snapshot(FakeInventoryAdapter(), TARGET))
    expected_reasons = {
        "collaboration.branch_update_suggestions": "repository_update_suggestion_read_field_not_resolved_in_official_matrix",
        "collaboration.discussions": "repository_read_is_available_but_rest_update_field_is_unresolved",
        "git_governance.merge_queue_readiness": "graphql_merge_queue_permission_mapping_unresolved",
        "security_supply_chain.secret_validity_checks": "validity_check_rest_field_and_endpoint_mapping_unresolved",
        "security_supply_chain.malware_findings": "dependabot_malware_classification_is_not_safely_exposed_as_an_aggregate_field",
        "releases.release_workflow_status": "release_to_workflow_and_artifact_attestation_link_not_verified",
    }
    for control_id, reason in expected_reasons.items():
        assert rows[control_id]["reason"] == reason
        assert rows[control_id]["observation"] == (
            "observed" if control_id == "collaboration.discussions" else "unknown"
        )


def test_gh_adapter_uses_fixed_read_routes_and_paginated_redacted_summaries(monkeypatch):
    calls = []

    def fake_run(argv, *, input_data=None, timeout):
        assert isinstance(argv, list)
        assert argv[0:2] == ["gh-fixture", "api"]
        assert timeout > 0
        route = argv[-1]
        calls.append((argv, {"input_data": input_data}, route))
        if route.startswith("repos/Owner/Repo/rules/branches/"):
            payload = []
        elif route.endswith("/protection/required_signatures"):
            payload = {"enabled": True}
        elif route.endswith("/protection"):
            payload = {"required_status_checks": None}
        elif route.endswith("/actions/workflows?"):
            payload = {"total_count": 1, "workflows": [{"id": 1, "state": "active"}]}
        elif route.startswith("repos/Owner/Repo/actions/workflows?"):
            payload = {"total_count": 1, "workflows": [{"id": 1, "state": "active"}]}
        elif route.startswith("repos/Owner/Repo/actions/runs?"):
            payload = {"total_count": 1, "workflow_runs": [{"id": 2, "status": "completed", "conclusion": "success"}]}
        elif route == "repos/Owner/Repo/actions/permissions":
            payload = {"enabled": True, "allowed_actions": "selected", "sha_pinning_required": True}
        elif route == "repos/Owner/Repo/actions/permissions/workflow":
            payload = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
        elif route == "repos/Owner/Repo/actions/permissions/selected-actions":
            payload = {"github_owned_allowed": True, "verified_allowed": True, "patterns_allowed": []}
        elif route == "repos/Owner/Repo/rulesets?per_page=100&page=1&includes_parents=true":
            payload = [{"id": 10, "name": "main", "target": "branch", "source_type": "Repository"}]
        elif route == "repos/Owner/Repo/rulesets/10":
            payload = {
                "id": 10, "name": "main", "target": "branch", "enforcement": "active",
                "conditions": {}, "rules": [], "bypass_actors": [],
            }
        elif route.startswith("repos/Owner/Repo/commits/"):
            if route.endswith("/check-runs?per_page=100&page=1"):
                payload = {"total_count": 1, "check_runs": [{"id": 3, "status": "completed", "conclusion": "success"}]}
            elif route.endswith("/status"):
                payload = {"state": "success", "total_count": 1}
            elif "/statuses?" in route:
                payload = [{"state": "success"}]
            else:
                payload = {"sha": "a" * 40}
        elif route.startswith("repos/Owner/Repo/deployments/environments?"):
            payload = [{"name": "production", "protection_rules": []}]
        elif route.startswith("repos/Owner/Repo/deployments?"):
            payload = [{"id": 8, "environment": "production", "created_at": "2026-01-01T00:00:00Z"}]
        elif route.startswith("repos/Owner/Repo/deployments/8/statuses?"):
            payload = [{"state": "success", "created_at": "2026-01-01T00:01:00Z"}]
        elif route == "repos/Owner/Repo/pages":
            payload = {"build_type": "legacy", "source": {"branch": "main", "path": "/"}, "cname": "docs.example.invalid", "https_enforced": True}
        elif route == "repos/Owner/Repo/pages/health":
            payload = {"domain": "docs.example.invalid", "is_https_eligible": True, "dns": {"a": "valid"}}
        elif route.startswith("repos/Owner/Repo/secret-scanning/custom-patterns?"):
            payload = [{"name": "private-pattern-name", "pattern": "private-pattern-body"}]
        elif route.startswith("repos/Owner/Repo/secret-scanning/alerts?"):
            payload = [{"number": 3, "secret": "private-secret-value"}]
        elif route == "repos/Owner/Repo/private-vulnerability-reporting":
            payload = {"enabled": True}
        elif route == "repos/Owner/Repo/vulnerability-alerts" or route == "repos/Owner/Repo/automated-security-fixes":
                return b"", b"gh: HTTP 404", 1
        elif route == "repos/Owner/Repo/dependency-graph/sbom":
            payload = {"sbom": {"spdxVersion": "SPDX-2.3", "packages": [{"name": "private-package-name"}]}}
        elif route == "repos/Owner/Repo/releases/latest":
            payload = {"id": 9, "tag_name": "latest", "assets": [], "published_at": "2026-01-01T00:00:00Z"}
        elif route.startswith("repos/Owner/Repo/releases?"):
            payload = [{"id": 9, "tag_name": "newest", "draft": False, "prerelease": False, "published_at": "2026-02-01T00:00:00Z"}]
        elif route == "repos/Owner/Repo/releases/9/assets?per_page=100&page=1":
            payload = [{"id": 91, "name": "release.zip", "digest": "sha256:abcd"}]
        else:
            raise AssertionError(f"unexpected route: {route}")
        return json.dumps(payload).encode("utf-8"), b"", 0

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", fake_run)
    adapter = GhCliAdapter(TARGET, gh_executable="gh-fixture")

    assert adapter.effective_branch_rules("feature/a") == []
    assert adapter.rulesets()[0]["id"] == 10
    assert adapter.get_ruleset(10)["bypass_actors"] == []
    assert adapter.legacy_branch_protection("main") == {"required_status_checks": None}
    assert adapter.required_signatures("main") == {"enabled": True}
    assert adapter.actions_permissions()["sha_pinning_required"] is True
    assert adapter.workflow_permissions()["default_workflow_permissions"] == "read"
    assert adapter.selected_actions()["verified_allowed"] is True
    assert adapter.workflow_inventory()["value"]["count"] == 1
    assert adapter.workflow_runs_summary("main")["value"]["reported_total"] == 1
    assert adapter.check_runs_summary("a" * 40)["value"]["observed_check_run_count"] == 1
    assert adapter.commit_status_summary("a" * 40)["value"]["aggregate_state"] == "success"
    assert adapter.environments_summary()["value"]["names"] == ["production"]
    assert adapter.deployments_summary()["value"]["latest"]["status"] == "success"
    assert adapter.pages_read()["https_enforced"] is True
    assert adapter.pages_health()["is_https_eligible"] is True
    assert adapter.custom_secret_pattern_count()["value"]["count"] == 1
    assert "private-pattern-name" not in json.dumps(adapter.custom_secret_pattern_count())
    assert "private-pattern-body" not in json.dumps(adapter.custom_secret_pattern_count())
    assert adapter.secret_scanning_alert_count() == 1
    assert adapter.private_vulnerability_reporting_enabled() is True
    assert adapter.vulnerability_alerts_enabled() is False
    assert adapter.automated_security_fixes_enabled() is False
    assert adapter.sbom_summary()["package_count"] == 1
    assert adapter.latest_release_summary()["tag_name"] == "latest"
    assert adapter.release_inventory()["value"]["newest_by_published_at"]["tag_name"] == "newest"
    assert adapter.release_assets_summary()["value"]["asset_digest_count"] == 1

    routes = [route for _, _, route in calls]
    assert "repos/Owner/Repo/rules/branches/feature%2Fa" in routes
    assert "repos/Owner/Repo/branches/main/protection/required_signatures" in routes
    assert "repos/Owner/Repo/actions/workflows?per_page=100&page=1" in routes
    assert "repos/Owner/Repo/actions/permissions" in routes
    assert "repos/Owner/Repo/actions/permissions/workflow" in routes
    assert "repos/Owner/Repo/actions/permissions/selected-actions" in routes
    assert "repos/Owner/Repo/rulesets?per_page=100&page=1&includes_parents=true" in routes
    assert "repos/Owner/Repo/rulesets/10" in routes
    assert "repos/Owner/Repo/vulnerability-alerts" in routes
    assert "repos/Owner/Repo/automated-security-fixes" in routes
    assert "repos/Owner/Repo/deployments/8/statuses?per_page=100&page=1" in routes
    assert "repos/Owner/Repo/secret-scanning/custom-patterns?per_page=100&page=1" in routes
    assert "repos/Owner/Repo/secret-scanning/alerts?state=open&per_page=100&page=1" in routes
    assert "repos/Owner/Repo/private-vulnerability-reporting" in routes
    assert "repos/Owner/Repo/releases/latest" in routes
    assert "repos/Owner/Repo/releases/9/assets?per_page=100&page=1" in routes
    assert all("private-pattern-body" not in json.dumps(kwargs) for _, kwargs, _ in calls)


def test_workflow_run_collection_marks_api_ceiling_incomplete(monkeypatch):
    request_count = 0

    def fake_run(argv, *, input_data=None, timeout):
        nonlocal request_count
        request_count += 1
        assert timeout > 0
        route = argv[-1]
        assert route.startswith("repos/Owner/Repo/actions/runs?")
        payload = {
            "total_count": 1500,
            "workflow_runs": [{"id": request_count, "status": "completed", "conclusion": "success"}] * 100,
        }
        return json.dumps(payload).encode("utf-8"), b"", 0

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", fake_run)
    result = GhCliAdapter(TARGET, gh_executable="gh-fixture").workflow_runs_summary("main")
    assert request_count == 10
    assert result["pagination"]["complete"] is False
    assert result["value"]["observed_run_count"] == 1000


@pytest.mark.parametrize(
    ("method", "operation"),
    [
        ("pages_read", "pages_read"),
        ("pages", "pages_read"),
        ("immutable_releases_enabled", "immutable_releases_status"),
    ],
)
def test_ambiguous_404_is_unavailable_not_disabled(monkeypatch, method, operation):
    adapter = GhCliAdapter(TARGET, gh_executable="gh-fixture")

    def missing(*args, **kwargs):
        raise GitHubError(404, operation=operation)

    monkeypatch.setattr(adapter, "_request", missing)
    with pytest.raises(GitHubError) as exc_info:
        getattr(adapter, method)()
    assert exc_info.value.status == 404


@pytest.mark.parametrize("status", [403, 404, 422])
def test_private_vulnerability_reporting_unavailable_status_is_not_disabled(monkeypatch, status):
    adapter = GhCliAdapter(TARGET, gh_executable="gh-fixture")

    def unavailable(*args, **kwargs):
        raise GitHubError(status, operation="private_vulnerability_reporting_status")

    monkeypatch.setattr(adapter, "_request", unavailable)
    with pytest.raises(GitHubError) as exc_info:
        adapter.private_vulnerability_reporting_enabled()
    assert exc_info.value.status == status


@pytest.mark.parametrize(
    ("method", "endpoint"),
    [
        ("vulnerability_alerts_enabled", "vulnerability-alerts"),
        ("automated_security_fixes_enabled", "automated-security-fixes"),
    ],
)
def test_documented_toggle_404_means_disabled(monkeypatch, method, endpoint):
    adapter = GhCliAdapter(TARGET, gh_executable="gh-fixture")

    def not_enabled(*args, **kwargs):
        raise GitHubError(404, operation=endpoint)

    monkeypatch.setattr(adapter, "_request", not_enabled)
    assert getattr(adapter, method)() is False


@pytest.mark.parametrize(
    ("method", "payload", "operation"),
    [
        ("topics", {"names": ["valid-topic", None]}, "repository_topics"),
        ("rulesets", [{"id": 1, "name": "main"}, "malformed-row"], "repository_rulesets"),
    ],
)
def test_repository_write_preconditions_reject_malformed_list_entries(monkeypatch, method, payload, operation):
    adapter = GhCliAdapter(TARGET, gh_executable="gh-fixture")

    def malformed_list(method_name, endpoint, *, operation):
        assert method_name == "GET"
        return payload

    monkeypatch.setattr(adapter, "_request", malformed_list)
    with pytest.raises(GitHubError) as exc_info:
        getattr(adapter, method)()
    assert exc_info.value.operation == operation
