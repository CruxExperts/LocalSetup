from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import sys

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

import ls.core.github_repo.adapter as adapter_module
import ls.core.github_repo.checkout as checkout_module
import ls.core.github_repo.service as service_module
from ls.core.github_repo.adapter import GitHubError, GhCliAdapter
from ls.core.github_repo.model import RepositoryTarget, canonical_ruleset, make_operation, plan_digest
from ls.core.github_repo.planning import operation_reduces_protection, ruleset_reduces_protection
from ls.core.github_repo.policy import PolicyError, normalize_policy, parse_target, read_policy
from ls.core.github_repo.service import (
    ApplyError,
    PlanError,
    apply_plan,
    audit,
    create_plan,
    read_plan,
    validate_plan,
    verify_plan,
    write_plan_pair,
)
from ls.core.github_repo.cli import _reject_unrelated_flags
from ls.core.github_repo.cli import _reject_unrelated_flags
from ls.core.cli import _add_config_flags, _add_harness_target_flags, _add_selector_flags, _add_visual_flags
from ls.core.cli_parser import build_parser
from ls.core.github_repo.state import (
    JournalError,
    TargetOperationBusy,
    append_journal,
    operation_state_directory,
    read_journal,
    target_operation_lock,
)


class FakeGitHubAdapter:
    """Small in-memory adapter for repository enhancement service tests."""

    def __init__(
        self,
        *,
        hostname: str = "github.com",
        custom_social_preview: bool = False,
        private_alert_details: tuple[str, ...] = (),
    ) -> None:
        self.hostname = hostname
        self.actor_id = 7001
        self.repo = {
            "id": 90123,
            "full_name": "Owner/Repo",
            "description": "before",
            "homepage": "",
            "private": False,
            "visibility": "public",
            "default_branch": "main",
            "has_issues": True,
            "has_projects": True,
            "has_wiki": True,
            "has_downloads": True,
            "has_pages": False,
            "has_discussions": False,
            "allow_squash_merge": True,
            "allow_merge_commit": True,
            "allow_rebase_merge": True,
            "allow_auto_merge": False,
            "delete_branch_on_merge": False,
            "web_commit_signoff_required": True,
            "squash_merge_commit_title": "PR_TITLE",
            "squash_merge_commit_message": "COMMIT_MESSAGES",
            "permissions": {"admin": True},
            "security_and_analysis": {
                "advanced_security": {"status": "disabled"},
                "code_security": {"status": "disabled"},
                "secret_scanning": {"status": "disabled"},
                "secret_scanning_push_protection": {"status": "disabled"},
                "private_vulnerability_reporting": {"status": "disabled"},
            },
        }
        self._topics: list[str] = []
        self._social_preview = custom_social_preview
        self._private_alert_details = private_alert_details
        self._private_reporting_enabled = False
        self._private_reporting_error: int | None = None
        self.auth = {"authenticated": True, "scope_visibility": "not_reported", "scopes": None}
        self.actions = {"enabled": True, "allowed_actions": "all", "sha_pinning_required": False}
        self.workflow = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
        self.selected = {"github_owned_allowed": True, "verified_allowed": True, "patterns_allowed": []}
        self.pages_state: dict[str, object] | None = None
        self.branches = {"main": "a" * 40, "develop": "b" * 40}
        self.rule_rows: list[dict[str, object]] = []
        self.rule_details: dict[int, dict[str, object]] = {}
        self.rule_detail_error = False
        self.apply_calls: list[str] = []
        self.before_apply = None
        self.ambiguous_at: str | None = None

    def auth_capabilities(self) -> dict[str, object]:
        return dict(self.auth)

    def actor(self) -> dict[str, object]:
        return {"id": self.actor_id, "login": "fixture-actor"}

    def repository(self) -> dict[str, object]:
        return self.repo

    def topics(self) -> list[str]:
        return list(self._topics)

    def rulesets(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.rule_rows]

    def get_ruleset(self, ruleset_id: int) -> dict[str, object]:
        if self.rule_detail_error:
            raise GitHubError(403, operation="repository_ruleset")
        if ruleset_id not in self.rule_details:
            raise GitHubError(404, operation="repository_ruleset")
        return dict(self.rule_details[ruleset_id])

    def actions_permissions(self) -> dict[str, object]:
        return dict(self.actions)

    def workflow_permissions(self) -> dict[str, object]:
        return dict(self.workflow)

    def selected_actions(self) -> dict[str, object]:
        return dict(self.selected)

    def pages(self) -> None:
        return dict(self.pages_state) if self.pages_state is not None else None

    def effective_branch_rules(self, branch: str) -> list[dict[str, object]]:
        assert branch == "main"
        return []

    def legacy_branch_protection(self, branch: str) -> dict[str, object]:
        assert branch == "main"
        raise GitHubError(404, operation="legacy_branch_protection")

    def required_signatures(self, branch: str) -> dict[str, object]:
        assert branch == "main"
        return {"enabled": False}

    def branch_head_sha(self, branch: str) -> str:
        if branch not in self.branches:
            raise GitHubError(404, operation="branch_head")
        return self.branches[branch]

    def workflow_inventory(self) -> dict[str, object]:
        return {"value": {"count": 0, "state_counts": {}}, "pagination": {"complete": True, "pages": 1}}

    def workflow_runs_summary(self, branch: str) -> dict[str, object]:
        assert branch == "main"
        return {
            "value": {"observed_run_count": 0, "reported_total": 0, "status_counts": {}, "conclusion_counts": {}},
            "pagination": {"complete": True, "pages": 1},
        }

    def check_runs_summary(self, ref: str) -> dict[str, object]:
        assert ref == "a" * 40
        return {
            "value": {"observed_check_run_count": 0, "reported_total": 0, "status_counts": {}, "conclusion_counts": {}},
            "pagination": {"complete": True, "pages": 1},
        }

    def commit_status_summary(self, ref: str) -> dict[str, object]:
        assert ref == "a" * 40
        return {
            "value": {"aggregate_state": "none", "total_count": 0, "observed_status_count": 0, "status_counts": {}},
            "pagination": {"complete": True, "pages": 1},
        }

    def environments_summary(self) -> dict[str, object]:
        return {"value": {"count": 0, "names": [], "required_reviewer_rule_counts": []}, "pagination": {"complete": True, "pages": 1}}

    def deployments_summary(self) -> dict[str, object]:
        return {"value": {"count": 0, "environment_counts": {}, "latest": None}, "pagination": {"complete": True, "pages": 1}}

    def pages_read(self) -> dict[str, object]:
        raise GitHubError(404, operation="pages_read")

    def pages_health(self) -> dict[str, object]:
        raise GitHubError(404, operation="pages_health")

    def secret_scanning_alert_count(self) -> int:
        return 0

    def custom_secret_pattern_count(self) -> dict[str, object]:
        return {"value": {"count": 0}, "pagination": {"complete": True, "pages": 1}}

    def sbom_summary(self) -> dict[str, object]:
        return {"format": "SPDX-2.3", "package_count": 0}

    def latest_release_summary(self) -> dict[str, object]:
        raise GitHubError(404, operation="latest_release")

    def release_inventory(self) -> dict[str, object]:
        return {
            "value": {"release_count": 0, "published_release_count": 0, "newest_by_published_at": None, "latest_endpoint_ordering_may_differ": True},
            "pagination": {"complete": True, "pages": 1},
        }

    def release_assets_summary(self) -> dict[str, object]:
        return {
            "value": {"release_count": 0, "asset_count": 0, "asset_digest_count": 0, "unavailable_release_count": 0},
            "pagination": {"complete": True, "release_pages": 1, "asset_pages": 0},
        }

    def vulnerability_alerts_enabled(self) -> bool:
        return True

    def automated_security_fixes_enabled(self) -> bool:
        return True

    def private_vulnerability_reporting_enabled(self) -> bool:
        if self._private_reporting_error is not None:
            raise GitHubError(self._private_reporting_error, operation="private_vulnerability_reporting_status")
        return self._private_reporting_enabled

    def dependabot_alert_count(self) -> int:
        return len(self._private_alert_details)

    def code_scanning_alert_count(self) -> int:
        return 2

    def immutable_releases_enabled(self) -> bool:
        return True

    def releases_summary(self) -> dict[str, object]:
        return {"release_count": 1, "published_release_count": 1, "latest": {"tag_name": "v1.0.0"}}

    def social_preview_custom(self) -> bool:
        return self._social_preview

    def operation_state(self, operation: dict[str, object]) -> object:
        kind = operation["kind"]
        desired = operation["desired"]
        if kind == "repository_patch":
            assert isinstance(desired, dict)
            return {key: self.repo.get(key) for key in desired}
        if kind == "repository_visibility":
            return self.repo.get("visibility")
        if kind == "repository_default_branch":
            branch = desired["default_branch"]
            return {
                "default_branch": self.repo.get("default_branch"),
                "target_branch": branch,
                "target_branch_sha": self.branch_head_sha(branch),
            }
        if kind == "topics_replace":
            return list(self._topics)
        if kind == "ruleset_upsert":
            ruleset_id = operation["resource_id"]
            if ruleset_id is None:
                matches = [row for row in self.rule_rows if row.get("name") == desired.get("name") and row.get("target") == desired.get("target") and row.get("source_type") == "Repository"]
                if not matches:
                    return None
                ruleset_id = matches[0]["id"]
            return canonical_ruleset(self.rule_details[ruleset_id])
        if kind == "actions_repository_policy":
            return {key: self.actions[key] for key in desired if key in self.actions}
        if kind == "actions_workflow_policy":
            return {key: self.workflow[key] for key in desired if key in self.workflow}
        if kind == "actions_selected_policy":
            return {key: self.selected[key] for key in desired if key in self.selected}
        if kind in {"pages_create", "pages_update"}:
            if self.pages_state is None:
                return None
            return {key: self.pages_state[key] for key in desired if key in self.pages_state}
        if kind == "security_analysis_patch":
            security = self.repo.get("security_and_analysis", {})
            assert isinstance(desired, dict)
            return {
                key: security.get(key, {}).get("status") == "enabled"
                for key in desired
            }
        if kind == "private_vulnerability_reporting_toggle":
            return self.private_vulnerability_reporting_enabled()
        raise AssertionError(f"unexpected fixture operation kind: {kind}")

    def apply_operation(self, operation: dict[str, object]) -> None:
        operation_id = str(operation["id"])
        self.apply_calls.append(operation_id)
        if self.before_apply is not None:
            self.before_apply(operation)
        if self.ambiguous_at == "before":
            raise GitHubError(None, operation="fixture_mutation", ambiguous=True)
        kind = operation["kind"]
        desired = operation["desired"]
        if kind == "repository_patch":
            assert isinstance(desired, dict)
            self.repo.update(desired)
        elif kind == "repository_visibility":
            self.repo["visibility"] = desired
            self.repo["private"] = desired == "private"
        elif kind == "repository_default_branch":
            self.repo["default_branch"] = desired["default_branch"]
        elif kind == "topics_replace":
            assert isinstance(desired, list)
            self._topics = list(desired)
        elif kind == "ruleset_upsert":
            ruleset_id = operation["resource_id"] or 6001
            assert isinstance(desired, dict)
            self.rule_details[ruleset_id] = {"id": ruleset_id, **desired}
            self.rule_rows = [{"id": ruleset_id, **desired, "source_type": "Repository"}]
        elif kind == "actions_repository_policy":
            self.actions.update(desired)
        elif kind == "actions_workflow_policy":
            self.workflow.update(desired)
        elif kind == "actions_selected_policy":
            self.selected.update(desired)
        elif kind in {"pages_create", "pages_update"}:
            self.pages_state = {**(self.pages_state or {}), **desired}
        elif kind == "security_analysis_patch":
            assert isinstance(desired, dict)
            for key, enabled in desired.items():
                self.repo["security_and_analysis"][key] = {"status": "enabled" if enabled else "disabled"}
        elif kind == "private_vulnerability_reporting_toggle":
            self._private_reporting_enabled = bool(desired)
            self.repo["security_and_analysis"]["private_vulnerability_reporting"] = {"status": "enabled" if desired else "disabled"}
        else:
            raise AssertionError(f"unexpected fixture operation kind: {kind}")
        if self.ambiguous_at == "after":
            raise GitHubError(None, operation="fixture_mutation", ambiguous=True)


def _policy(**sections: object) -> dict[str, object]:
    return normalize_policy({"schema_version": 2, **sections})


def _target(hostname: str = "github.com") -> RepositoryTarget:
    return parse_target(hostname, "Owner/Repo")


@pytest.fixture(autouse=True)
def _bind_default_checkout_for_service_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service fixtures use an in-memory Owner/Repo remote, not this checkout's origin."""
    original = service_module.snapshot_checkout

    def bound_snapshot(path: object, target: RepositoryTarget) -> dict[str, object]:
        result = original(path, target)
        if result.get("supported") is True:
            origin = result.get("origin")
            if isinstance(origin, dict):
                result["origin"] = {"configured": True, "matches_target": True}
            upstream = result.get("upstream")
            if isinstance(upstream, dict) and upstream.get("configured") is True:
                result["upstream"] = {**upstream, "matches_target": True}
        return result

    monkeypatch.setattr(service_module, "snapshot_checkout", bound_snapshot)


_KEEP = object()


def _reissue_operation(
    plan: dict[str, object],
    index: int,
    *,
    current: object = _KEEP,
    desired: object = _KEEP,
) -> None:
    old = plan["operations"][index]
    assert isinstance(old, dict)
    replacement = make_operation(
        group=old["group"],
        kind=old["kind"],
        resource_id=old["resource_id"],
        current=old["current"] if current is _KEEP else current,
        desired=old["desired"] if desired is _KEEP else desired,
        required_permissions=tuple(old["required_permissions"]),
        risk=old["risk"],
        verification=old["verification"],
        recovery=old["recovery"],
    ).to_dict()
    plan["operations"][index] = replacement
    plan["operation_ids"] = [operation["id"] for operation in plan["operations"]]
    plan["plan_digest"] = plan_digest(plan)


def _schemas() -> tuple[dict[str, object], Draft202012Validator, Draft202012Validator]:
    config_dir = Path(__file__).parents[1] / "config"
    policy_schema = json.loads((config_dir / "github-repository-policy.schema.json").read_text(encoding="utf-8"))
    plan_schema = json.loads((config_dir / "github-repository-plan.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resource(policy_schema["$id"], Resource.from_contents(policy_schema))
    return (
        policy_schema,
        Draft202012Validator(policy_schema),
        Draft202012Validator(plan_schema, registry=registry),
    )


def _ruleset_full_state(ruleset_id: int = 6100) -> dict[str, object]:
    return {
        "id": ruleset_id,
        "name": "main-protection",
        "target": "branch",
        "source_type": "Repository",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_signatures"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 2,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": True,
                    "require_last_push_approval": True,
                    "required_review_thread_resolution": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "ci/build", "integration_id": 1234}],
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": False,
                },
            },
        ],
        "bypass_actors": [],
    }


def _install_ruleset(adapter: FakeGitHubAdapter, *, detail: dict[str, object] | None = None) -> None:
    full = detail or _ruleset_full_state()
    ruleset_id = int(full["id"])
    adapter.rule_rows = [
        {
            "id": ruleset_id,
            "name": full["name"],
            "target": full["target"],
            "source_type": "Repository",
            "enforcement": full["enforcement"],
        }
    ]
    adapter.rule_details[ruleset_id] = dict(full)


def _mock_gh_api(monkeypatch: pytest.MonkeyPatch, response_for=None):
    requests: list[dict[str, object]] = []

    def fake_run(argv: list[str], *, input_data: bytes | None = None, timeout: float) -> tuple[bytes, bytes, int]:
        assert isinstance(argv, list)
        assert "--hostname" in argv
        assert timeout > 0
        method = argv[argv.index("--method") + 1]
        endpoint = argv[-1]
        body = json.loads(input_data.decode("utf-8")) if input_data is not None else None
        request = {"method": method, "endpoint": endpoint, "body": body, "argv": argv, "timeout": timeout}
        requests.append(request)
        response = response_for(request) if response_for else {}
        return json.dumps(response).encode("utf-8"), b"", 0

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", fake_run)
    return requests


def test_target_policy_and_plan_binding_are_deterministic_and_schema_valid() -> None:
    target = parse_target("GITHUB.COM.", "Owner/Repo")
    assert target.hostname == "github.com"
    policy_a = _policy(repository={"topics": ["release", "docs"], "description": "after"})
    policy_b = _policy(repository={"description": "after", "topics": ["docs", "release"]})
    assert policy_a == policy_b

    adapter = FakeGitHubAdapter()
    plan_a = create_plan(adapter, target, policy_a)
    plan_b = create_plan(adapter, target, policy_b)
    assert plan_a == plan_b
    assert plan_a["target"] == {
        "hostname": "github.com",
        "requested_full_name": "Owner/Repo",
        "repository_id": 90123,
        "observed_full_name": "Owner/Repo",
        "actor_id": 7001,
    }
    _, policy_validator, plan_validator = _schemas()
    policy_validator.validate(policy_a)
    plan_validator.validate(plan_a)
    validate_plan(plan_a)

    visibility = create_plan(adapter, target, _policy(repository={"visibility": "private"}))
    assert len(visibility["operations"]) == 1
    assert visibility["operations"][0]["kind"] == "repository_visibility"
    assert visibility["operations"][0]["risk"] == "high"
    plan_validator.validate(visibility)

    mixed_visibility = {"schema_version": 2, "repository": {"visibility": "private"}, "security": {}}
    assert not policy_validator.is_valid(mixed_visibility)
    mixed_visibility_signoff = {
        "schema_version": 2,
        "repository": {"visibility": "private", "web_commit_signoff_required": True},
    }
    assert not policy_validator.is_valid(mixed_visibility_signoff)
    with pytest.raises(PolicyError, match="only setting"):
        normalize_policy(mixed_visibility)
    with pytest.raises(PolicyError, match="internal is not supported"):
        normalize_policy({"schema_version": 2, "repository": {"visibility": "internal"}})


@pytest.mark.parametrize(("initial", "desired"), [(False, True), (True, False)])
def test_web_commit_signoff_is_a_policy_bound_repository_patch_with_readback(
    initial: bool, desired: bool, tmp_path: Path
) -> None:
    adapter = FakeGitHubAdapter()
    adapter.repo["web_commit_signoff_required"] = initial
    policy = _policy(repository={"web_commit_signoff_required": desired})
    _, policy_validator, _ = _schemas()
    policy_validator.validate(policy)

    plan = create_plan(adapter, _target(), policy)
    assert len(plan["operations"]) == 1
    operation = plan["operations"][0]
    assert operation["group"] == "collaboration"
    assert operation["kind"] == "repository_patch"
    assert operation["current"] == {"web_commit_signoff_required": initial}
    assert operation["desired"] == {"web_commit_signoff_required": desired}
    assert operation["interface"]["transport"] == "gh_api"
    assert "without an exact gh repo edit equivalent" in operation["interface"]["selection_reason"]
    validate_plan(plan)

    result = apply_plan(
        plan,
        _target(),
        adapter,
        tmp_path / "state",
        supplied_digest=plan["plan_digest"],
        operation_ids=[operation["id"]],
    )
    assert result["status"] == "complete"
    assert adapter.repo["web_commit_signoff_required"] is desired
    assert adapter.apply_calls == [operation["id"]]

    verified = verify_plan(plan, _target(), adapter)
    assert verified["status"] == "verified"
    assert verified["operations"][0]["current"] == {"web_commit_signoff_required": desired}


def test_web_commit_signoff_requires_an_observed_boolean_current_value() -> None:
    adapter = FakeGitHubAdapter()
    adapter.repo.pop("web_commit_signoff_required")
    policy = _policy(repository={"web_commit_signoff_required": False})
    plan = create_plan(adapter, _target(), policy)
    assert plan["operations"] == []
    assert any(
        item.get("scope") == "requested_policy"
        and item.get("control") == "web_commit_signoff"
        and "web_commit_signoff_required_field_not_returned" in item.get("reason", "")
        for item in plan["report_only"]
    )
    validate_plan(plan)

    with pytest.raises(PolicyError, match="repository.web_commit_signoff_required must be a boolean"):
        normalize_policy({"schema_version": 2, "repository": {"web_commit_signoff_required": "false"}})
    _, policy_validator, _ = _schemas()
    assert not policy_validator.is_valid({"schema_version": 2, "repository": {"web_commit_signoff_required": "false"}})


@pytest.mark.parametrize("fresh_state", ["missing", "already_desired"])
def test_web_commit_signoff_apply_rechecks_fresh_state_before_write(
    fresh_state: str, tmp_path: Path
) -> None:
    adapter = FakeGitHubAdapter()
    adapter.repo["web_commit_signoff_required"] = False
    plan = create_plan(adapter, _target(), _policy(repository={"web_commit_signoff_required": True}))
    if fresh_state == "missing":
        adapter.repo.pop("web_commit_signoff_required")
        with pytest.raises(ApplyError, match="current state is missing or invalid"):
            apply_plan(
                plan,
                _target(),
                adapter,
                tmp_path / "state",
                supplied_digest=plan["plan_digest"],
                operation_ids=plan["operation_ids"],
            )
    else:
        # Another actor may have completed this Boolean setting after plan
        # creation. Its only valid alternate value is the desired value, which
        # should reconcile as already correct without sending another write.
        adapter.repo["web_commit_signoff_required"] = True
        result = apply_plan(
            plan,
            _target(),
            adapter,
            tmp_path / "state",
            supplied_digest=plan["plan_digest"],
            operation_ids=plan["operation_ids"],
        )
        assert result["applied"] == [{
            "operation_id": plan["operations"][0]["id"],
            "status": "already_correct",
        }]
    assert adapter.apply_calls == []


def test_web_commit_signoff_saved_plan_rejects_current_and_desired_tampering() -> None:
    adapter = FakeGitHubAdapter()
    adapter.repo["web_commit_signoff_required"] = False
    plan = create_plan(adapter, _target(), _policy(repository={"web_commit_signoff_required": True}))

    forged_current = copy.deepcopy(plan)
    _reissue_operation(forged_current, 0, current={"web_commit_signoff_required": True})
    with pytest.raises(PlanError, match="web commit signoff current state.*saved control observation"):
        validate_plan(forged_current)

    forged_desired = copy.deepcopy(plan)
    _reissue_operation(forged_desired, 0, desired={"web_commit_signoff_required": False})
    with pytest.raises(PlanError, match="repository patch operation is not authorized by the embedded policy"):
        validate_plan(forged_desired)


def test_verification_policy_schema_plan_v4_and_v2_v3_replan_guidance() -> None:
    verification_policy = {
        "signatures": {
            "commit_oid": "a" * 40,
            "tag_name": "v1.0.0",
            "expected_primary_fingerprints": ["a" * 40],
        },
        "release": {
            "release_id": 73,
            "tag_name": "v1.0.0",
            "source_ref": "refs/tags/v1.0.0",
            "source_commit": "a" * 40,
            "signer_workflow": "Owner/Repo/.github/workflows/release.yml@refs/tags/v1.0.0",
            "predicate_type": "https://slsa.dev/provenance/v1",
            "artifacts": [{
                "asset_id": 12,
                "name": "package.tar.gz",
                "path": "dist/package.tar.gz",
                "expected_sha256": "b" * 64,
            }],
        },
    }
    policy = _policy(verification=verification_policy)
    _, policy_validator, plan_validator = _schemas()
    policy_validator.validate(policy)
    plan = create_plan(FakeGitHubAdapter(), _target(), policy)
    assert plan["schema_version"] == 4
    plan_validator.validate(plan)

    for old_version in (2, 3):
        old_plan = copy.deepcopy(plan)
        old_plan["schema_version"] = old_version
        old_plan["plan_digest"] = plan_digest(old_plan)
        with pytest.raises(PlanError, match=rf"schema v{old_version}.*rerun --mode plan.*new v4 plan"):
            validate_plan(old_plan)

    with pytest.raises(PolicyError, match="source_ref must identify its exact tag_name"):
        _policy(verification={"release": {**verification_policy["release"], "source_ref": "refs/tags/v2.0.0"}})


def test_verification_keys_are_verify_only_and_not_serialized_into_plans_or_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy(verification={
        "signatures": {
            "commit_oid": "a" * 40,
            "tag_name": "v1.0.0",
            "expected_primary_fingerprints": ["A" * 40],
        },
    })
    plan = create_plan(FakeGitHubAdapter(), _target(), policy)
    plan_text = json.dumps(plan, sort_keys=True)
    assert "trusted_public_key" not in plan_text
    assert "private-key-path" not in plan_text
    assert plan["schema_version"] == 4

    key_path = "/private/trusted-key.asc"
    key_bytes = b"fixture public key bytes"
    seen: list[tuple[object, ...]] = []

    def signatures(checkout: Path, commit_oid: str, tag_name: str, trusted_keys, fingerprints):
        seen.append((checkout, commit_oid, tag_name, trusted_keys, fingerprints))
        if not trusted_keys:
            return {"status": "incomplete", "reason": "trusted_key_input_missing"}
        return {
            "status": "verified",
            "reason": None,
            "evidence_scope": "local_git_openpgp_signatures",
            "commit_object_id": commit_oid,
            "tag_name": tag_name,
            "commit_primary_fingerprint": fingerprints[0],
            "tag_primary_fingerprint": fingerprints[0],
        }

    monkeypatch.setattr(service_module, "verify_release_signatures", signatures)
    adapter = FakeGitHubAdapter()
    missing = verify_plan(plan, _target(), adapter)
    assert missing["release_readiness"]["status"] == "incomplete"
    assert missing["requested_policy"]["status"] == "incomplete"
    assert missing["requested_policy"]["findings"][-1]["scope"] == "requested_policy"

    verified = verify_plan(plan, _target(), adapter, trusted_public_keys=[key_bytes])
    assert verified["release_readiness"]["status"] == "verified"
    assert verified["requested_policy"]["status"] == "complete"
    assert seen[-1][3] == [key_bytes]
    rendered = json.dumps(verified, sort_keys=True)
    assert key_bytes.decode("ascii") not in rendered
    assert key_path not in rendered


def test_gh_repository_parser_accepts_repeatable_trusted_keys_and_rejects_them_for_plan() -> None:
    parser = build_parser(_add_config_flags, _add_selector_flags, _add_visual_flags, _add_harness_target_flags)
    args = parser.parse_args([
        "github-repo", "--repository", "Owner/Repo", "--hostname", "github.com",
        "--mode", "verify", "--plan", "plan.json",
        "--trusted-public-key", "first.asc", "--trusted-public-key", "second.asc",
    ])
    assert args.trusted_public_key == ["first.asc", "second.asc"]
    args.mode = "plan"
    with pytest.raises(PolicyError, match="not used by --mode plan: --trusted-public-key"):
        _reject_unrelated_flags(args, allowed={"plan"})


def test_release_proof_binds_fresh_repository_tag_asset_digest_and_builder_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = "b" * 64
    release_policy = {
        "release_id": 73,
        "tag_name": "v1.0.0",
        "source_ref": "refs/tags/v1.0.0",
        "source_commit": "a" * 40,
        "signer_workflow": "Owner/Repo/.github/workflows/release.yml@refs/tags/v1.0.0",
        "predicate_type": "https://slsa.dev/provenance/v1",
        "artifacts": [{
            "asset_id": 12,
            "name": "package.tar.gz",
            "path": "dist/package.tar.gz",
            "expected_sha256": digest,
        }],
    }
    policy = _policy(verification={"release": release_policy})
    identity_calls: list[tuple[str, object]] = []
    proof_calls: list[tuple[str, object]] = []

    def local_hashes(checkout: Path, identity: dict[str, object], artifacts: list[dict[str, object]]) -> dict[str, object]:
        identity_calls.append(("local", identity))
        return {
            "status": "incomplete",
            "reason": "independent_release_proof_missing",
            "artifacts": [{
                "asset_id": 12,
                "name": "package.tar.gz",
                "relative_path": "dist/package.tar.gz",
                "status": "matched",
                "sha256": digest,
                "size_bytes": 27,
            }],
        }

    monkeypatch.setattr(service_module, "verify_release_artifacts", local_hashes)
    adapter = FakeGitHubAdapter()
    adapter.release_identity = lambda release_id: {"id": release_id, "tag_name": "v1.0.0"}
    adapter.tag_commit_sha = lambda tag_name: "a" * 40
    adapter.release_asset_identity = lambda release_id, asset_id: {
        "id": asset_id, "name": "package.tar.gz", "digest": f"sha256:{digest}",
    }

    def verify_asset(tag_name: str, file_path: str) -> dict[str, object]:
        proof_calls.append(("asset", (tag_name, file_path)))
        return {"status": "verified", "reason": None}

    def verify_attestation(file_path: str, *, signer_workflow: str, source_ref: str, predicate_type: str) -> dict[str, object]:
        proof_calls.append(("attestation", (file_path, signer_workflow, source_ref, predicate_type)))
        # Predicate contents are workflow controlled and are not receipt authority.
        return {"status": "verified", "predicate": {"claimed_identity": "untrusted"}}

    adapter.verify_release_asset = verify_asset
    adapter.verify_attestation = verify_attestation
    result = verify_plan(create_plan(adapter, _target(), policy), _target(), adapter)

    readiness = result["release_readiness"]
    assert readiness["status"] == "verified"
    assert readiness["release"]["identity"]["release_id"] == 73
    assert readiness["release"]["identity"]["source_commit"] == "a" * 40
    assert readiness["release"]["artifacts"] == [{
        "asset_id": 12,
        "name": "package.tar.gz",
        "sha256": digest,
        "status": "verified",
        "reason": None,
    }]
    assert identity_calls[0][1]["repository_id"] == 90123
    assert identity_calls[0][1]["source_ref"] == "refs/tags/v1.0.0"
    assert proof_calls[0][0] == "asset"
    assert proof_calls[0][1][0] == "v1.0.0"
    assert proof_calls[1][0] == "attestation"
    assert proof_calls[1][1][1:] == (
        "Owner/Repo/.github/workflows/release.yml@refs/tags/v1.0.0",
        "refs/tags/v1.0.0",
        "https://slsa.dev/provenance/v1",
    )
    rendered = json.dumps(result, sort_keys=True)
    assert str(Path.cwd() / "dist" / "package.tar.gz") not in rendered
    assert "claimed_identity" not in rendered


def test_release_hash_match_without_remote_signature_and_attestation_proof_stays_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = "c" * 64
    release_policy = {
        "release_id": 73,
        "tag_name": "v1.0.0",
        "source_ref": "refs/tags/v1.0.0",
        "source_commit": "a" * 40,
        "signer_workflow": "Owner/Repo/.github/workflows/release.yml@refs/tags/v1.0.0",
        "predicate_type": "https://slsa.dev/provenance/v1",
        "artifacts": [{"asset_id": 12, "name": "package.tar.gz", "path": "dist/package.tar.gz", "expected_sha256": digest}],
    }

    def local_hashes(checkout: Path, identity: dict[str, object], artifacts: list[dict[str, object]]) -> dict[str, object]:
        return {
            "status": "incomplete",
            "reason": "independent_release_proof_missing",
            "artifacts": [{"asset_id": 12, "name": "package.tar.gz", "relative_path": "dist/package.tar.gz", "status": "matched", "sha256": digest, "size_bytes": 27}],
        }

    monkeypatch.setattr(service_module, "verify_release_artifacts", local_hashes)
    adapter = FakeGitHubAdapter()
    adapter.release_identity = lambda release_id: {"id": release_id, "tag_name": "v1.0.0"}
    adapter.tag_commit_sha = lambda tag_name: "a" * 40
    adapter.release_asset_identity = lambda release_id, asset_id: {"id": asset_id, "name": "package.tar.gz", "digest": f"sha256:{digest}"}
    adapter.verify_release_asset = lambda tag_name, file_path: {"status": "verified"}
    adapter.verify_attestation = lambda *args, **kwargs: {"status": "unavailable", "reason": "gh_attestation_verify_unavailable"}

    result = verify_plan(create_plan(adapter, _target(), _policy(verification={"release": release_policy})), _target(), adapter)
    assert result["release_readiness"]["status"] == "unavailable"
    assert result["release_readiness"]["release"]["artifacts"][0]["status"] == "unavailable"
    assert result["requested_policy"]["status"] == "incomplete"


def test_gh_verification_commands_are_feature_detected_and_use_fixed_identity_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def missing(argv: list[str], *, input_data: bytes | None = None, timeout: float) -> tuple[bytes, bytes, int]:
        calls.append(argv)
        return b"", b"unknown command", 1

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", missing)
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    assert adapter.verify_release_asset("v1.0.0", "/private/artifact.tar.gz")["status"] == "unavailable"
    assert calls == [["gh-fixture", "release", "verify-asset", "--help"]]

    calls.clear()

    def generic_release_help(argv: list[str], *, input_data: bytes | None = None, timeout: float) -> tuple[bytes, bytes, int]:
        calls.append(argv)
        return b"Usage: gh release <command> [flags]\n", b"", 0

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", generic_release_help)
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    assert adapter.verify_release_asset("v1.0.0", "/private/artifact.tar.gz")["status"] == "unavailable"
    assert calls == [["gh-fixture", "release", "verify-asset", "--help"]]

    calls.clear()

    def available(argv: list[str], *, input_data: bytes | None = None, timeout: float) -> tuple[bytes, bytes, int]:
        calls.append(argv)
        if argv[-1] == "--help":
            command_help = (
                b"Usage: gh release verify-asset <tag> <file> [flags]\n"
                if "release" in argv else b"Usage: gh attestation verify <file> [flags]\n"
            )
            return command_help, b"", 0
        return b"{}", b"", 0

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", available)
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    assert adapter._verification_commands_ready() is True
    assert calls == [
        ["gh-fixture", "release", "verify-asset", "--help"],
        ["gh-fixture", "attestation", "verify", "--help"],
    ]
    calls.clear()
    assert adapter.verify_release_asset("v1.0.0", "/private/artifact.tar.gz")["status"] == "verified"
    assert adapter.verify_attestation(
        "/private/artifact.tar.gz",
        signer_workflow="Owner/Repo/.github/workflows/release.yml@refs/tags/v1.0.0",
        source_ref="refs/tags/v1.0.0",
        predicate_type="https://slsa.dev/provenance/v1",
    )["status"] == "verified"
    assert calls[0] == [
        "gh-fixture", "release", "verify-asset", "v1.0.0", "/private/artifact.tar.gz",
        "--repo", "github.com/Owner/Repo", "--format", "json",
    ]
    assert calls[1] == [
        "gh-fixture", "attestation", "verify", "/private/artifact.tar.gz",
        "--repo", "github.com/Owner/Repo",
        "--signer-workflow", "Owner/Repo/.github/workflows/release.yml@refs/tags/v1.0.0",
        "--source-ref", "refs/tags/v1.0.0",
        "--predicate-type", "https://slsa.dev/provenance/v1",
        "--format", "json",
    ]


def test_gh_process_output_is_capped_while_streaming_before_json_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "_MAX_GH_OUTPUT_BYTES", 1024)
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 700); sys.stderr.write('y' * 700)",
    ]
    with pytest.raises(adapter_module._GhProcessFailure, match="gh_output_limit_exceeded"):
        adapter_module._run_bounded_gh(command, timeout=5)


@pytest.mark.parametrize(
    ("surface", "policy", "invalidate"),
    [
        ("actions", _policy(actions={"enabled": False}), lambda adapter: adapter.actions.pop("enabled")),
        ("workflow", _policy(actions={"default_workflow_permissions": "write"}), lambda adapter: adapter.workflow.update(default_workflow_permissions=None)),
        (
            "selected",
            _policy(actions={"selected_actions": {"patterns_allowed": ["Crux/*"]}}),
            lambda adapter: (adapter.actions.update(allowed_actions="selected"), adapter.selected.update(patterns_allowed=[7])),
        ),
        (
            "pages",
            _policy(pages={"https_enforced": False}),
            lambda adapter: setattr(adapter, "pages_state", {"build_type": "workflow", "source": {"branch": "main", "path": "/docs"}}),
        ),
    ],
)
def test_requested_actions_and_pages_require_present_type_valid_current_state(
    surface: str, policy: dict[str, object], invalidate: object
) -> None:
    adapter = FakeGitHubAdapter()
    if surface == "pages":
        adapter.pages_state = {"build_type": "workflow", "source": {"branch": "main", "path": "/docs"}, "cname": None, "https_enforced": True}
    invalidate(adapter)
    plan = create_plan(adapter, _target(), policy)
    assert plan["operations"] == []
    assert any(
        item.get("scope") == "requested_policy"
        and item.get("status") == "report_only"
        and ("current_values_missing_or_invalid" in item.get("reason", "") or "current_values_missing_or_invalid" in item.get("reason", ""))
        for item in plan["report_only"]
    )
    validate_plan(plan)


def test_apply_refuses_when_fresh_actions_state_becomes_missing_or_invalid(tmp_path: Path) -> None:
    adapter = FakeGitHubAdapter()
    plan = create_plan(adapter, _target(), _policy(actions={"default_workflow_permissions": "write"}))
    assert plan["operations"][0]["kind"] == "actions_workflow_policy"
    adapter.workflow["default_workflow_permissions"] = None
    with pytest.raises(ApplyError, match="current state is missing or invalid"):
        apply_plan(
            plan,
            _target(),
            adapter,
            tmp_path / "state",
            supplied_digest=plan["plan_digest"],
            operation_ids=plan["operation_ids"],
        )
    assert adapter.apply_calls == []


def test_pages_cname_null_is_a_valid_explicit_desired_value() -> None:
    adapter = FakeGitHubAdapter()
    adapter.pages_state = {
        "build_type": "workflow",
        "source": {"branch": "main", "path": "/docs"},
        "cname": "docs.example.test",
        "https_enforced": True,
    }
    policy = _policy(pages={"cname": None})
    plan = create_plan(adapter, _target(), policy)
    operation = plan["operations"][0]
    assert operation["kind"] == "pages_update"
    assert operation["current"] == {"cname": "docs.example.test"}
    assert operation["desired"] == {"cname": None}
    _schemas()[1].validate(policy)
    validate_plan(plan)


def _action_or_pages_fixture(surface: str) -> tuple[FakeGitHubAdapter, dict[str, object], object]:
    adapter = FakeGitHubAdapter()
    if surface == "actions":
        policy = _policy(actions={"enabled": False})
        invalidate = lambda: adapter.actions.pop("enabled")
        inventory_capture, inventory_field = "actions", "enabled"
    elif surface == "workflow":
        policy = _policy(actions={"default_workflow_permissions": "write"})
        invalidate = lambda: adapter.workflow.pop("default_workflow_permissions")
        inventory_capture, inventory_field = "workflow_permissions", "default_workflow_permissions"
    elif surface == "selected":
        adapter.actions["allowed_actions"] = "selected"
        policy = _policy(actions={"selected_actions": {"patterns_allowed": ["Crux/*"]}})
        invalidate = lambda: adapter.selected.pop("patterns_allowed")
        inventory_capture, inventory_field = "selected_actions", "patterns_allowed"
    else:
        adapter.pages_state = {
            "build_type": "workflow",
            "source": {"branch": "main", "path": "/docs"},
            "cname": "docs.example.test",
            "https_enforced": True,
        }
        policy = _policy(pages={"cname": None})
        invalidate = lambda: adapter.pages_state.pop("cname")
        inventory_capture, inventory_field = "pages", "cname"
    return adapter, policy, (invalidate, inventory_capture, inventory_field)


@pytest.mark.parametrize("surface", ["actions", "workflow", "selected", "pages"])
def test_saved_plan_acceptance_rejects_missing_requested_current_values(surface: str) -> None:
    adapter, policy, metadata = _action_or_pages_fixture(surface)
    _, inventory_capture, inventory_field = metadata
    plan = create_plan(adapter, _target(), policy)
    assert plan["operations"]
    forged = copy.deepcopy(plan)
    capture = forged["inventory"]["actions_deployment"][inventory_capture]
    capture["value"].pop(inventory_field)
    forged["plan_digest"] = plan_digest(forged)
    with pytest.raises(PlanError, match="current state is missing, invalid, or inconsistent"):
        validate_plan(forged)


@pytest.mark.parametrize("surface", ["actions", "workflow", "selected", "pages"])
def test_fresh_requested_current_values_must_remain_present_before_any_write(surface: str, tmp_path: Path) -> None:
    adapter, policy, metadata = _action_or_pages_fixture(surface)
    invalidate, _, _ = metadata
    plan = create_plan(adapter, _target(), policy)
    invalidate()
    with pytest.raises(ApplyError, match="current state is missing or invalid"):
        apply_plan(
            plan,
            _target(),
            adapter,
            tmp_path / "state",
            supplied_digest=plan["plan_digest"],
            operation_ids=plan["operation_ids"],
            checkout_path=Path.cwd(),
        )
    assert adapter.apply_calls == []


def test_default_branch_change_isolated_high_risk_and_reconciles_ambiguous_write(tmp_path: Path) -> None:
    adapter = FakeGitHubAdapter()
    policy = _policy(repository={"default_branch": "develop"})
    plan = create_plan(adapter, _target(), policy, checkout_path=Path.cwd())
    operation = plan["operations"][0]
    assert len(plan["operations"]) == 1
    assert operation["kind"] == "repository_default_branch"
    assert operation["risk"] == "high"
    assert operation["current"] == {
        "default_branch": "main",
        "target_branch": "develop",
        "target_branch_sha": "b" * 40,
    }
    assert operation["desired"] == {
        "default_branch": "develop",
        "target_branch": "develop",
        "target_branch_sha": "b" * 40,
    }
    _schemas()[1].validate(policy)
    _schemas()[2].validate(plan)
    validate_plan(plan)
    with pytest.raises(PolicyError, match="default_branch must be the only setting"):
        _policy(repository={"default_branch": "develop", "description": "mixed"})
    _, policy_validator, _ = _schemas()
    assert not policy_validator.is_valid({"schema_version": 2, "repository": {"default_branch": "develop", "description": "mixed"}})
    assert not policy_validator.is_valid({"schema_version": 2, "repository": {"default_branch": "develop", "web_commit_signoff_required": True}})
    assert not policy_validator.is_valid({"schema_version": 2, "repository": {"default_branch": "../main"}})

    adapter.ambiguous_at = "after"
    result = apply_plan(
        plan,
        _target(),
        adapter,
        tmp_path / "state",
        supplied_digest=plan["plan_digest"],
        operation_ids=plan["operation_ids"],
        checkout_path=Path.cwd(),
    )
    assert result["status"] == "reconciled_after_write_error"
    assert result["applied"][0]["status"] == "reconciled_applied"
    assert adapter.repo["default_branch"] == "develop"
    assert adapter.apply_calls == [operation["id"]]

    adapter.ambiguous_at = None
    retry = apply_plan(
        plan,
        _target(),
        adapter,
        tmp_path / "state",
        supplied_digest=plan["plan_digest"],
        operation_ids=plan["operation_ids"],
        checkout_path=Path.cwd(),
    )
    assert retry["applied"][0]["status"] == "already_correct"
    assert adapter.apply_calls == [operation["id"]]


def test_default_branch_missing_or_nonexistent_remains_report_only() -> None:
    adapter = FakeGitHubAdapter()
    plan = create_plan(adapter, _target(), _policy(repository={"default_branch": "missing"}))
    assert plan["operations"] == []
    assert any(item.get("control") == "default_branch" and "existence_unavailable" in item.get("reason", "") for item in plan["report_only"])
    adapter.repo.pop("default_branch")
    absent_current = create_plan(adapter, _target(), _policy(repository={"default_branch": "develop"}))
    assert absent_current["operations"] == []
    assert any("current_default_branch_not_returned" in item.get("reason", "") for item in absent_current["report_only"])


def test_rehashed_operations_must_match_policy_and_ruleset_current_state() -> None:
    adapter = FakeGitHubAdapter()
    plan = create_plan(adapter, _target(), _policy(repository={"description": "after", "topics": ["docs"]}))
    forged = copy.deepcopy(plan)
    repo_index = next(index for index, operation in enumerate(forged["operations"]) if operation["kind"] == "repository_patch")
    _reissue_operation(forged, repo_index, desired={"description": "not-requested"})
    with pytest.raises(PlanError, match="not authorized by the embedded policy"):
        validate_plan(forged)

    overlap = copy.deepcopy(plan)
    overlap["operations"].append(copy.deepcopy(overlap["operations"][0]))
    overlap["operation_ids"].append(overlap["operations"][-1]["id"])
    overlap["plan_digest"] = plan_digest(overlap)
    with pytest.raises(PlanError, match="overlapping operation targets"):
        validate_plan(overlap)

    rules = FakeGitHubAdapter()
    detail = _ruleset_full_state()
    _install_ruleset(rules, detail=detail)
    ruleset_plan = create_plan(
        rules,
        _target(),
        _policy(rulesets=[{"name": "main-protection", "target": "branch", "include": ["refs/heads/main"], "require_signed_commits": False}]),
    )
    forged_ruleset = copy.deepcopy(ruleset_plan)
    altered = copy.deepcopy(forged_ruleset["operations"][0]["desired"])
    altered["conditions"]["ref_name"]["include"] = ["refs/heads/release"]
    _reissue_operation(forged_ruleset, 0, desired=altered)
    with pytest.raises(PlanError, match="ruleset operation payload does not match"):
        validate_plan(forged_ruleset)


def test_rehashed_action_operation_cannot_embed_missing_current_state() -> None:
    plan = create_plan(FakeGitHubAdapter(), _target(), _policy(actions={"default_workflow_permissions": "write"}))
    forged = copy.deepcopy(plan)
    workflow_index = next(index for index, operation in enumerate(forged["operations"]) if operation["kind"] == "actions_workflow_policy")
    _reissue_operation(forged, workflow_index, current={"default_workflow_permissions": None})
    with pytest.raises(PlanError, match="workflow current values are missing or invalid"):
        validate_plan(forged)


def test_gh_adapter_default_branch_uses_specific_help_and_exact_native_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = {"id": 90123, "default_branch": "main"}
    requests = _mock_gh_api(
        monkeypatch,
        response_for=lambda request: repo if request["endpoint"] == "repos/Owner/Repo" else {"sha": "b" * 40},
    )
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    operation = {
        "kind": "repository_default_branch",
        "resource_id": None,
        "current": {"default_branch": "main", "target_branch": "develop", "target_branch_sha": "b" * 40},
        "desired": {"default_branch": "develop", "target_branch": "develop", "target_branch_sha": "b" * 40},
    }
    assert adapter.operation_state(operation) == operation["current"]
    assert [(request["method"], request["endpoint"]) for request in requests] == [
        ("GET", "repos/Owner/Repo"),
        ("GET", "repos/Owner/Repo/commits/develop"),
    ]
    requests.clear()
    gh_calls: list[list[str]] = []

    def run_gh(argv: list[str], *, input_data=None, timeout: float, env=None):
        gh_calls.append(argv)
        if argv[-1] == "--help":
            return b"USAGE\n  gh repo edit [<repository>] [flags]\nFLAGS\n  --default-branch name\n", b"", 0
        assert env == {"GH_HOST": "github.com"}
        return b"", b"", 0

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", run_gh)
    adapter.apply_operation(operation)
    assert not requests
    assert gh_calls == [
        ["gh-fixture", "repo", "edit", "--help"],
        ["gh-fixture", "repo", "edit", "Owner/Repo", "--default-branch", "develop"],
    ]


def test_gh_repo_edit_generic_zero_exit_help_fails_closed_without_api_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    calls: list[list[str]] = []

    def run_gh(argv: list[str], *, input_data=None, timeout: float):
        calls.append(argv)
        return b"USAGE\n  gh repo <command> [flags]\n", b"", 0

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", run_gh)
    operation = {
        "kind": "repository_visibility",
        "resource_id": None,
        "current": "public",
        "desired": "private",
    }
    with pytest.raises(GitHubError, match="native_gh_repo_edit_command_or_required_flag_unavailable"):
        adapter.apply_operation(operation)
    assert calls == [["gh-fixture", "repo", "edit", "--help"]]


def test_gh_repo_edit_requires_each_flag_and_documented_false_syntax(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def run_gh(argv: list[str], *, input_data=None, timeout: float, env=None):
        calls.append((argv, env))
        return (
            b"USAGE\n  gh repo edit [<repository>] [flags]\n"
            b"To toggle a setting off, use the `--<flag>=false` syntax.\n"
            b"FLAGS\n  --enable-issues\n  --delete-branch-on-merge\n",
            b"",
            0,
        )

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", run_gh)
    operation = {
        "kind": "repository_patch",
        "resource_id": None,
        "current": {"has_issues": True, "delete_branch_on_merge": False},
        "desired": {"has_issues": False, "delete_branch_on_merge": True},
    }
    adapter.apply_operation(operation)
    assert calls == [
        (["gh-fixture", "repo", "edit", "--help"], None),
        (
            ["gh-fixture", "repo", "edit", "Owner/Repo", "--delete-branch-on-merge", "--enable-issues=false"],
            {"GH_HOST": "github.com"},
        ),
    ]


def test_combined_repository_patch_without_exact_native_flags_uses_rest_api(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_gh_api(monkeypatch, response_for=lambda _request: None)
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    operation = {
        "kind": "repository_patch",
        "resource_id": None,
        "current": {"description": "before", "has_downloads": True},
        "desired": {"description": "after", "has_downloads": False},
    }
    adapter.apply_operation(operation)
    assert [(row["method"], row["endpoint"], row["body"]) for row in requests] == [
        ("PATCH", "repos/Owner/Repo", {"description": "after", "has_downloads": False}),
    ]


def test_ruleset_update_planning_reads_full_local_detail_and_isolates_weakenings() -> None:
    adapter = FakeGitHubAdapter()
    detail = _ruleset_full_state()
    _install_ruleset(adapter, detail=detail)
    policy = _policy(
        rulesets=[
            {
                "name": "main-protection",
                "target": "branch",
                "include": ["refs/heads/main"],
                "require_signed_commits": False,
            }
        ]
    )
    plan = create_plan(adapter, _target(), policy)
    assert len(plan["operations"]) == 1
    operation = plan["operations"][0]
    assert operation["kind"] == "ruleset_upsert"
    assert operation["risk"] == "high"
    assert operation["resource_id"] == detail["id"]
    assert operation["current"]["bypass_actors"] == []
    assert any(rule["type"] == "required_signatures" for rule in operation["current"]["rules"])
    assert all(rule["type"] != "required_signatures" for rule in operation["desired"]["rules"])

    mixed = _policy(
        rulesets=policy["rulesets"],
        repository={"description": "after"},
    )
    mixed_plan = create_plan(adapter, _target(), mixed)
    assert mixed_plan["operations"] == []
    assert any(
        row.get("control") == "ruleset_upsert"
        and row.get("reason") == "protection_reducing_settings_require_a_single_operation_and_isolated_policy"
        for row in mixed_plan["report_only"]
    )

    forged = copy.deepcopy(plan)
    repo_operation = make_operation(
        group="identity_discovery",
        kind="repository_patch",
        resource_id=None,
        current={"description": "before"},
        desired={"description": "after"},
        required_permissions=("Administration:write",),
        verification="read repository description",
        recovery="restore description",
    ).to_dict()
    forged["operations"].append(repo_operation)
    forged["operation_ids"].append(repo_operation["id"])
    forged["plan_digest"] = plan_digest(forged)
    with pytest.raises(PlanError, match="isolated single-operation policy plan|isolated single-ruleset plan"):
        validate_plan(forged)

    forged_risk = copy.deepcopy(plan)
    forged_risk["operations"][0]["risk"] = "moderate"
    forged_risk["plan_digest"] = plan_digest(forged_risk)
    with pytest.raises(PlanError, match="high risk"):
        validate_plan(forged_risk)

    malformed_current = copy.deepcopy(plan)
    original = malformed_current["operations"][0]
    bad_current = copy.deepcopy(original["current"])
    bad_current["rules"] = 5
    malformed_operation = make_operation(
        group=original["group"],
        kind=original["kind"],
        resource_id=original["resource_id"],
        current=bad_current,
        desired=original["desired"],
        required_permissions=("Administration:write",),
        risk="high",
        verification=original["verification"],
        recovery=original["recovery"],
    ).to_dict()
    malformed_current["operations"] = [malformed_operation]
    malformed_current["operation_ids"] = [malformed_operation["id"]]
    malformed_current["plan_digest"] = plan_digest(malformed_current)
    with pytest.raises(PlanError, match="current state is malformed"):
        validate_plan(malformed_current)


def _ruleset_create_plan(adapter: FakeGitHubAdapter) -> dict[str, object]:
    return create_plan(
        adapter,
        _target(),
        _policy(rulesets=[{
            "name": "main-protection",
            "target": "branch",
            "include": ["refs/heads/main"],
            "require_signed_commits": True,
        }]),
    )


def test_ruleset_create_post_state_and_later_verify_accept_the_created_identity(tmp_path: Path) -> None:
    adapter = FakeGitHubAdapter()
    plan = _ruleset_create_plan(adapter)
    operation = plan["operations"][0]
    assert operation["kind"] == "ruleset_upsert"
    assert operation["resource_id"] is None
    assert operation["current"] is None

    applied = apply_plan(
        plan,
        _target(),
        adapter,
        tmp_path / "state",
        supplied_digest=plan["plan_digest"],
        operation_ids=[operation["id"]],
    )
    assert applied["status"] == "complete"
    assert applied["applied"] == [{"operation_id": operation["id"], "status": "applied"}]
    assert adapter.apply_calls == [operation["id"]]

    verified = verify_plan(plan, _target(), adapter)
    assert verified["status"] == "verified"
    assert verified["operations"] == [{
        "operation_id": operation["id"],
        "status": "verified",
        "current": operation["desired"],
    }]


def test_ruleset_create_stale_precondition_and_ambiguous_write_reconcile_without_replay(tmp_path: Path) -> None:
    stale_adapter = FakeGitHubAdapter()
    stale_plan = _ruleset_create_plan(stale_adapter)
    stale_operation = stale_plan["operations"][0]
    external_id = 6002
    changed_remote = {**stale_operation["desired"], "enforcement": "disabled"}
    stale_adapter.rule_details[external_id] = {"id": external_id, **changed_remote}
    stale_adapter.rule_rows = [{"id": external_id, **changed_remote, "source_type": "Repository"}]
    with pytest.raises(ApplyError, match="stale precondition"):
        apply_plan(
            stale_plan,
            _target(),
            stale_adapter,
            tmp_path / "stale-state",
            supplied_digest=stale_plan["plan_digest"],
            operation_ids=[stale_operation["id"]],
        )
    assert not stale_adapter.apply_calls

    adapter = FakeGitHubAdapter()
    adapter.ambiguous_at = "after"
    plan = _ruleset_create_plan(adapter)
    operation = plan["operations"][0]
    result = apply_plan(
        plan,
        _target(),
        adapter,
        tmp_path / "ambiguous-state",
        supplied_digest=plan["plan_digest"],
        operation_ids=[operation["id"]],
    )
    assert result["status"] == "reconciled_after_write_error"
    assert result["applied"][0]["status"] == "reconciled_applied"
    assert adapter.apply_calls == [operation["id"]]
    verified = verify_plan(plan, _target(), adapter)
    assert verified["operations"][0]["status"] == "verified"
    assert adapter.apply_calls == [operation["id"]]


@pytest.mark.parametrize(
    "weaken",
    [
        lambda ruleset: ruleset["rules"].remove(next(rule for rule in ruleset["rules"] if rule["type"] == "required_signatures")),
        lambda ruleset: ruleset["rules"].remove(next(rule for rule in ruleset["rules"] if rule["type"] == "deletion")),
        lambda ruleset: ruleset["rules"].remove(next(rule for rule in ruleset["rules"] if rule["type"] == "non_fast_forward")),
        lambda ruleset: next(rule for rule in ruleset["rules"] if rule["type"] == "pull_request")["parameters"].update(required_approving_review_count=1),
        lambda ruleset: next(rule for rule in ruleset["rules"] if rule["type"] == "pull_request")["parameters"].update(dismiss_stale_reviews_on_push=False),
        lambda ruleset: next(rule for rule in ruleset["rules"] if rule["type"] == "pull_request")["parameters"].update(require_code_owner_review=False),
        lambda ruleset: next(rule for rule in ruleset["rules"] if rule["type"] == "pull_request")["parameters"].update(require_last_push_approval=False),
        lambda ruleset: next(rule for rule in ruleset["rules"] if rule["type"] == "pull_request")["parameters"].update(required_review_thread_resolution=False),
        lambda ruleset: ruleset["rules"].remove(next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")),
        lambda ruleset: next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")["parameters"].update(strict_required_status_checks_policy=False),
        lambda ruleset: next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")["parameters"].update(do_not_enforce_on_create=True),
        lambda ruleset: ruleset.update(enforcement="disabled"),
        lambda ruleset: ruleset["conditions"]["ref_name"].update(include=["refs/heads/*"]),
    ],
    ids=[
        "signed-commits", "deletions", "force-pushes", "review-count", "stale-reviews",
        "code-owner-review", "last-push-approval", "thread-resolution", "status-checks",
        "strict-status-checks", "status-check-create-enforcement", "enforcement", "ref-scope",
    ],
)
def test_ruleset_protection_reductions_are_detected(weaken) -> None:
    current = canonical_ruleset(_ruleset_full_state())
    desired = copy.deepcopy(current)
    weaken(desired)
    assert ruleset_reduces_protection(current, desired)


def test_ruleset_strengthening_is_not_classified_as_protection_reduction() -> None:
    current = canonical_ruleset(_ruleset_full_state())
    desired = copy.deepcopy(current)
    next(rule for rule in desired["rules"] if rule["type"] == "pull_request")["parameters"]["required_approving_review_count"] = 3
    desired["rules"].append({"type": "required_linear_history"})
    assert not ruleset_reduces_protection(current, desired)


def test_unreadable_full_ruleset_detail_is_report_only_for_requested_update() -> None:
    adapter = FakeGitHubAdapter()
    _install_ruleset(adapter)
    adapter.rule_detail_error = True
    plan = create_plan(
        adapter,
        _target(),
        _policy(rulesets=[{"name": "main-protection", "target": "branch", "include": ["refs/heads/main"], "require_signed_commits": False}]),
    )
    assert plan["operations"] == []
    finding = next(row for row in plan["report_only"] if row.get("scope") == "requested_policy" and row.get("control") == "ruleset:main-protection")
    assert "local_ruleset_detail_authentication_or_permission_unavailable" in finding["reason"]
    assert "bypass actors" in finding["reason"]
    summary = plan["inventory"]["git_governance"]["rulesets"]["items"][0]
    assert summary["details_available"] is False


def test_audit_covers_seven_groups_redacts_alert_details_and_verify_tracks_requested_state() -> None:
    private_detail = "fixture-private-alert-CVE-2099-0001"
    adapter = FakeGitHubAdapter(private_alert_details=(private_detail,))
    target = _target()
    report = audit(adapter, target)
    assert set(report["groups"]) == {
        "identity_discovery",
        "collaboration",
        "git_governance",
        "actions_deployment",
        "security_supply_chain",
        "releases",
        "repository_content",
    }
    assert all(group["report_only"] for group in report["groups"].values())
    security = report["groups"]["security_supply_chain"]
    assert security["open_dependabot_alert_count"] == {"available": True, "value": 1}
    assert "private_alert_details" in {finding["control"] for finding in security["report_only"]}
    rendered = json.dumps(report, sort_keys=True)
    assert private_detail not in rendered
    assert "organization_enterprise_rulesets" in rendered
    assert "organization_actions_policy" in rendered
    assert "required_status_check_health" in rendered

    policy = _policy(repository={"description": "after"})
    plan = create_plan(adapter, target, policy)
    before = verify_plan(plan, target, adapter)
    assert before["status"] == "incomplete"
    assert before["requested_policy"]["status"] == "incomplete"
    assert before["requested_policy"]["mismatches"]
    assert set(before["inventory"]) == set(report["groups"])
    assert {row["scope"] for row in before["report_only"]} >= {"inventory", "authorization"}

    with pytest.raises(PlanError, match="different hostname or requested repository"):
        verify_plan(plan, parse_target("github.com", "Other/Repo"), adapter)


def test_security_analysis_reduction_is_one_combined_high_risk_operation() -> None:
    adapter = FakeGitHubAdapter()
    adapter.repo["security_and_analysis"].pop("private_vulnerability_reporting")
    adapter.repo["security_and_analysis"]["advanced_security"]["status"] = "enabled"
    plan = create_plan(
        adapter,
        _target(),
        _policy(security={
            "advanced_security": False,
            "code_security": True,
        }),
    )
    patches = [row for row in plan["operations"] if row["kind"] == "security_analysis_patch"]
    assert len(patches) == 1
    patch = patches[0]
    assert patch["current"] == {"advanced_security": True, "code_security": False}
    assert patch["desired"] == {"advanced_security": False, "code_security": True}
    assert patch["risk"] == "high"
    assert plan["inventory"]["security_supply_chain"]["private_vulnerability_reporting"] == {"available": True, "value": False}

    forged = copy.deepcopy(plan)
    forged_patch = next(row for row in forged["operations"] if row["kind"] == "security_analysis_patch")
    forged_patch["risk"] = "moderate"
    forged["plan_digest"] = plan_digest(forged)
    with pytest.raises(PlanError, match="disable a setting.*high risk"):
        validate_plan(forged)

    split = copy.deepcopy(plan)
    split_operations = []
    for key in sorted(patch["desired"]):
        split_operations.append(make_operation(
            group="security_supply_chain",
            kind="security_analysis_patch",
            resource_id=None,
            current={key: patch["current"][key]},
            desired={key: patch["desired"][key]},
            required_permissions=("Administration:write",),
            risk="high",
            verification="GET /repos/{owner}/{repo} security_and_analysis",
            recovery="PATCH the recorded status for each changed security feature.",
        ).to_dict())
    split["operations"] = split_operations
    split["operation_ids"] = [operation["id"] for operation in split_operations]
    split["plan_digest"] = plan_digest(split)
    with pytest.raises(PlanError, match="isolated single-operation policy plan|one combined operation"):
        validate_plan(split)


def test_actions_reductions_are_high_risk_and_cannot_be_mixed_or_rehashed_lower() -> None:
    adapter = FakeGitHubAdapter()
    plan = create_plan(adapter, _target(), _policy(actions={"default_workflow_permissions": "write"}))
    operation = plan["operations"][0]
    assert operation["kind"] == "actions_workflow_policy"
    assert operation["risk"] == "high"

    forged = copy.deepcopy(plan)
    forged["operations"][0]["risk"] = "moderate"
    forged["plan_digest"] = plan_digest(forged)
    with pytest.raises(PlanError, match="protection-reducing and identity-changing operations must be marked high risk"):
        validate_plan(forged)

    mixed = create_plan(
        FakeGitHubAdapter(),
        _target(),
        _policy(actions={"default_workflow_permissions": "write"}, repository={"description": "after"}),
    )
    assert mixed["operations"] == []
    assert any(
        row.get("reason") == "protection_reducing_settings_require_a_single_operation_and_isolated_policy"
        for row in mixed["report_only"]
    )
    assert operation_reduces_protection(
        "actions_selected_policy",
        {"patterns_allowed": ["Owner/Safe/*"]},
        {"patterns_allowed": ["*"]},
    )
    assert operation_reduces_protection("pages_create", None, {"https_enforced": False})
    assert operation_reduces_protection("dependabot_alerts_toggle", True, False)


@pytest.mark.parametrize(
    ("desired", "current", "handoff"),
    [
        (True, False, "Upload an image"),
        (False, True, "Remove image"),
    ],
)
def test_social_preview_handoff_matches_requested_action_and_keeps_verify_incomplete(
    desired: bool, current: bool, handoff: str
) -> None:
    adapter = FakeGitHubAdapter(custom_social_preview=current)
    social_policy = (
        {"action": "present", "asset_path": "assets/localsetup-logo.png"}
        if desired else {"action": "absent"}
    )
    plan = create_plan(adapter, _target(), _policy(repository_content={"social_preview": social_policy}))
    findings = [row for row in plan["report_only"] if row.get("scope") == "requested_policy"]
    result = verify_plan(plan, _target(), adapter)
    social_control = next(row for row in result["control_observations"] if row["control_id"] == "repository_content.social_preview_image_handoff")
    local_preview_valid = result["local_evidence"]["social_preview"].get("validation") == "valid"
    if desired and current and local_preview_valid:
        assert not any(row["control"] == "social_preview_image_handoff" for row in findings)
        assert result["requested_policy"]["status"] == "complete"
        assert social_control["observation"] == "observed"
        assert social_control["reason"] == "ui_handoff_cannot_verify_exact_remote_image_pixels"
    else:
        social = next(row for row in findings if row["control"] == "social_preview_image_handoff")
        assert handoff in social["handoff"]
        assert result["status"] == "incomplete"
        assert result["requested_policy"]["status"] == "incomplete"
        assert result["requested_policy"]["findings"]
        if local_preview_valid:
            assert social_control["observation"] == "observed"
            assert social_control["reason"] == "ui_handoff_cannot_verify_exact_remote_image_pixels"


def test_target_mismatched_checkout_cannot_be_used_for_apply_or_signature_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wrong-target-checkout"
    root.mkdir()
    subprocess = adapter_module.subprocess
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Target Binding Test"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "target-binding@example.invalid"], cwd=root, check=True, capture_output=True)
    (root / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/Else/Repo.git"], cwd=root, check=True, capture_output=True)
    monkeypatch.setattr(service_module, "snapshot_checkout", checkout_module.snapshot_checkout)

    adapter = FakeGitHubAdapter()
    write_plan = create_plan(adapter, _target(), _policy(repository={"description": "after"}), checkout_path=root)
    assert write_plan["local_checkout"]["supported"] is True
    assert write_plan["local_checkout"]["origin"] == {"configured": True, "matches_target": False}
    with pytest.raises(ApplyError, match="no target-bound local checkout"):
        apply_plan(
            write_plan,
            _target(),
            adapter,
            tmp_path / "private-state",
            supplied_digest=write_plan["plan_digest"],
            operation_ids=write_plan["operation_ids"],
            checkout_path=root,
        )
    assert not adapter.apply_calls

    signatures_policy = _policy(verification={"signatures": {
        "commit_oid": "a" * 40,
        "tag_name": "v1.0.0",
        "expected_primary_fingerprints": ["A" * 40],
    }})
    evidence_plan = create_plan(adapter, _target(), signatures_policy, checkout_path=root)
    monkeypatch.setattr(
        service_module,
        "verify_release_signatures",
        lambda *args, **kwargs: pytest.fail("unbound checkout reached local signature verifier"),
    )
    result = verify_plan(evidence_plan, _target(), adapter, checkout_path=root, trusted_public_keys=[b"key bytes"])
    assert result["release_readiness"]["status"] == "incomplete"
    assert result["release_readiness"]["reason"] == "checkout_changed_or_target_binding_unestablished"


def test_v4_operation_interface_is_derived_bound_and_rendered() -> None:
    from ls.core.github_repo.service import _plan_markdown

    plan = create_plan(
        FakeGitHubAdapter(),
        _target(),
        _policy(repository={"features": {"issues": False}, "merge": {"delete_branch_on_merge": True}}),
    )
    operation = plan["operations"][0]
    interface = operation["interface"]
    assert interface["transport"] == "gh_repo_edit"
    assert interface["command"] == "gh repo edit"
    assert interface["target_binding"] == "plan.target"
    assert interface["required_flags"] == ["--delete-branch-on-merge", "--enable-issues"]
    assert interface["selection_reason"]
    assert "--enable-issues=false" in interface["argv_template"]
    markdown = _plan_markdown(plan)
    assert "Exact interface" in markdown
    assert "gh repo edit" in markdown
    assert "plan.target" in markdown

    forged = copy.deepcopy(plan)
    forged["operations"][0]["interface"]["transport"] = "gh_api"
    forged["plan_digest"] = plan_digest(forged)
    with pytest.raises(PlanError, match="interface does not match its canonical"):
        validate_plan(forged)

    rest_plan = create_plan(FakeGitHubAdapter(), _target(), _policy(repository={"features": {"downloads": False}}))
    rest_operation = rest_plan["operations"][0]
    assert rest_operation["interface"]["transport"] == "gh_api"
    assert rest_operation["interface"]["method"] == "PATCH"
    assert rest_operation["interface"]["endpoint_template"] == "repos/{owner}/{repo}"
    assert "without an exact gh repo edit equivalent" in rest_operation["interface"]["selection_reason"]


def test_apply_requires_exact_digest_allows_an_authorized_subset_and_is_idempotent(tmp_path: Path) -> None:
    target = _target()
    adapter = FakeGitHubAdapter()
    plan = create_plan(
        adapter,
        target,
        _policy(repository={"description": "after", "topics": ["docs", "release"]}),
    )
    ids = plan["operation_ids"]
    assert len(ids) == 2
    state_root = tmp_path / "private-state"

    with pytest.raises(PlanError, match="digest does not match"):
        apply_plan(plan, target, adapter, state_root, supplied_digest="0" * 64, operation_ids=[ids[0]])
    with pytest.raises(PlanError, match="exactly match"):
        apply_plan(plan, target, adapter, state_root, supplied_digest=plan["plan_digest"], operation_ids=["f" * 24])
    with pytest.raises(PlanError, match="unique"):
        apply_plan(plan, target, adapter, state_root, supplied_digest=plan["plan_digest"], operation_ids=[ids[0], ids[0]])
    assert not adapter.apply_calls

    first = apply_plan(plan, target, adapter, state_root, supplied_digest=plan["plan_digest"], operation_ids=[ids[0]])
    assert first["status"] == "complete"
    assert first["applied"][0]["status"] == "applied"
    assert first["settings_apply_completion"]["status"] == "complete"
    assert first["release_readiness"]["status"] == "not_assessed"
    assert adapter.apply_calls == [ids[0]]

    repeated = apply_plan(plan, target, adapter, state_root, supplied_digest=plan["plan_digest"], operation_ids=[ids[0]])
    assert repeated["applied"][0]["status"] == "already_correct"
    assert adapter.apply_calls == [ids[0]]

    rest = apply_plan(plan, target, adapter, state_root, supplied_digest=plan["plan_digest"], operation_ids=ids)
    assert {row["status"] for row in rest["applied"]} == {"already_correct", "applied"}
    assert len(adapter.apply_calls) == 2
    verified = verify_plan(plan, target, adapter)
    assert verified["status"] == "verified"
    assert verified["settings"]["status"] == "verified"
    assert verified["release_readiness"]["status"] == "not_assessed"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("actor", "authenticated actor changed"),
        ("repository_id", "immutable repository ID changed"),
        ("admin", "administrator access"),
        ("scope", "classic token scopes"),
        ("precondition", "stale precondition"),
    ],
)
def test_apply_refreshes_actor_repository_permissions_and_current_precondition(
    tmp_path: Path, change: str, message: str
) -> None:
    adapter = FakeGitHubAdapter()
    target = _target()
    plan = create_plan(adapter, target, _policy(repository={"description": "after"}))
    if change == "actor":
        adapter.actor_id += 1
    elif change == "repository_id":
        adapter.repo["id"] += 1
    elif change == "admin":
        adapter.repo["permissions"]["admin"] = False
    elif change == "scope":
        adapter.auth["scope_visibility"] = "reported"
        adapter.auth["scopes"] = []
    elif change == "precondition":
        adapter.repo["description"] = "changed independently"
    with pytest.raises(ApplyError, match=message):
        apply_plan(
            plan,
            target,
            adapter,
            tmp_path / "state",
            supplied_digest=plan["plan_digest"],
            operation_ids=plan["operation_ids"],
        )
    assert not adapter.apply_calls


def test_ghes_requested_write_is_report_only_until_api_version_support_is_verified() -> None:
    target = _target("ghe.example.test")
    plan = create_plan(FakeGitHubAdapter(hostname=target.hostname), target, _policy(repository={"description": "after"}))
    assert plan["operations"] == []
    requested_findings = [row for row in plan["report_only"] if row.get("scope") == "requested_policy"]
    assert any("github_enterprise_endpoint_and_2026_03_10_api_version_compatibility_is_unverified" in row["reason"] for row in requested_findings)
    with pytest.raises(PlanError, match="enabled only for GitHub.com"):
        forged = dict(plan)
        operation = make_operation(
            group="identity_discovery",
            kind="repository_patch",
            resource_id=None,
            current={"description": "before"},
            desired={"description": "after"},
            required_permissions=("Administration:write",),
            verification="read current repository field",
            recovery="restore recorded field",
        ).to_dict()
        forged["operations"] = [operation]
        forged["operation_ids"] = [operation["id"]]
        forged["plan_digest"] = plan_digest(forged)
        validate_plan(forged)


def test_pending_write_is_fsynced_before_call_and_ambiguous_response_is_reconciled_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ls.core.github_repo.state as state_module

    target = _target()
    adapter = FakeGitHubAdapter()
    plan = create_plan(adapter, target, _policy(repository={"description": "after"}))
    state_root = tmp_path / "private-state"
    state_directory = operation_state_directory(state_root, target.hostname, plan["target"]["repository_id"])
    real_fsync = os.fsync
    fsynced_files: set[tuple[int, int]] = set()

    def record_fsync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        fsynced_files.add((info.st_ino, info.st_size))
        real_fsync(descriptor)

    monkeypatch.setattr(state_module.os, "fsync", record_fsync)

    def assert_pending_row(operation: dict[str, object]) -> None:
        journal = state_directory / "operations.jsonl"
        journal_info = journal.stat()
        assert (journal_info.st_ino, journal_info.st_size) in fsynced_files
        rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["state"] == "pending"
        assert rows[-1]["operation_id"] == operation["id"]

    adapter.before_apply = assert_pending_row
    adapter.ambiguous_at = "after"
    result = apply_plan(
        plan,
        target,
        adapter,
        state_root,
        supplied_digest=plan["plan_digest"],
        operation_ids=plan["operation_ids"],
    )
    assert result["status"] == "reconciled_after_write_error"
    assert result["applied"][0]["status"] == "reconciled_applied"
    assert adapter.apply_calls == plan["operation_ids"]
    retry = apply_plan(
        plan,
        target,
        adapter,
        state_root,
        supplied_digest=plan["plan_digest"],
        operation_ids=plan["operation_ids"],
    )
    assert retry["applied"][0]["status"] == "already_correct"
    assert adapter.apply_calls == plan["operation_ids"]


def test_target_lock_refuses_a_second_application(tmp_path: Path) -> None:
    adapter = FakeGitHubAdapter()
    target = _target()
    plan = create_plan(adapter, target, _policy(repository={"description": "after"}))
    state_root = tmp_path / "private-state"
    directory = operation_state_directory(state_root, target.hostname, plan["target"]["repository_id"])
    with target_operation_lock(directory):
        with pytest.raises(TargetOperationBusy):
            apply_plan(
                plan,
                target,
                adapter,
                state_root,
                supplied_digest=plan["plan_digest"],
                operation_ids=plan["operation_ids"],
            )
    assert not adapter.apply_calls


def test_policy_and_plan_readers_reject_oversize_duplicate_key_and_symlink_inputs(tmp_path: Path) -> None:
    valid_policy = tmp_path / "valid-policy.json"
    valid_policy.write_text('{"schema_version":2}', encoding="utf-8")
    assert read_policy(valid_policy) == {"schema_version": 2}

    duplicate_policy = tmp_path / "duplicate-policy.json"
    duplicate_policy.write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")
    with pytest.raises(PolicyError):
        read_policy(duplicate_policy)
    oversized_policy = tmp_path / "oversized-policy.json"
    oversized_policy.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(PolicyError, match="size limit"):
        read_policy(oversized_policy)
    policy_link = tmp_path / "policy-link.json"
    policy_link.symlink_to(valid_policy)
    with pytest.raises(PolicyError):
        read_policy(policy_link)

    plan = create_plan(FakeGitHubAdapter(), _target(), _policy(repository={"description": "after"}))
    output = tmp_path / "secure-output"
    plan_json, _ = write_plan_pair(plan, output)
    assert stat.S_IMODE(plan_json.stat().st_mode) == 0o600
    plan_link = tmp_path / "plan-link.json"
    plan_link.symlink_to(plan_json)
    with pytest.raises(PlanError):
        read_plan(plan_link)
    duplicate_plan = output / "duplicate-plan.json"
    duplicate_plan.write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")
    duplicate_plan.chmod(0o600)
    with pytest.raises(PlanError):
        read_plan(duplicate_plan)
    oversized_plan = output / "oversized-plan.json"
    with oversized_plan.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    oversized_plan.chmod(0o600)
    with pytest.raises(PlanError, match="size limit"):
        read_plan(oversized_plan)


def test_private_output_and_target_state_fail_closed_on_symlink_or_unsafe_modes(tmp_path: Path) -> None:
    plan = create_plan(FakeGitHubAdapter(), _target(), _policy(repository={"description": "after"}))
    real_output = tmp_path / "real-output"
    real_output.mkdir(mode=0o700)
    output_link = tmp_path / "output-link"
    output_link.symlink_to(real_output, target_is_directory=True)
    with pytest.raises(JournalError):
        write_plan_pair(plan, output_link)

    unsafe_output = tmp_path / "unsafe-output"
    unsafe_output.mkdir(mode=0o700)
    unsafe_output.chmod(0o755)
    with pytest.raises(JournalError):
        write_plan_pair(plan, unsafe_output)

    unsafe_state = tmp_path / "unsafe-state"
    unsafe_state.mkdir(mode=0o700)
    unsafe_state.chmod(0o775)
    with pytest.raises(JournalError, match="group/world-writable parent"):
        operation_state_directory(unsafe_state, "github.com", 90123)
    assert stat.S_IMODE(unsafe_state.stat().st_mode) == 0o775

    private_state = tmp_path / "private-state"
    private_state.mkdir(mode=0o700)
    state_link = tmp_path / "state-link"
    state_link.symlink_to(private_state, target_is_directory=True)
    with pytest.raises(JournalError):
        operation_state_directory(state_link, "github.com", 90123)


def test_journal_duplicate_keys_bounds_and_hardlinked_lock_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ls.core.github_repo.state as state_module

    target = _target()
    plan = create_plan(FakeGitHubAdapter(), target, _policy(repository={"description": "after"}))
    operation = plan["operations"][0]
    directory = operation_state_directory(tmp_path / "private-state", target.hostname, plan["target"]["repository_id"])
    with target_operation_lock(directory) as guard:
        append_journal(
            directory,
            {
                "operation_id": operation["id"],
                "plan_digest": plan["plan_digest"],
                "state": "pending",
                "operation": operation,
            },
            guard,
        )
        lock_copy = tmp_path / "lock-copy"
        os.link(directory / "operation.lock", lock_copy)
        with pytest.raises(JournalError, match="single-link"):
            # The active guard protects journal access; independently opening
            # the same target detects the hard-linked lock identity violation.
            with target_operation_lock(directory):
                pass
        lock_copy.unlink()

        journal = directory / "operations.jsonl"
        journal.write_text('{"operation_id":"000000000000000000000000","operation_id":"000000000000000000000000","plan_digest":"' + "0" * 64 + '","state":"pending","operation":{"id":"000000000000000000000000"}}\n', encoding="utf-8")
        journal.chmod(0o600)
        with pytest.raises(JournalError, match="malformed"):
            read_journal(directory, guard)

        journal.write_text("x" * 100, encoding="utf-8")
        monkeypatch.setattr(state_module, "_MAX_JOURNAL_BYTES", 50)
        with pytest.raises(JournalError, match="size limit"):
            read_journal(directory, guard)


@pytest.mark.parametrize(
    ("operation", "method", "endpoint", "expected_body"),
    [
        (
            {
                "kind": "ruleset_upsert",
                "resource_id": 321,
                "desired": {
                    "name": "main-protection",
                    "target": "branch",
                    "enforcement": "active",
                    "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
                    "rules": [{"type": "required_signatures"}],
                    "bypass_actors": [],
                },
            },
            "PUT",
            "repos/Owner/Repo/rulesets/321",
            {
                "name": "main-protection",
                "target": "branch",
                "enforcement": "active",
                "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
                "rules": [{"type": "required_signatures"}],
                "bypass_actors": [],
            },
        ),
        (
            {"kind": "actions_workflow_policy", "desired": {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}},
            "PUT",
            "repos/Owner/Repo/actions/permissions/workflow",
            {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
        ),
        (
            {"kind": "actions_selected_policy", "desired": {"github_owned_allowed": True, "verified_allowed": False, "patterns_allowed": ["Crux/*"]}},
            "PUT",
            "repos/Owner/Repo/actions/permissions/selected-actions",
            {"github_owned_allowed": True, "verified_allowed": False, "patterns_allowed": ["Crux/*"]},
        ),
        (
            {"kind": "pages_create", "desired": {"build_type": "workflow", "source": {"branch": "main", "path": "/docs"}}},
            "POST",
            "repos/Owner/Repo/pages",
            {"build_type": "workflow", "source": {"branch": "main", "path": "/docs"}},
        ),
        (
            {"kind": "pages_update", "desired": {"cname": "docs.example.test", "https_enforced": True}},
            "PUT",
            "repos/Owner/Repo/pages",
            {"cname": "docs.example.test", "https_enforced": True},
        ),
        (
            {"kind": "security_analysis_patch", "desired": {"advanced_security": False, "secret_scanning": True}},
            "PATCH",
            "repos/Owner/Repo",
            {"security_and_analysis": {"advanced_security": {"status": "disabled"}, "secret_scanning": {"status": "enabled"}}},
        ),
        ({"kind": "dependabot_alerts_toggle", "desired": False}, "DELETE", "repos/Owner/Repo/vulnerability-alerts", None),
        ({"kind": "automated_security_fixes_toggle", "desired": True}, "PUT", "repos/Owner/Repo/automated-security-fixes", None),
        ({"kind": "private_vulnerability_reporting_toggle", "desired": True}, "PUT", "repos/Owner/Repo/private-vulnerability-reporting", None),
        ({"kind": "private_vulnerability_reporting_toggle", "desired": False}, "DELETE", "repos/Owner/Repo/private-vulnerability-reporting", None),
        ({"kind": "immutable_releases_toggle", "desired": False}, "DELETE", "repos/Owner/Repo/immutable-releases", None),
    ],
)
def test_gh_cli_adapter_uses_fixed_routes_and_documented_payload_shapes(
    monkeypatch: pytest.MonkeyPatch,
    operation: dict[str, object],
    method: str,
    endpoint: str,
    expected_body: object,
) -> None:
    requests = _mock_gh_api(monkeypatch)
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    adapter.apply_operation(operation)
    assert len(requests) == 1
    request = requests[0]
    assert request["method"] == method
    assert request["endpoint"] == endpoint
    assert request["body"] == expected_body
    assert "X-GitHub-Api-Version: 2026-03-10" in request["argv"]


def test_gh_cli_actions_repository_policy_preserves_unmodified_observed_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {"enabled": True, "allowed_actions": "all", "sha_pinning_required": False}
    requests = _mock_gh_api(
        monkeypatch,
        response_for=lambda request: observed if request["method"] == "GET" else {},
    )
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    adapter.apply_operation({"kind": "actions_repository_policy", "desired": {"allowed_actions": "local_only"}})
    assert [(request["method"], request["endpoint"]) for request in requests] == [
        ("GET", "repos/Owner/Repo/actions/permissions"),
        ("PUT", "repos/Owner/Repo/actions/permissions"),
    ]
    assert requests[1]["body"] == {"enabled": True, "allowed_actions": "local_only", "sha_pinning_required": False}


def test_gh_cli_actions_policy_refuses_incomplete_preservation_state_before_put(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_gh_api(
        monkeypatch,
        response_for=lambda request: {"allowed_actions": "all", "sha_pinning_required": False} if request["method"] == "GET" else {},
    )
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    with pytest.raises(GitHubError, match="actions_permissions_state_before_write"):
        adapter.apply_operation({"kind": "actions_repository_policy", "desired": {"allowed_actions": "local_only"}})
    assert [(request["method"], request["endpoint"]) for request in requests] == [
        ("GET", "repos/Owner/Repo/actions/permissions"),
    ]


def test_gh_cli_private_vulnerability_reporting_reads_dedicated_endpoint_for_inventory_and_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _mock_gh_api(monkeypatch, response_for=lambda request: {"enabled": True})
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")

    assert adapter.private_vulnerability_reporting_enabled() is True
    assert adapter.operation_state({"kind": "private_vulnerability_reporting_toggle", "desired": True}) is True
    assert [(request["method"], request["endpoint"]) for request in requests] == [
        ("GET", "repos/Owner/Repo/private-vulnerability-reporting"),
        ("GET", "repos/Owner/Repo/private-vulnerability-reporting"),
    ]


@pytest.mark.parametrize("status", [403, 404, 422])
def test_private_vulnerability_reporting_status_errors_remain_unavailable(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def denied(argv: list[str], *, input_data: bytes | None = None, timeout: float) -> tuple[bytes, bytes, int]:
        assert argv[-1] == "repos/Owner/Repo/private-vulnerability-reporting"
        return b"", f"gh: HTTP {status}".encode("ascii"), 1

    monkeypatch.setattr(adapter_module, "_run_bounded_gh", denied)
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    with pytest.raises(GitHubError) as caught:
        adapter.private_vulnerability_reporting_enabled()
    assert caught.value.status == status


def test_unavailable_private_vulnerability_reporting_status_stays_report_only() -> None:
    adapter = FakeGitHubAdapter()
    adapter._private_reporting_error = 404
    plan = create_plan(adapter, _target(), _policy(security={"private_vulnerability_reporting": True}))
    assert not any(row["kind"] == "private_vulnerability_reporting_toggle" for row in plan["operations"])
    finding = next(row for row in plan["report_only"] if row.get("control") == "private_vulnerability_reporting")
    assert finding["reason"] == "private_vulnerability_reporting_status_unavailable:endpoint_or_feature_not_available"
    assert plan["inventory"]["security_supply_chain"]["private_vulnerability_reporting"] == {
        "available": False,
        "reason": "endpoint_or_feature_not_available",
        "http_status": 404,
    }


def test_gh_cli_ruleset_update_and_create_readback_fetch_full_detail_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    detail = _ruleset_full_state(7100)
    listed = {"id": 7100, "name": detail["name"], "target": detail["target"], "source_type": "Repository"}

    def response(request: dict[str, object]) -> object:
        endpoint = request["endpoint"]
        if request["method"] == "POST" and endpoint == "repos/Owner/Repo/rulesets":
            return {"id": 7100}
        if endpoint == "repos/Owner/Repo/rulesets/7100":
            return detail
        if endpoint == "repos/Owner/Repo/rulesets?per_page=100&page=1&includes_parents=true":
            return [listed]
        return {}

    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    requests = _mock_gh_api(monkeypatch, response_for=response)
    current = adapter.operation_state({"kind": "ruleset_upsert", "resource_id": 7100, "desired": {"name": "main-protection"}})
    assert current == canonical_ruleset(detail)
    assert [(request["method"], request["endpoint"]) for request in requests] == [
        ("GET", "repos/Owner/Repo/rulesets/7100"),
    ]

    requests.clear()
    created_state = adapter.operation_state({"kind": "ruleset_upsert", "resource_id": None, "desired": {"name": "main-protection", "target": "branch"}})
    assert created_state == canonical_ruleset(detail)
    assert [(request["method"], request["endpoint"]) for request in requests] == [
        ("GET", "repos/Owner/Repo/rulesets?per_page=100&page=1&includes_parents=true"),
        ("GET", "repos/Owner/Repo/rulesets/7100"),
    ]

    requests.clear()
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    requests = _mock_gh_api(monkeypatch, response_for=response)
    create_operation = {
        "kind": "ruleset_upsert",
        "resource_id": None,
        "desired": {
            "name": detail["name"],
            "target": detail["target"],
            "enforcement": detail["enforcement"],
            "conditions": detail["conditions"],
            "rules": detail["rules"],
            "bypass_actors": detail["bypass_actors"],
        },
    }
    adapter.apply_operation(create_operation)
    created_state = adapter.operation_state(create_operation)
    assert created_state == canonical_ruleset(detail)
    assert [(request["method"], request["endpoint"]) for request in requests] == [
        ("POST", "repos/Owner/Repo/rulesets"),
        ("GET", "repos/Owner/Repo/rulesets/7100"),
    ]
    assert requests[0]["body"] == create_operation["desired"]


def test_gh_cli_ruleset_create_reports_unreadable_detail_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_gh_api(
        monkeypatch,
        response_for=lambda request: {"id": 7100} if request["method"] == "POST" else {"id": 7100},
    )
    adapter = GhCliAdapter(_target(), gh_executable="gh-fixture")
    operation = {
        "kind": "ruleset_upsert",
        "resource_id": None,
        "desired": {
            "name": "main-protection",
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": [],
            "bypass_actors": [],
        },
    }
    adapter.apply_operation(operation)
    with pytest.raises(GitHubError) as caught:
        adapter.operation_state(operation)
    assert caught.value.operation == "ruleset_detail_unavailable_after_create"
    assert [request["endpoint"] for request in requests] == [
        "repos/Owner/Repo/rulesets",
        "repos/Owner/Repo/rulesets/7100",
    ]
