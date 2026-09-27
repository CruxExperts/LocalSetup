from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from .adapter import GitHubAdapter, GitHubError
from .checkout import checkout_is_bound_to_target, snapshot_checkout
from .controls import CONTROL_GROUPS, validate_control_observations
from .inventory import build_snapshot
from .local_evidence import collect_local_evidence
from .model import RepositoryTarget, canonical_ruleset, make_operation, plan_digest
from .interfaces import describe_operation_interface
from .planning import (
    _ruleset_payload,
    build_plan,
    operation_reduces_protection,
    protection_reduction_policy_is_isolated,
    ruleset_policy_is_isolated,
    ruleset_reduces_protection,
)
from .policy import normalize_policy, policy_digest
from .state import append_journal, latest_operation_records, open_private_directory, operation_state_directory, read_journal, target_operation_lock
from .verification import load_trusted_public_keys, verify_release_artifacts, verify_release_signatures


_PLAN_KEYS = {
    "schema_version", "plan_type", "target", "authorization", "policy_digest", "policy",
    "inventory", "operations", "operation_ids", "report_only", "local_checkout",
    "local_evidence", "control_observations", "audit_coverage", "plan_digest",
}
_PERMISSIONS = {
    "repository_visibility": ("Administration:write",),
    "repository_default_branch": ("Administration:write",),
    "repository_patch": ("Administration:write",),
    "topics_replace": ("Administration:write",),
    "ruleset_upsert": ("Administration:write",),
    "actions_repository_policy": ("Administration:write",),
    "actions_workflow_policy": ("Administration:write",),
    "actions_selected_policy": ("Administration:write",),
    "pages_create": ("Pages:write", "Administration:write"),
    "pages_update": ("Pages:write", "Administration:write"),
    "security_analysis_patch": ("Administration:write",),
    "dependabot_alerts_toggle": ("Administration:write",),
    "automated_security_fixes_toggle": ("Administration:write",),
    "private_vulnerability_reporting_toggle": ("Administration:write",),
    "immutable_releases_toggle": ("Administration:write",),
}
_BOOLEAN_OPERATION_KINDS = {
    "dependabot_alerts_toggle", "automated_security_fixes_toggle",
    "private_vulnerability_reporting_toggle", "immutable_releases_toggle",
}
_REPO_PATCH_KEYS = {
    "description", "homepage", "has_issues", "has_projects", "has_wiki", "has_downloads",
    "allow_squash_merge", "allow_merge_commit", "allow_rebase_merge", "allow_auto_merge",
    "delete_branch_on_merge", "squash_merge_commit_title", "squash_merge_commit_message",
    "web_commit_signoff_required",
}
_SECURITY_KEYS = {
    "advanced_security", "code_security", "secret_scanning", "secret_scanning_push_protection",
    "secret_scanning_non_provider_patterns", "secret_scanning_ai_detection",
}
_RULE_TYPES = {"required_linear_history", "required_signatures", "deletion", "non_fast_forward", "pull_request", "required_status_checks"}


class PlanError(ValueError):
    pass


class ApplyError(RuntimeError):
    pass


_LOCAL_EVIDENCE_CONTROLS = {
    "repository_content.readme_presentation": "readme_presentation",
    "repository_content.status_badges": "status_badges",
    "repository_content.installation_route": "installation_route",
    "repository_content.support_route": "support_route",
    "repository_content.contribution_route": "contribution_route",
    "repository_content.security_reporting_route": "security_reporting_route",
    "repository_content.changelog_version_alignment": "changelog_version_signals",
    "security_supply_chain.grouped_dependency_updates": "dependabot_configuration",
    "security_supply_chain.security_policy_route": "security_reporting_route",
    "identity_discovery.license_and_community_files": "community_files",
    "repository_content.pages_site_metadata": "pages_site_metadata",
    "repository_content.open_graph_metadata": "open_graph_metadata_inputs",
    "repository_content.accessibility_inputs": "accessibility_inputs",
    "repository_content.footer_attribution": "footer_attribution_inputs",
}


_NOT_CONTENT_QUALITY_CONTROLS = {
    "repository_content.changelog_version_alignment": "version_alignment_not_evaluated",
    "security_supply_chain.grouped_dependency_updates": "dependency_group_configuration_not_interpreted",
    "repository_content.pages_site_metadata": "site_metadata_values_not_interpreted",
    "repository_content.open_graph_metadata": "open_graph_values_not_interpreted",
    "repository_content.accessibility_inputs": "accessibility_quality_not_evaluated",
    "repository_content.footer_attribution": "attribution_policy_not_evaluated",
}


def _local_control_row(
    control_id: str,
    *,
    observation: str,
    capability: str,
    value: Any = None,
    reason: str | None = None,
    applicability: str = "applicable",
    authority: str = "local",
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    group = control_id.partition(".")[0]
    row: dict[str, Any] = {
        "control_id": control_id,
        "group": group,
        "applicability": applicability,
        "authority": authority,
        "capability": capability,
        "observation": observation,
    }
    if value is not None:
        row["value"] = value
    if reason is not None:
        row["reason"] = reason
    if evidence:
        row["evidence"] = evidence
    return row


def _local_control_observations(local_evidence: dict[str, Any], checkout: dict[str, Any]) -> list[dict[str, Any]]:
    records = local_evidence.get("controls") if isinstance(local_evidence.get("controls"), dict) else {}
    rows: list[dict[str, Any]] = []
    for control_id, evidence_key in _LOCAL_EVIDENCE_CONTROLS.items():
        record = records.get(evidence_key)
        if not isinstance(record, dict):
            rows.append(_local_control_row(control_id, observation="unavailable", capability="local-workflow", reason="local_evidence_not_returned"))
            continue
        status = record.get("status")
        if not local_evidence.get("supported"):
            observation = "unavailable"
            reason = local_evidence.get("reason") or "local_evidence_unavailable"
        elif status == "unavailable":
            observation = "unavailable"
            reason = record.get("reason") or "local_file_evidence_unavailable"
        elif status == "partially_observed":
            observation = "incomplete"
            reason = record.get("reason") or "some_local_file_evidence_unavailable"
        else:
            observation = "observed"
            reason = _NOT_CONTENT_QUALITY_CONTROLS.get(control_id)
        rows.append(_local_control_row(
            control_id,
            observation=observation,
            capability="local-workflow",
            value={
                "structural_evidence": record.get("value"),
                "quality_review": record.get("quality_review", "not_assessed"),
            },
            reason=reason,
            evidence=record.get("evidence") if isinstance(record.get("evidence"), list) else None,
        ))

    community = records.get("community_files") if isinstance(records.get("community_files"), dict) else {}
    community_paths = community.get("value", {}).get("tracked_paths", []) if isinstance(community.get("value"), dict) else []
    if not isinstance(community_paths, list):
        community_paths = []
    path_groups = {
        "collaboration.issue_forms": [path for path in community_paths if isinstance(path, str) and ("ISSUE_TEMPLATE" in path or path.endswith("ISSUE_TEMPLATE.md"))],
        "collaboration.pull_request_templates": [path for path in community_paths if isinstance(path, str) and "PULL_REQUEST_TEMPLATE" in path],
        "collaboration.funding_links": [path for path in community_paths if isinstance(path, str) and path.endswith("FUNDING.yml")],
    }
    for control_id, paths in path_groups.items():
        rows.append(_local_control_row(
            control_id,
            observation="observed" if local_evidence.get("supported") else "unavailable",
            capability="local-workflow",
            value={"tracked_paths": sorted(paths)},
            reason=None if local_evidence.get("supported") else local_evidence.get("reason", "local_evidence_unavailable"),
        ))

    signature_status = checkout.get("signature_evidence") if isinstance(checkout, dict) else None
    if isinstance(signature_status, dict):
        observation = signature_status.get("observation", "unknown")
        signature_reason = signature_status.get("reason")
        signature_value = signature_status.get("value")
    else:
        observation = "unknown" if checkout.get("supported") else "unavailable"
        signature_reason = "signature_evidence_not_collected" if checkout.get("supported") else checkout.get("reason", "checkout_unavailable")
        signature_value = None
    rows.append(_local_control_row(
        "git_governance.signed_commit_and_tag_evidence",
        observation=observation,
        capability="local-workflow",
        value=signature_value,
        reason=signature_reason,
    ))

    rows.append(_local_control_row(
        "releases.signed_release_checks",
        observation="unknown",
        capability="local-workflow",
        reason="release_artifact_signature_and_attestation_checks_require_a_selected_artifact_and_trusted_builder_policy",
    ))
    installation = records.get("installation_route") if isinstance(records.get("installation_route"), dict) else {}
    installation_value = installation.get("value") if isinstance(installation.get("value"), dict) else {}
    rows.append(_local_control_row(
        "releases.reproducible_install_evidence",
        observation="unknown",
        capability="local-workflow",
        value={"installation_route_present": bool(installation_value.get("present"))},
        reason="installation_reproducibility_requires_project_specific_execution_evidence",
    ))

    preview = local_evidence.get("social_preview") if isinstance(local_evidence.get("social_preview"), dict) else {}
    if preview.get("validation") == "valid":
        preview_observation = "observed"
        preview_reason = "ui_handoff_cannot_verify_exact_remote_image_pixels"
    elif preview.get("validation") == "not_requested":
        preview_observation = "unknown"
        preview_reason = "no_social_preview_action_or_asset_was_selected"
    elif preview.get("validation") == "invalid":
        preview_observation = "incomplete"
        preview_reason = preview.get("reason") or "social_preview_asset_validation_failed"
    else:
        preview_observation = "unavailable"
        preview_reason = preview.get("reason") or "social_preview_evidence_unavailable"
    rows.append(_local_control_row(
        "repository_content.social_preview_image_handoff",
        observation=preview_observation,
        capability="ui-handoff",
        authority="ui",
        value={key: preview.get(key) for key in ("action", "validation", "handoff", "evidence_scope", "relative_path", "sha256", "size_bytes", "format") if key in preview},
        reason=preview_reason,
        evidence=[preview["relative_path"]] if isinstance(preview.get("relative_path"), str) else None,
    ))
    return rows


def _local_context(checkout_path: Path, target: RepositoryTarget, policy: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    local_checkout = snapshot_checkout(checkout_path, target)
    preview = (policy or {}).get("repository_content", {}).get("social_preview", {})
    action = preview.get("action") if isinstance(preview, dict) else None
    asset_path = preview.get("asset_path") if isinstance(preview, dict) else None
    if local_checkout.get("supported") is True and not checkout_is_bound_to_target(local_checkout):
        local_evidence = {
            "supported": False,
            "reason": "checkout_target_binding_unestablished",
            "controls": {},
            "file_hashes": {},
            "social_preview": {
                "action": action,
                "validation": "unavailable",
                "reason": "checkout_target_binding_unestablished",
            },
        }
    else:
        local_evidence = collect_local_evidence(
            checkout_path,
            social_preview_action=action,
            social_preview_asset=asset_path,
        )
    if (
        local_checkout.get("supported") is True
        and local_evidence.get("supported") is True
        and local_checkout.get("root_binding") != local_evidence.get("root_binding")
    ):
        local_checkout = {"supported": False, "reason": "checkout_root_changed_during_collection"}
        local_evidence = {
            "supported": False,
            "reason": "checkout_root_changed_during_collection",
            "controls": {},
            "file_hashes": {},
            "social_preview": {"action": action, "validation": "unavailable", "reason": "checkout_root_changed_during_collection"},
        }
    return local_checkout, local_evidence, _local_control_observations(local_evidence, local_checkout)


def _combine_controls(remote_rows: Any, local_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(remote_rows, list):
        remote_rows = []
    rows = sorted(
        [*remote_rows, *local_rows],
        key=lambda row: (
            str(row.get("control_id", "")) if isinstance(row, dict) else "",
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
    coverage = validate_control_observations(rows)
    return rows, coverage


def _snapshot_with_local_context(
    adapter: GitHubAdapter,
    target: RepositoryTarget,
    checkout_path: Path,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = build_snapshot(adapter, target)
    requested_branch = (policy or {}).get("repository", {}).get("default_branch")
    if isinstance(requested_branch, str):
        try:
            branch_head = adapter.branch_head_sha(requested_branch)
            if not isinstance(branch_head, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", branch_head):
                raise GitHubError(None, operation="requested_default_branch_head")
            snapshot["requested_default_branch"] = {
                "branch": requested_branch,
                "available": True,
                "sha": branch_head.lower(),
            }
        except GitHubError as exc:
            snapshot["requested_default_branch"] = {
                "branch": requested_branch,
                "available": False,
                "reason": exc.code,
                "http_status": exc.status,
            }
    local_checkout, local_evidence, local_rows = _local_context(checkout_path, target, policy)
    rows, coverage = _combine_controls(snapshot.get("control_observations"), local_rows)
    snapshot["local_checkout"] = local_checkout
    snapshot["local_evidence"] = local_evidence
    snapshot["control_observations"] = rows
    snapshot["audit_coverage"] = coverage
    return snapshot


def _local_state_matches(plan: dict[str, Any], checkout_path: Path, target: RepositoryTarget) -> bool:
    checkout, evidence, _ = _local_context(checkout_path, target, plan["policy"])
    return checkout_is_bound_to_target(checkout) and checkout == plan["local_checkout"] and evidence == plan["local_evidence"]


def validate_operation(operation: Any) -> None:
    if not isinstance(operation, dict):
        raise PlanError("plan operation must be an object")
    required = {"id", "group", "kind", "resource_id", "current", "desired", "interface", "preconditions", "required_permissions", "risk", "verification", "recovery"}
    if set(operation) != required:
        raise PlanError("plan operation fields do not match the supported typed operation schema")
    kind = operation.get("kind")
    if not isinstance(kind, str) or kind not in _PERMISSIONS:
        raise PlanError("plan contains an unsupported operation kind")
    group = operation.get("group")
    group_by_kind = {
        "repository_visibility": {"identity_discovery"},
        "repository_default_branch": {"identity_discovery"},
        "repository_patch": {"identity_discovery", "collaboration"}, "topics_replace": {"identity_discovery"},
        "ruleset_upsert": {"git_governance"}, "actions_repository_policy": {"actions_deployment"},
        "actions_workflow_policy": {"actions_deployment"}, "actions_selected_policy": {"actions_deployment"},
        "pages_create": {"actions_deployment"}, "pages_update": {"actions_deployment"},
        "security_analysis_patch": {"security_supply_chain"}, "dependabot_alerts_toggle": {"security_supply_chain"},
        "automated_security_fixes_toggle": {"security_supply_chain"}, "private_vulnerability_reporting_toggle": {"security_supply_chain"},
        "immutable_releases_toggle": {"releases"},
    }
    if group not in group_by_kind[kind]:
        raise PlanError("operation kind is assigned to an unsupported plan group")
    if tuple(operation.get("required_permissions", [])) != _PERMISSIONS[kind]:
        raise PlanError("plan operation permission declaration does not match its typed operation")
    if not isinstance(operation.get("risk"), str) or operation.get("risk") not in {"moderate", "high"}:
        raise PlanError("operation risk must be moderate or high")
    if not isinstance(operation.get("verification"), str) or not isinstance(operation.get("recovery"), str):
        raise PlanError("operation verification and recovery must be text")
    preconditions = operation.get("preconditions")
    if not isinstance(preconditions, dict) or set(preconditions) != {"binding", "current"} or preconditions.get("binding") != "plan.target" or preconditions.get("current") != operation.get("current"):
        raise PlanError("operation preconditions do not bind to the plan target and current state")
    resource_id = operation.get("resource_id")
    if resource_id is not None and (not isinstance(resource_id, int) or isinstance(resource_id, bool) or resource_id <= 0):
        raise PlanError("operation resource_id must be a positive integer or null")

    current, desired = operation.get("current"), operation.get("desired")
    if kind == "repository_visibility":
        if not isinstance(current, str) or current not in {"public", "private"} or not isinstance(desired, str) or desired not in {"public", "private"}:
            raise PlanError("visibility operations support only documented public or private values")
        if operation.get("risk") != "high":
            raise PlanError("repository visibility operations must be marked high risk")
    elif kind == "repository_default_branch":
        fields = {"default_branch", "target_branch", "target_branch_sha"}
        if (
            resource_id is not None
            or not isinstance(current, dict) or set(current) != fields
            or not isinstance(desired, dict) or set(desired) != fields
            or any(not isinstance(value.get("default_branch"), str) or not value["default_branch"] for value in (current, desired))
            or current.get("target_branch") != desired.get("target_branch")
            or desired.get("target_branch") != desired.get("default_branch")
            or not isinstance(current.get("target_branch_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", current["target_branch_sha"])
            or desired.get("target_branch_sha") != current.get("target_branch_sha")
        ):
            raise PlanError("default branch operation requires a valid existing target branch and recorded current pointer")
        if operation.get("risk") != "high":
            raise PlanError("default branch operations must be marked high risk")
    elif kind == "repository_patch":
        if not isinstance(desired, dict) or not desired or set(desired) - _REPO_PATCH_KEYS or not isinstance(current, dict) or set(current) != set(desired):
            raise PlanError("repository patch contains unsupported fields")
        for key, value in desired.items():
            if not _operation_field_valid(kind, key, value) or not _operation_field_valid(kind, key, current[key]):
                raise PlanError("repository patch current and desired values must use supported types")
    elif kind == "topics_replace":
        if not isinstance(desired, list) or not isinstance(current, list) or any(not isinstance(item, str) for item in desired + current) or len(set(desired)) != len(desired) or len(set(current)) != len(current):
            raise PlanError("topic operation must contain string arrays")
    elif kind == "ruleset_upsert":
        _validate_ruleset_operation(current, desired, resource_id)
        if ruleset_reduces_protection(current, desired) and operation.get("risk") != "high":
            raise PlanError("protection-reducing ruleset operations must be marked high risk")
    elif kind == "actions_repository_policy":
        allowed = {"enabled", "allowed_actions", "sha_pinning_required"}
        if not isinstance(current, dict) or not isinstance(desired, dict) or not desired or set(desired) - allowed or set(current) != set(desired):
            raise PlanError("Actions repository policy contains unsupported fields")
        if "enabled" in desired and not isinstance(desired["enabled"], bool):
            raise PlanError("Actions enabled must be a boolean")
        if "allowed_actions" in desired and (not isinstance(desired["allowed_actions"], str) or desired["allowed_actions"] not in {"all", "local_only", "selected"}):
            raise PlanError("allowed_actions is unsupported")
        if "sha_pinning_required" in desired and not isinstance(desired["sha_pinning_required"], bool):
            raise PlanError("sha_pinning_required must be a boolean")
        if any(not _operation_field_valid(kind, key, value) for mapping in (current, desired) for key, value in mapping.items()):
            raise PlanError("Actions repository current values are missing or invalid")
    elif kind == "actions_workflow_policy":
        allowed = {"default_workflow_permissions", "can_approve_pull_request_reviews"}
        if not isinstance(current, dict) or not isinstance(desired, dict) or not desired or set(desired) - allowed or set(current) != set(desired):
            raise PlanError("workflow permissions contain unsupported fields")
        if "default_workflow_permissions" in desired and (not isinstance(desired["default_workflow_permissions"], str) or desired["default_workflow_permissions"] not in {"read", "write"}):
            raise PlanError("workflow permission value is unsupported")
        if "can_approve_pull_request_reviews" in desired and not isinstance(desired["can_approve_pull_request_reviews"], bool):
            raise PlanError("workflow PR approval must be a boolean")
        if any(not _operation_field_valid(kind, key, value) for mapping in (current, desired) for key, value in mapping.items()):
            raise PlanError("workflow current values are missing or invalid")
    elif kind == "actions_selected_policy":
        allowed = {"github_owned_allowed", "verified_allowed", "patterns_allowed"}
        if not isinstance(current, dict) or not isinstance(desired, dict) or not desired or set(desired) - allowed or set(current) != set(desired):
            raise PlanError("selected Actions policy contains unsupported fields")
        for key, value in desired.items():
            if not _operation_field_valid(kind, key, value) or not _operation_field_valid(kind, key, current[key]):
                raise PlanError("selected Actions current and desired values are missing or invalid")
    elif kind in {"pages_create", "pages_update"}:
        allowed = {"build_type", "source", "cname", "https_enforced"}
        if not isinstance(desired, dict) or not desired or set(desired) - allowed:
            raise PlanError("Pages operation contains unsupported fields")
        if kind == "pages_create" and current is not None:
            raise PlanError("Pages create operation must have a null current value")
        if kind == "pages_update" and (not isinstance(current, dict) or set(current) != set(desired)):
            raise PlanError("Pages update precondition does not match its requested fields")
        if "build_type" in desired and (not isinstance(desired["build_type"], str) or desired["build_type"] not in {"legacy", "workflow"}):
            raise PlanError("Pages build type is unsupported")
        if "source" in desired:
            source = desired["source"]
            if not isinstance(source, dict) or set(source) != {"branch", "path"} or not isinstance(source.get("branch"), str) or not isinstance(source.get("path"), str) or source.get("path") not in {"/", "/docs"}:
                raise PlanError("Pages source is malformed")
        if any(not _operation_field_valid(kind, key, value) for mapping in (current or {}, desired) for key, value in mapping.items()):
            raise PlanError("Pages current and desired values are missing or invalid")
        if "https_enforced" in desired and not isinstance(desired["https_enforced"], bool):
            raise PlanError("Pages https_enforced must be a boolean")
    elif kind == "security_analysis_patch":
        if not isinstance(current, dict) or not isinstance(desired, dict) or not desired or set(desired) - _SECURITY_KEYS or set(current) != set(desired):
            raise PlanError("security analysis operation contains unsupported fields")
        if any(not isinstance(value, bool) for value in desired.values()):
            raise PlanError("security analysis statuses must be booleans")
        if any(value is False for value in desired.values()) and operation.get("risk") != "high":
            raise PlanError("security analysis operations that disable a setting must be marked high risk")
    elif kind in _BOOLEAN_OPERATION_KINDS:
        if not isinstance(current, bool) or not isinstance(desired, bool):
            raise PlanError("toggle operations require boolean current and desired states")

    if (
        operation_reduces_protection(kind, current, desired)
        or kind in {"repository_visibility", "repository_default_branch"}
    ) and operation.get("risk") != "high":
        raise PlanError("protection-reducing and identity-changing operations must be marked high risk")

    expected = make_operation(
        group=group,
        kind=kind,
        resource_id=resource_id,
        current=current,
        desired=desired,
        required_permissions=_PERMISSIONS[kind],
        risk=operation["risk"],
        verification=operation["verification"],
        recovery=operation["recovery"],
    ).operation_id
    try:
        expected_interface = describe_operation_interface(kind, current, desired, resource_id)
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError("operation interface cannot be derived from its typed payload") from exc
    if operation.get("interface") != expected_interface:
        raise PlanError("operation interface does not match its canonical typed command or API contract")
    if operation.get("id") != expected:
        raise PlanError("operation ID does not match its typed payload")


def _inventory_capture_value(plan: dict[str, Any], key: str) -> tuple[bool, Any]:
    inventory = plan.get("inventory")
    group = inventory.get("actions_deployment", {}) if isinstance(inventory, dict) else {}
    capture = group.get(key) if isinstance(group, dict) else None
    if not isinstance(capture, dict) or capture.get("available") is not True:
        return False, None
    return True, capture.get("value")


def _saved_control_observation(plan: dict[str, Any], control_id: str) -> dict[str, Any] | None:
    rows = plan.get("control_observations")
    if not isinstance(rows, list):
        return None
    matches = [
        row for row in rows
        if isinstance(row, dict) and row.get("control_id") == control_id
    ]
    return matches[0] if len(matches) == 1 else None


def _operation_policy_binding(plan: dict[str, Any], operation: dict[str, Any]) -> None:
    policy = plan["policy"]
    kind = operation["kind"]
    desired = operation["desired"]
    repository = policy.get("repository", {})

    def require_expected(expected: dict[str, Any], description: str) -> None:
        if not isinstance(desired, dict) or not desired or any(key not in expected or expected[key] != value for key, value in desired.items()):
            raise PlanError(f"{description} operation is not authorized by the embedded policy")

    if kind == "repository_visibility":
        if repository.get("visibility") != desired:
            raise PlanError("visibility operation is not authorized by the embedded policy")
    elif kind == "repository_default_branch":
        if (
            set(policy) != {"schema_version", "repository"}
            or set(repository) != {"default_branch"}
            or desired.get("default_branch") != repository.get("default_branch")
        ):
            raise PlanError("default branch operation requires an isolated matching policy")
        identity_group = plan["inventory"].get("identity_discovery")
        identity_repo = identity_group.get("repository") if isinstance(identity_group, dict) else None
        stored_branch = identity_repo.get("default_branch") if isinstance(identity_repo, dict) else None
        if operation["current"].get("default_branch") != stored_branch:
            raise PlanError("default branch precondition does not match the saved repository inventory")
    elif kind == "repository_patch":
        expected: dict[str, Any] = {}
        if operation["group"] == "identity_discovery":
            expected.update({key: repository[key] for key in ("description", "homepage") if key in repository})
        elif operation["group"] == "collaboration":
            features = repository.get("features", {})
            expected.update({f"has_{key}": value for key, value in features.items()})
            expected.update(repository.get("merge", {}))
            if "web_commit_signoff_required" in repository:
                expected["web_commit_signoff_required"] = repository["web_commit_signoff_required"]
        require_expected(expected, "repository patch")
        if "web_commit_signoff_required" in desired:
            observation = _saved_control_observation(plan, "collaboration.web_commit_signoff")
            if (
                not isinstance(observation, dict)
                or observation.get("observation") != "observed"
                or not isinstance(observation.get("value"), bool)
                or observation["value"] != operation["current"].get("web_commit_signoff_required")
            ):
                raise PlanError("web commit signoff current state is missing, invalid, or inconsistent with saved control observation")
    elif kind == "topics_replace":
        if repository.get("topics") != desired:
            raise PlanError("topics operation is not authorized by the embedded policy")
    elif kind == "ruleset_upsert":
        matches = [
            item for item in policy.get("rulesets", [])
            if item.get("name") == desired.get("name") and item.get("target") == desired.get("target")
        ]
        if len(matches) != 1 or _ruleset_payload(matches[0], operation["current"]) != desired:
            raise PlanError("ruleset operation payload does not match the embedded policy and current state")
        governance_group = plan["inventory"].get("git_governance")
        ruleset_inventory = governance_group.get("rulesets", {}) if isinstance(governance_group, dict) else {}
        items = ruleset_inventory.get("items", []) if isinstance(ruleset_inventory, dict) else []
        if not isinstance(items, list):
            raise PlanError("saved ruleset inventory is malformed")
        conflicts = [item for item in items if isinstance(item, dict) and item.get("target") == desired["target"] and item.get("name") == desired["name"]]
        if operation["resource_id"] is None:
            if any(item.get("source_type") == "Repository" for item in conflicts) or conflicts:
                raise PlanError("ruleset create operation conflicts with saved local or inherited inventory")
        else:
            local = next((item for item in conflicts if item.get("source_type") == "Repository" and item.get("id") == operation["resource_id"]), None)
            if local is None:
                raise PlanError("ruleset update resource ID does not match saved local inventory")
            current = operation["current"]
            if (
                local.get("enforcement") != current.get("enforcement")
                or canonical_ruleset({"conditions": local.get("conditions")}).get("conditions") != current.get("conditions")
                or local.get("rule_count") != len(current.get("rules", []))
                or local.get("bypass_actor_count") != len(current.get("bypass_actors", []))
            ):
                raise PlanError("ruleset current state does not match the saved local inventory summary")
    elif kind == "actions_repository_policy":
        requested = {key: policy.get("actions", {})[key] for key in desired if key in policy.get("actions", {})}
        require_expected(requested, "Actions repository policy")
        available, state = _inventory_capture_value(plan, "actions")
        if not available or not isinstance(state, dict) or any(key not in state or state[key] != operation["current"][key] or not _operation_field_valid(kind, key, state[key]) for key in desired):
            raise PlanError("Actions repository operation current state is missing, invalid, or inconsistent with saved inventory")
    elif kind == "actions_workflow_policy":
        requested = {key: policy.get("actions", {})[key] for key in desired if key in policy.get("actions", {})}
        require_expected(requested, "workflow permissions")
        available, state = _inventory_capture_value(plan, "workflow_permissions")
        if not available or not isinstance(state, dict) or any(key not in state or state[key] != operation["current"][key] or not _operation_field_valid(kind, key, state[key]) for key in desired):
            raise PlanError("workflow operation current state is missing, invalid, or inconsistent with saved inventory")
    elif kind == "actions_selected_policy":
        requested = policy.get("actions", {}).get("selected_actions", {})
        require_expected(requested, "selected Actions")
        available, state = _inventory_capture_value(plan, "selected_actions")
        if not available or not isinstance(state, dict) or any(key not in state or state[key] != operation["current"][key] or not _operation_field_valid(kind, key, state[key]) for key in desired):
            raise PlanError("selected Actions current state is missing, invalid, or inconsistent with saved inventory")
    elif kind in {"pages_create", "pages_update"}:
        requested = policy.get("pages", {})
        require_expected({key: requested[key] for key in desired if key in requested}, "Pages")
        if kind == "pages_create" and requested.get("enabled") is not True:
            raise PlanError("Pages creation requires enabled=true in the embedded policy")
        available, state = _inventory_capture_value(plan, "pages")
        if not available or (kind == "pages_create" and state is not None):
            raise PlanError("Pages operation has no valid saved current state")
        if kind == "pages_update":
            if not isinstance(state, dict) or any(key not in state or state[key] != operation["current"][key] or not _operation_field_valid(kind, key, state[key]) for key in desired):
                raise PlanError("Pages current state is missing, invalid, or inconsistent with saved inventory")
    elif kind == "security_analysis_patch":
        names = {
            "advanced_security": "advanced_security", "code_security": "code_security",
            "secret_scanning": "secret_scanning", "secret_scanning_push_protection": "secret_scanning_push_protection",
            "secret_scanning_non_provider_patterns": "secret_scanning_non_provider_patterns",
            "secret_scanning_ai_detection": "secret_scanning_ai_detection",
        }
        expected = {names[name]: value for name, value in policy.get("security", {}).items() if name in names}
        require_expected(expected, "security analysis")
    else:
        policy_names = {
            "dependabot_alerts_toggle": "dependabot_alerts",
            "automated_security_fixes_toggle": "automated_security_fixes",
            "private_vulnerability_reporting_toggle": "private_vulnerability_reporting",
            "immutable_releases_toggle": "immutable",
        }
        section = "releases" if kind == "immutable_releases_toggle" else "security"
        if policy.get(section, {}).get(policy_names.get(kind)) != desired:
            raise PlanError("toggle operation is not authorized by the embedded policy")


def _operation_targets(operation: dict[str, Any]) -> set[tuple[str, str]]:
    kind = operation["kind"]
    desired = operation["desired"]
    if kind in {"repository_patch", "actions_repository_policy", "actions_workflow_policy", "actions_selected_policy", "security_analysis_patch"}:
        endpoint = {
            "repository_patch": f"{operation['group']}:repository",
            "actions_repository_policy": "actions:repository_permissions",
            "actions_workflow_policy": "actions:workflow_permissions",
            "actions_selected_policy": "actions:selected_permissions",
            "security_analysis_patch": "security:analysis",
        }[kind]
        return {(endpoint, key) for key in desired}
    if kind == "ruleset_upsert":
        return {("ruleset", f"{desired['target']}:{desired['name'].casefold()}")}
    if kind in {"pages_create", "pages_update"}:
        return {("pages", "site")}
    if kind == "topics_replace":
        return {("repository", "topics")}
    if kind == "repository_visibility":
        return {("repository", "visibility")}
    if kind == "repository_default_branch":
        return {("repository", "default_branch")}
    return {(kind, "state")}


def _validate_remote_operation_state(operation: dict[str, Any], state: Any) -> None:
    kind = operation["kind"]
    desired = operation["desired"]
    valid = True
    if kind == "repository_visibility":
        valid = isinstance(state, str) and state in {"public", "private"}
    elif kind == "repository_default_branch":
        valid = (
            isinstance(state, dict) and set(state) == {"default_branch", "target_branch", "target_branch_sha"}
            and isinstance(state.get("default_branch"), str) and bool(state["default_branch"])
            and isinstance(state.get("target_branch"), str) and state.get("target_branch") == desired.get("default_branch")
            and isinstance(state.get("target_branch_sha"), str) and re.fullmatch(r"[0-9a-f]{40,64}", state["target_branch_sha"]) is not None
        )
    elif kind == "repository_patch":
        valid = isinstance(state, dict) and set(state) == set(desired) and all(_operation_field_valid(kind, key, value) for key, value in state.items())
    elif kind == "topics_replace":
        valid = isinstance(state, list) and all(isinstance(item, str) for item in state) and len(set(state)) == len(state)
    elif kind == "ruleset_upsert":
        fields = {"name", "target", "enforcement", "conditions", "rules", "bypass_actors"}
        valid = state is None if operation["resource_id"] is None else False
        if isinstance(state, dict) and set(state) == fields:
            valid = (
                state.get("name") == desired.get("name")
                and state.get("target") == desired.get("target")
                and isinstance(state.get("enforcement"), str)
                and isinstance(state.get("conditions"), dict)
                and isinstance(state.get("rules"), list)
                and isinstance(state.get("bypass_actors"), list)
            )
    elif kind in {"actions_repository_policy", "actions_workflow_policy", "actions_selected_policy", "pages_update", "security_analysis_patch"}:
        valid = isinstance(state, dict) and set(state) == set(desired) and all(_operation_field_valid(kind, key, value) for key, value in state.items())
    elif kind == "pages_create":
        valid = state is None or (isinstance(state, dict) and set(state) == set(desired) and all(_operation_field_valid(kind, key, value) for key, value in state.items()))
    elif kind in _BOOLEAN_OPERATION_KINDS:
        valid = isinstance(state, bool)
    if not valid:
        raise GitHubError(None, operation="typed_operation_state_missing_or_invalid")


def _validate_ruleset_operation(current: Any, desired: Any, resource_id: Any) -> None:
    fields = {"name", "target", "enforcement", "conditions", "rules", "bypass_actors"}
    if not isinstance(desired, dict) or set(desired) != fields or not isinstance(desired.get("target"), str) or desired.get("target") not in {"branch", "tag"} or not isinstance(desired.get("enforcement"), str) or desired.get("enforcement") not in {"active", "disabled"}:
        raise PlanError("ruleset operation contains an unsupported payload")
    if resource_id is None and current is not None:
        raise PlanError("ruleset create operation must have a null current value")
    if resource_id is not None and (not isinstance(current, dict) or set(current) != fields):
        raise PlanError("ruleset update precondition is malformed")
    if current is not None:
        if (
            current.get("target") not in {"branch", "tag"}
            or current.get("enforcement") not in {"active", "disabled"}
            or not isinstance(current.get("name"), str)
            or not isinstance(current.get("conditions"), dict)
            or not isinstance(current.get("rules"), list)
            or not isinstance(current.get("bypass_actors"), list)
            or any(not isinstance(row, dict) or not isinstance(row.get("type"), str) for row in current.get("rules", []))
        ):
            raise PlanError("ruleset update current state is malformed")
        current_types = [row["type"] for row in current["rules"]]
        if len(set(current_types)) != len(current_types):
            raise PlanError("ruleset current rule types must be unique")
    if not isinstance(desired.get("name"), str) or not desired["name"] or len(desired["name"]) > 100:
        raise PlanError("ruleset name is malformed")
    if current is not None and (current.get("name") != desired["name"] or current.get("target") != desired["target"]):
        raise PlanError("ruleset update may not rename or retarget an existing ruleset")
    conditions = desired.get("conditions")
    if not isinstance(conditions, dict) or set(conditions) - {"ref_name"}:
        raise PlanError("ruleset conditions contain unsupported fields")
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict) or set(ref_name) - {"include", "exclude"}:
        raise PlanError("ruleset ref conditions are malformed")
    for key in ("include", "exclude"):
        if not isinstance(ref_name.get(key, []), list) or any(not isinstance(item, str) for item in ref_name.get(key, [])):
            raise PlanError("ruleset ref patterns must be string arrays")
    if not ref_name.get("include"):
        raise PlanError("ruleset must include at least one ref pattern")
    rules = desired.get("rules")
    if not isinstance(rules, list) or any(not isinstance(row, dict) or not isinstance(row.get("type"), str) for row in rules):
        raise PlanError("ruleset rules must be typed objects")
    rule_types = [row["type"] for row in rules]
    if len(set(rule_types)) != len(rule_types):
        raise PlanError("ruleset rule types must be unique")
    old_rules = {row.get("type"): row for row in (current or {}).get("rules", []) if isinstance(row, dict)}
    for row in rules:
        rule_type = row["type"]
        if rule_type not in _RULE_TYPES and old_rules.get(rule_type) != row:
            raise PlanError("operation may preserve but cannot add or change an unsupported ruleset rule")
        if rule_type in {"required_linear_history", "required_signatures", "deletion", "non_fast_forward"}:
            if set(row) - {"type", "parameters"} or row.get("parameters", {}) not in ({}, None):
                raise PlanError("boolean ruleset rule has unsupported parameters")
        elif rule_type == "pull_request":
            parameters = row.get("parameters")
            pull_keys = {"required_approving_review_count", "dismiss_stale_reviews_on_push", "require_code_owner_review", "require_last_push_approval", "required_review_thread_resolution"}
            if not isinstance(parameters, dict) or set(parameters) != pull_keys:
                raise PlanError("pull request rules must include the documented complete parameter set")
            count = parameters["required_approving_review_count"]
            if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 6:
                raise PlanError("pull request approval count must be from 0 to 6")
            if any(not isinstance(parameters[key], bool) for key in pull_keys - {"required_approving_review_count"}):
                raise PlanError("pull request rule parameters must use booleans")
        elif rule_type == "required_status_checks":
            parameters = row.get("parameters")
            check_keys = {"required_status_checks", "strict_required_status_checks_policy", "do_not_enforce_on_create"}
            if not isinstance(parameters, dict) or set(parameters) - check_keys or not {"required_status_checks", "strict_required_status_checks_policy"} <= set(parameters):
                raise PlanError("required status check rule parameters are malformed")
            checks = parameters["required_status_checks"]
            if not isinstance(checks, list) or not checks:
                raise PlanError("required status check rule must include checks")
            if any(not isinstance(item, dict) or set(item) - {"context", "integration_id"} or not isinstance(item.get("context"), str) or not item["context"] for item in checks):
                raise PlanError("required status check entries are malformed")
            for item in checks:
                if "integration_id" in item and (not isinstance(item["integration_id"], int) or isinstance(item["integration_id"], bool) or item["integration_id"] <= 0):
                    raise PlanError("required status check integration IDs must be positive integers")
            if not isinstance(parameters["strict_required_status_checks_policy"], bool):
                raise PlanError("strict required status check setting must be a boolean")
            if "do_not_enforce_on_create" in parameters and not isinstance(parameters["do_not_enforce_on_create"], bool):
                raise PlanError("status check create behavior must be a boolean")
        elif old_rules.get(rule_type) != row:
            raise PlanError("operation may only preserve unknown ruleset rule types unchanged")
    if not isinstance(desired.get("bypass_actors"), list):
        raise PlanError("ruleset bypass actors must be an array")
    if resource_id is None and desired["bypass_actors"]:
        raise PlanError("ruleset creation cannot add bypass actors")
    if current is not None and current.get("bypass_actors") != desired.get("bypass_actors"):
        raise PlanError("ruleset operation may not change bypass actors")


def validate_plan(value: Any) -> dict[str, Any]:
    old_version = value.get("schema_version") if isinstance(value, dict) else None
    if old_version == 2 or old_version == 3:
        version = old_version
        raise PlanError(f"saved plan schema v{version} is no longer supported; rerun --mode plan and review the new v4 plan")
    if not isinstance(value, dict) or set(value) != _PLAN_KEYS:
        raise PlanError("plan JSON fields do not match the supported plan schema")
    if value.get("schema_version") != 4 or value.get("plan_type") != "localsetup.github-repository-plan":
        raise PlanError("unsupported GitHub repository plan schema; rerun --mode plan and review the new v4 plan")
    policy = normalize_policy(value.get("policy"))
    if policy != value.get("policy") or policy_digest(policy) != value.get("policy_digest"):
        raise PlanError("embedded policy is not canonical or its digest does not match")
    target = value.get("target")
    if not isinstance(target, dict) or set(target) != {"hostname", "requested_full_name", "repository_id", "observed_full_name", "actor_id"}:
        raise PlanError("plan target binding is malformed")
    if not isinstance(target.get("repository_id"), int) or isinstance(target.get("repository_id"), bool) or target["repository_id"] <= 0:
        raise PlanError("plan repository ID must be a positive integer")
    if not isinstance(target.get("actor_id"), int) or isinstance(target.get("actor_id"), bool) or target["actor_id"] <= 0:
        raise PlanError("plan actor ID must be a positive integer")
    if not isinstance(value.get("operations"), list) or not isinstance(value.get("operation_ids"), list):
        raise PlanError("plan operations must be arrays")
    inventory = value.get("inventory")
    expected_groups = set(CONTROL_GROUPS)
    if not isinstance(inventory, dict) or set(inventory) != expected_groups:
        raise PlanError("plan inventory must cover all seven repository groups")
    if value["operations"] and target.get("hostname") != "github.com":
        raise PlanError("remote write operations are enabled only for GitHub.com until the GitHub Enterprise API version matrix is verified")
    operation_ids = []
    target_owners: dict[tuple[str, str], str] = {}
    for operation in value["operations"]:
        validate_operation(operation)
        operation_ids.append(operation["id"])
        for target_key in _operation_targets(operation):
            if target_key in target_owners:
                raise PlanError("plan contains duplicate or overlapping operation targets")
            target_owners[target_key] = operation["id"]
    if operation_ids != value["operation_ids"] or len(set(operation_ids)) != len(operation_ids):
        raise PlanError("plan operation ID index does not match its operations")
    reducing_operations = [
        operation
        for operation in value["operations"]
        if operation_reduces_protection(operation["kind"], operation["current"], operation["desired"])
    ]
    if reducing_operations and (
        len(value["operations"]) != 1
        or len(reducing_operations) != 1
        or not protection_reduction_policy_is_isolated(policy, reducing_operations[0])
    ):
        raise PlanError("protection-reducing changes require an isolated single-operation policy plan")
    security_operations = [operation for operation in value["operations"] if operation["kind"] == "security_analysis_patch"]
    if len(security_operations) > 1:
        raise PlanError("security analysis settings must use one combined operation")
    if any(
        key in _SECURITY_KEYS and enabled is False
        for key, enabled in policy.get("security", {}).items()
    ) and any(
        operation["kind"] == "security_analysis_patch" and operation["risk"] != "high"
        for operation in value["operations"]
    ):
        raise PlanError("security analysis operations for a policy disabling any setting must be marked high risk")
    visibility = policy.get("repository", {}).get("visibility")
    visibility_operations = [operation for operation in value["operations"] if operation["kind"] == "repository_visibility"]
    if visibility_operations and (
        visibility is None
        or value["target"]["hostname"] != "github.com"
        or len(value["operations"]) != 1
        or visibility_operations[0]["desired"] != visibility
    ):
        raise PlanError("visibility operations require an isolated public/private policy plan")
    default_branch_operations = [operation for operation in value["operations"] if operation["kind"] == "repository_default_branch"]
    if default_branch_operations and (
        len(value["operations"]) != 1
        or len(default_branch_operations) != 1
        or set(policy) != {"schema_version", "repository"}
        or set(policy.get("repository", {})) != {"default_branch"}
    ):
        raise PlanError("default branch changes require an isolated single-operation policy plan")
    for operation in value["operations"]:
        _operation_policy_binding(value, operation)
    local_checkout = value.get("local_checkout")
    if not isinstance(local_checkout, dict) or not isinstance(local_checkout.get("supported"), bool):
        raise PlanError("plan local checkout binding is malformed")
    if local_checkout["supported"] is True:
        required_checkout_fields = {"supported", "reason", "target", "root_binding", "head", "branch", "dirty", "origin", "upstream"}
        if not required_checkout_fields <= set(local_checkout):
            raise PlanError("plan local checkout binding is incomplete")
        if local_checkout.get("target", "").casefold() != value["target"]["observed_full_name"].casefold():
            raise PlanError("plan local checkout target does not match the remote repository")
        if not isinstance(local_checkout.get("root_binding"), str) or not re.fullmatch(r"[0-9a-f]{64}", local_checkout["root_binding"]):
            raise PlanError("plan local checkout root binding is malformed")
        if not isinstance(local_checkout.get("head"), str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", local_checkout["head"]):
            raise PlanError("plan local checkout HEAD is malformed")
        branch = local_checkout.get("branch")
        dirty = local_checkout.get("dirty")
        origin = local_checkout.get("origin")
        upstream = local_checkout.get("upstream")
        if (
            not isinstance(branch, dict) or set(branch) != {"detached", "name"}
            or not isinstance(branch.get("detached"), bool)
            or (branch.get("name") is not None and not isinstance(branch.get("name"), str))
            or not isinstance(dirty, dict)
            or set(dirty) != {"clean", "staged_count", "worktree_count", "untracked_count", "status_digest"}
            or not isinstance(dirty.get("clean"), bool)
            or any(not isinstance(dirty.get(key), int) or isinstance(dirty.get(key), bool) or dirty[key] < 0 for key in ("staged_count", "worktree_count", "untracked_count"))
            or not isinstance(dirty.get("status_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", dirty["status_digest"])
            or dirty["clean"] is not (dirty["staged_count"] == 0 and dirty["worktree_count"] == 0 and dirty["untracked_count"] == 0)
            or not isinstance(origin, dict) or set(origin) != {"configured", "matches_target"}
            or any(not isinstance(origin.get(key), bool) for key in ("configured", "matches_target"))
            or not isinstance(upstream, dict) or set(upstream) != {"configured", "remote", "branch", "matches_target"}
            or not isinstance(upstream.get("configured"), bool)
            or not isinstance(upstream.get("matches_target"), bool)
            or (upstream.get("remote") is not None and not isinstance(upstream.get("remote"), str))
            or (upstream.get("branch") is not None and not isinstance(upstream.get("branch"), str))
        ):
            raise PlanError("plan local checkout status is malformed")
        if (branch["detached"] and branch["name"] is not None) or (not branch["detached"] and not branch["name"]):
            raise PlanError("plan local branch binding is inconsistent")
    elif set(local_checkout) != {"supported", "reason"} or not isinstance(local_checkout.get("reason"), str):
        raise PlanError("unsupported local checkout report is malformed")

    local_evidence = value.get("local_evidence")
    if not isinstance(local_evidence, dict) or not isinstance(local_evidence.get("supported"), bool):
        raise PlanError("plan local repository evidence is malformed")
    if local_evidence.get("supported"):
        if set(local_evidence) != {"supported", "reason", "root_binding", "controls", "file_hashes", "social_preview"}:
            raise PlanError("plan local repository evidence fields are unsupported")
        if local_evidence.get("root_binding") != local_checkout.get("root_binding"):
            raise PlanError("plan local evidence is not bound to the selected Git checkout")
        if not isinstance(local_evidence.get("controls"), dict) or set(local_evidence["controls"]) != set(_LOCAL_EVIDENCE_CONTROLS.values()):
            raise PlanError("plan local evidence does not contain the fixed content-control registry")
        file_hashes = local_evidence.get("file_hashes")
        if not isinstance(file_hashes, dict):
            raise PlanError("plan local file hashes must be an object")
        for relative_path, record in file_hashes.items():
            if (
                not isinstance(relative_path, str) or not relative_path or relative_path.startswith("/")
                or "\\" in relative_path or ":" in relative_path or "\x00" in relative_path
                or any(part in {"", ".", ".."} for part in relative_path.split("/"))
                or not isinstance(record, dict) or set(record) != {"sha256", "size_bytes"}
                or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or not isinstance(record.get("size_bytes"), int) or isinstance(record.get("size_bytes"), bool) or not 0 <= record["size_bytes"] <= 1_000_000
            ):
                raise PlanError("plan local file evidence contains an unsafe path or malformed hash")
        preview = local_evidence.get("social_preview")
        if not isinstance(preview, dict) or preview.get("action") not in {None, "present", "absent"}:
            raise PlanError("plan social preview local evidence is malformed")
        if preview.get("validation") == "valid" and preview.get("action") == "present":
            if (
                not isinstance(preview.get("relative_path"), str)
                or preview.get("relative_path") not in file_hashes
                or file_hashes[preview["relative_path"]].get("sha256") != preview.get("sha256")
                or file_hashes[preview["relative_path"]].get("size_bytes") != preview.get("size_bytes")
                or preview.get("format") not in {"png", "jpeg", "gif"}
            ):
                raise PlanError("plan social preview asset binding is malformed")
    else:
        if set(local_evidence) != {"supported", "reason", "controls", "file_hashes", "social_preview"} or not isinstance(local_evidence.get("reason"), str):
            raise PlanError("unsupported local evidence report is malformed")
        if not isinstance(local_evidence.get("controls"), dict) or local_evidence.get("file_hashes") != {} or not isinstance(local_evidence.get("social_preview"), dict):
            raise PlanError("unsupported local evidence report is malformed")

    control_rows = value.get("control_observations")
    coverage = validate_control_observations(control_rows)
    if coverage != value.get("audit_coverage"):
        raise PlanError("plan audit coverage receipt does not match its exact control observations")
    computed = plan_digest(value)
    if value.get("plan_digest") != computed:
        raise PlanError("plan digest does not match its content")
    return value


def read_plan(path: Path) -> dict[str, Any]:
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        parent_fd = open_private_directory(path.parent, create=False)
        try:
            descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise PlanError("plan JSON must be a user-owned private single-link regular file")
            maximum = 8 * 1024 * 1024
            if info.st_size > maximum:
                raise PlanError("plan JSON exceeds the supported size limit")
            chunks = bytearray()
            while True:
                chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > maximum:
                    raise PlanError("plan JSON exceeds the supported size limit")
        finally:
            os.close(descriptor)
        raw = json.loads(bytes(chunks).decode("utf-8"), object_pairs_hook=_json_object_no_duplicates)
    except PlanError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PlanError("could not read a safe, valid plan JSON file") from exc
    return validate_plan(raw)


def _json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _check_requested_target(plan: dict[str, Any], target: RepositoryTarget) -> None:
    binding = plan["target"]
    if binding["hostname"] != target.hostname or binding["requested_full_name"].casefold() != target.full_name.casefold():
        raise PlanError("plan is bound to a different hostname or requested repository")


def _fresh_binding(adapter: GitHubAdapter, target: RepositoryTarget, expected: dict[str, Any], *, require_admin: bool) -> dict[str, Any]:
    capabilities = adapter.auth_capabilities()
    if not capabilities.get("authenticated"):
        raise ApplyError("GitHub authentication is unavailable for the requested hostname")
    actor = adapter.actor()
    repo = adapter.repository()
    if actor.get("id") != expected["actor_id"]:
        raise ApplyError("authenticated actor changed since the plan was created")
    if repo.get("id") != expected["repository_id"]:
        raise ApplyError("immutable repository ID changed since the plan was created")
    if repo.get("full_name") != expected["observed_full_name"]:
        raise ApplyError("observed repository full name changed since the plan was created")
    if repo.get("full_name", "").casefold() != target.full_name.casefold():
        raise ApplyError("requested repository now resolves to a different full name")
    permissions = repo.get("permissions") if isinstance(repo.get("permissions"), dict) else {}
    if require_admin and permissions.get("admin") is not True:
        raise ApplyError("authenticated actor no longer has observed repository administrator access")
    scopes = capabilities.get("scopes")
    scope_visibility = capabilities.get("scope_visibility")
    if require_admin and scope_visibility == "reported" and isinstance(scopes, list) and "repo" not in scopes:
        raise ApplyError("visible classic token scopes do not include the required repo scope")
    return {"capabilities": capabilities, "actor": actor, "repository": repo}


def _state_equal(left: Any, right: Any) -> bool:
    return left == right


def _verification_report(
    plan: dict[str, Any],
    target: RepositoryTarget,
    adapter: GitHubAdapter,
    checkout_path: Path,
    *,
    trusted_public_keys: list[bytes] | None,
    local_matches: bool,
) -> dict[str, Any]:
    requirements = plan["policy"].get("verification", {})
    signatures = requirements.get("signatures") if isinstance(requirements, dict) else None
    release = requirements.get("release") if isinstance(requirements, dict) else None
    if not signatures and not release:
        return {"status": "not_assessed", "reason": "verification_requirements_not_configured"}
    if not local_matches or not checkout_is_bound_to_target(plan["local_checkout"]):
        return {
            "status": "incomplete",
            "reason": "checkout_changed_or_target_binding_unestablished",
            "signatures": {"status": "incomplete", "reason": "checkout_changed_or_target_binding_unestablished"} if signatures else None,
            "release": {"status": "incomplete", "reason": "checkout_changed_or_target_binding_unestablished"} if release else None,
        }

    signature_result: dict[str, Any] | None = None
    if signatures:
        signature_result = verify_release_signatures(
            checkout_path,
            signatures["commit_oid"],
            signatures["tag_name"],
            trusted_public_keys,
            signatures["expected_primary_fingerprints"],
        )

    release_result: dict[str, Any] | None = None
    if release:
        identity = {
            "repository_id": plan["target"]["repository_id"],
            "repository_name": plan["target"]["observed_full_name"],
            "release_id": release["release_id"],
            "tag_name": release["tag_name"],
            "source_ref": release["source_ref"],
            "source_commit": release["source_commit"],
        }
        safe_identity = {
            "hostname": target.hostname,
            "repository_id": plan["target"]["repository_id"],
            "repository_name": plan["target"]["observed_full_name"],
            "release_id": release["release_id"],
            "tag_name": release["tag_name"],
            "source_ref": release["source_ref"],
            "source_commit": release["source_commit"],
            "signer_workflow": release["signer_workflow"],
            "predicate_type": release["predicate_type"],
        }
        artifact_expectations = [
            {
                "asset_id": artifact["asset_id"],
                "name": artifact["name"],
                "relative_path": artifact["path"],
                "expected_sha256": artifact["expected_sha256"],
            }
            for artifact in release["artifacts"]
        ]
        local_hashes = verify_release_artifacts(checkout_path, identity, artifact_expectations)
        safe_assets: list[dict[str, Any]] = []
        statuses = [local_hashes["status"]] if local_hashes.get("status") != "incomplete" else []
        artifact_hashes = {row.get("asset_id"): row for row in local_hashes.get("artifacts", []) if isinstance(row, dict)}
        remote_identity_ok = False
        try:
            remote_release = adapter.release_identity(release["release_id"])
            if not isinstance(remote_release, dict) or remote_release.get("id") != release["release_id"] or remote_release.get("tag_name") != release["tag_name"]:
                statuses.append("invalid")
            else:
                remote_commit = adapter.tag_commit_sha(release["tag_name"])
                if remote_commit != release["source_commit"]:
                    statuses.append("invalid")
                else:
                    remote_identity_ok = True
        except (AttributeError, GitHubError):
            statuses.append("unavailable")

        root: Path | None = None
        try:
            root = checkout_path.expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            statuses.append("unavailable")

        for artifact in release["artifacts"]:
            local = artifact_hashes.get(artifact["asset_id"], {})
            item_status = local.get("status", "incomplete")
            reason = local.get("reason")
            digest = local.get("sha256")
            remote_asset: dict[str, Any] | None = None
            if item_status == "matched" and remote_identity_ok:
                try:
                    remote_asset = adapter.release_asset_identity(release["release_id"], artifact["asset_id"])
                    remote_digest = remote_asset.get("digest")
                    if remote_asset.get("id") != artifact["asset_id"] or remote_asset.get("name") != artifact["name"]:
                        item_status, reason = "invalid", "release_asset_identity_mismatch"
                    elif not isinstance(remote_digest, str):
                        item_status, reason = "incomplete", "release_asset_digest_unavailable"
                    elif remote_digest != f"sha256:{artifact['expected_sha256']}":
                        item_status, reason = "invalid", "release_asset_digest_mismatch"
                except (AttributeError, GitHubError):
                    item_status, reason = "unavailable", "release_asset_identity_unavailable"
            if item_status == "matched" and remote_identity_ok and root is not None:
                file_path = root / artifact["path"]
                verify_asset = getattr(adapter, "verify_release_asset", None)
                verify_attestation = getattr(adapter, "verify_attestation", None)
                if not callable(verify_asset) or not callable(verify_attestation):
                    item_status, reason = "unavailable", "release_verification_commands_unavailable"
                else:
                    try:
                        release_proof = verify_asset(release["tag_name"], os.fspath(file_path))
                        attestation_proof = verify_attestation(
                            os.fspath(file_path),
                            signer_workflow=release["signer_workflow"],
                            source_ref=release["source_ref"],
                            predicate_type=release["predicate_type"],
                        )
                    except (AttributeError, GitHubError, OSError, RuntimeError, TypeError, ValueError):
                        release_proof = attestation_proof = None
                    proof_statuses = [
                        item.get("status") if isinstance(item, dict) else "unavailable"
                        for item in (release_proof, attestation_proof)
                    ]
                    if any(status == "invalid" for status in proof_statuses):
                        item_status, reason = "invalid", "release_signature_or_attestation_invalid"
                    elif any(status == "unavailable" for status in proof_statuses):
                        item_status, reason = "unavailable", "release_signature_or_attestation_unavailable"
                    elif proof_statuses != ["verified", "verified"]:
                        item_status, reason = "incomplete", "release_signature_or_attestation_incomplete"
                    else:
                        item_status, reason = "verified", None
            if item_status == "matched":
                item_status, reason = "incomplete", reason or "release_cryptographic_verification_not_completed"
            if item_status != "verified":
                statuses.append(item_status if item_status in {"invalid", "unavailable"} else "incomplete")
            safe_assets.append({
                "asset_id": artifact["asset_id"],
                "name": artifact["name"],
                "sha256": digest if isinstance(digest, str) else None,
                "status": item_status,
                "reason": reason,
            })

        # Rehash after the GitHub CLI reads each local file to catch replacement or mutation during proof.
        after_hashes = verify_release_artifacts(checkout_path, identity, artifact_expectations)
        if after_hashes.get("status") == "invalid":
            statuses.append("invalid")
        elif after_hashes.get("status") != "incomplete":
            statuses.append("unavailable" if after_hashes.get("status") == "unavailable" else "incomplete")
        before_map = {row.get("asset_id"): row.get("sha256") for row in local_hashes.get("artifacts", []) if isinstance(row, dict)}
        after_map = {row.get("asset_id"): row.get("sha256") for row in after_hashes.get("artifacts", []) if isinstance(row, dict)}
        if before_map != after_map:
            statuses.append("invalid")
        status = "invalid" if "invalid" in statuses else "unavailable" if "unavailable" in statuses else "incomplete" if statuses else "verified"
        release_result = {
            "status": status,
            "reason": None if status == "verified" else "release_evidence_incomplete_or_invalid",
            "identity": safe_identity,
            "artifacts": safe_assets,
        }

    component_statuses = [item.get("status") for item in (signature_result, release_result) if isinstance(item, dict)]
    status = "invalid" if "invalid" in component_statuses else "unavailable" if "unavailable" in component_statuses else "incomplete" if any(value != "verified" for value in component_statuses) else "verified"
    report: dict[str, Any] = {"status": status, "reason": None if status == "verified" else "one_or_more_required_verifications_did_not_pass"}
    if signature_result is not None:
        report["signatures"] = signature_result
    if release_result is not None:
        report["release"] = release_result
    return report


def _operation_field_valid(kind: str, key: str, value: Any) -> bool:
    if kind == "repository_patch":
        if key in {"description", "homepage"}:
            return isinstance(value, str)
        if key in {"squash_merge_commit_title", "squash_merge_commit_message"}:
            allowed = {"PR_TITLE", "COMMIT_OR_PR_TITLE"} if key.endswith("title") else {"PR_BODY", "COMMIT_MESSAGES", "BLANK"}
            return isinstance(value, str) and value in allowed
        return isinstance(value, bool)
    if kind == "actions_repository_policy":
        if key in {"enabled", "sha_pinning_required"}:
            return isinstance(value, bool)
        return isinstance(value, str) and value in {"all", "local_only", "selected"}
    if kind == "actions_workflow_policy":
        if key == "default_workflow_permissions":
            return isinstance(value, str) and value in {"read", "write"}
        return isinstance(value, bool)
    if kind == "actions_selected_policy":
        if key in {"github_owned_allowed", "verified_allowed"}:
            return isinstance(value, bool)
        return isinstance(value, list) and all(isinstance(item, str) for item in value) and len(set(value)) == len(value)
    if kind in {"pages_create", "pages_update"}:
        if key == "build_type":
            return isinstance(value, str) and value in {"legacy", "workflow"}
        if key == "source":
            return isinstance(value, dict) and set(value) == {"branch", "path"} and isinstance(value.get("branch"), str) and bool(value["branch"]) and isinstance(value.get("path"), str) and value["path"] in {"/", "/docs"}
        if key == "cname":
            return value is None or isinstance(value, str)
        return isinstance(value, bool)
    if kind == "security_analysis_patch":
        return isinstance(value, bool)
    return False


def _reconcile_pending(adapter: GitHubAdapter, target: RepositoryTarget, expected: dict[str, Any], directory: Path, row: dict[str, Any], lock_guard: Any) -> dict[str, Any]:
    operation = row.get("operation")
    if not isinstance(operation, dict):
        raise ApplyError("pending operation has no typed payload; manual reconciliation is required")
    validate_operation(operation)
    _fresh_binding(adapter, target, expected, require_admin=False)
    current = adapter.operation_state(operation)
    _validate_remote_operation_state(operation, current)
    if _state_equal(current, operation.get("desired")):
        status = "reconciled_applied"
    elif _state_equal(current, operation.get("current")):
        status = "reconciled_not_applied"
    else:
        status = "reconciliation_conflict"
    append_journal(directory, {
        "operation_id": operation["id"],
        "plan_digest": row.get("plan_digest"),
        "state": status,
        "reconciliation": "read_only_current_state",
    }, lock_guard)
    return {"operation_id": operation["id"], "status": status}


def apply_plan(
    plan: dict[str, Any],
    target: RepositoryTarget,
    adapter: GitHubAdapter,
    state_root: Path,
    *,
    supplied_digest: str,
    operation_ids: list[str],
    checkout_path: Path | None = None,
) -> dict[str, Any]:
    validate_plan(plan)
    _check_requested_target(plan, target)
    if supplied_digest != plan["plan_digest"]:
        raise PlanError("supplied plan digest does not match the reviewed plan")
    if not operation_ids or len(set(operation_ids)) != len(operation_ids):
        raise PlanError("apply requires one or more exact, unique --operation OPERATION_ID values")
    known = {operation["id"]: operation for operation in plan["operations"]}
    unknown = sorted(set(operation_ids) - set(known))
    if unknown:
        raise PlanError("apply operation IDs must exactly match IDs in the plan")
    selected = [known[operation_id] for operation_id in operation_ids]
    selected_checkout = Path(checkout_path or Path.cwd()).expanduser()
    if (
        plan["local_checkout"].get("supported") is not True
        or not checkout_is_bound_to_target(plan["local_checkout"])
        or plan["local_evidence"].get("supported") is not True
    ):
        raise ApplyError("the reviewed plan has no target-bound local checkout and content evidence")
    if plan["audit_coverage"].get("status") != "complete":
        raise ApplyError("the reviewed plan has incomplete repository control coverage; create and review a complete audit first")
    if not _local_state_matches(plan, selected_checkout, target):
        raise ApplyError("local checkout or inspected content changed since the plan was created; create and review a fresh plan")
    directory = operation_state_directory(state_root, target.hostname, plan["target"]["repository_id"])
    results: list[dict[str, Any]] = []
    with target_operation_lock(directory) as lock_guard:
        latest = latest_operation_records(read_journal(directory, lock_guard))
        pending = [row for row in latest.values() if row.get("state") == "pending"]
        if pending:
            reconciled = []
            for row in pending:
                reconciled.append(_reconcile_pending(adapter, target, plan["target"], directory, row, lock_guard))
            return {"status": "reconciliation_required", "plan_digest": plan["plan_digest"], "reconciled": reconciled, "applied": []}

        for operation in selected:
            if not _local_state_matches(plan, selected_checkout, target):
                raise ApplyError("local checkout or inspected content changed before a remote write; no further write was sent")
            _fresh_binding(adapter, target, plan["target"], require_admin=True)
            try:
                current = adapter.operation_state(operation)
                _validate_remote_operation_state(operation, current)
            except GitHubError as exc:
                raise ApplyError("requested operation current state is missing or invalid; no remote write was sent") from exc
            if _state_equal(current, operation["desired"]):
                results.append({"operation_id": operation["id"], "status": "already_correct"})
                continue
            if not _state_equal(current, operation["current"]):
                raise ApplyError(f"stale precondition for operation {operation['id']}; create and review a fresh plan")
            capabilities = adapter.auth_capabilities()
            scopes = capabilities.get("scopes")
            if capabilities.get("scope_visibility") == "reported" and isinstance(scopes, list) and "repo" not in scopes:
                raise ApplyError("visible classic token scopes do not include repo; no remote write was sent")

            append_journal(directory, {
                "operation_id": operation["id"],
                "plan_digest": plan["plan_digest"],
                "state": "pending",
                "operation": operation,
            }, lock_guard)
            try:
                adapter.apply_operation(operation)
            except GitHubError as exc:
                try:
                    reconciled = _reconcile_pending(adapter, target, plan["target"], directory, {
                        "operation_id": operation["id"], "plan_digest": plan["plan_digest"], "operation": operation,
                    }, lock_guard)
                except Exception:
                    reconciled = {"operation_id": operation["id"], "status": "reconciliation_pending"}
                result = {"operation_id": operation["id"], "status": reconciled["status"], "error": exc.code}
                if exc.status is not None:
                    result["http_status"] = exc.status
                results.append(result)
                return {"status": "reconciled_after_write_error", "plan_digest": plan["plan_digest"], "applied": results}
            try:
                verified_state = adapter.operation_state(operation)
                _validate_remote_operation_state(operation, verified_state)
            except GitHubError as exc:
                results.append({"operation_id": operation["id"], "status": "verification_pending", "error": exc.code})
                return {"status": "verification_pending", "plan_digest": plan["plan_digest"], "applied": results}
            if not _state_equal(verified_state, operation["desired"]):
                append_journal(directory, {"operation_id": operation["id"], "plan_digest": plan["plan_digest"], "state": "verification_mismatch"}, lock_guard)
                results.append({"operation_id": operation["id"], "status": "verification_mismatch"})
                return {"status": "verification_failed", "plan_digest": plan["plan_digest"], "applied": results}
            append_journal(directory, {"operation_id": operation["id"], "plan_digest": plan["plan_digest"], "state": "applied"}, lock_guard)
            results.append({"operation_id": operation["id"], "status": "applied"})
    return {
        "status": "complete",
        "plan_digest": plan["plan_digest"],
        "applied": results,
        "settings_apply_completion": {"status": "complete"},
        "release_readiness": {
            "status": "not_assessed",
            "reason": "settings_apply_does_not_assess_release_readiness",
        },
    }


def verify_plan(
    plan: dict[str, Any],
    target: RepositoryTarget,
    adapter: GitHubAdapter,
    *,
    checkout_path: Path | None = None,
    trusted_public_keys: list[bytes] | None = None,
) -> dict[str, Any]:
    validate_plan(plan)
    _check_requested_target(plan, target)
    selected_checkout = Path(checkout_path or Path.cwd()).expanduser()
    _fresh_binding(adapter, target, plan["target"], require_admin=False)
    results = []
    for operation in plan["operations"]:
        try:
            current = adapter.operation_state(operation)
            _validate_remote_operation_state(operation, current)
            matches = _state_equal(current, operation["desired"])
            results.append({"operation_id": operation["id"], "status": "verified" if matches else "mismatch", "current": current})
        except GitHubError as exc:
            item = {"operation_id": operation["id"], "status": "unavailable", "reason": exc.code}
            if exc.status is not None:
                item["http_status"] = exc.status
            results.append(item)
    current_snapshot = _snapshot_with_local_context(adapter, target, selected_checkout, plan["policy"])
    if current_snapshot["binding"] != plan["target"]:
        raise ApplyError("repository or actor identity changed during verification")
    local_matches = (
        checkout_is_bound_to_target(current_snapshot["local_checkout"])
        and current_snapshot["local_checkout"] == plan["local_checkout"]
        and current_snapshot["local_evidence"] == plan["local_evidence"]
    )
    current_plan = build_plan(current_snapshot, plan["policy"])
    requested_findings = [item for item in current_plan["report_only"] if item.get("scope") == "requested_policy"]
    requested_mismatches = current_plan["operations"]
    all_operation_checks_pass = all(row["status"] == "verified" for row in results)
    coverage_complete = current_snapshot["audit_coverage"].get("status") == "complete"
    settings_complete = all_operation_checks_pass and not requested_findings and not requested_mismatches and local_matches and coverage_complete
    evidence = _verification_report(
        plan,
        target,
        adapter,
        selected_checkout,
        trusted_public_keys=trusted_public_keys,
        local_matches=local_matches,
    )
    evidence_status = evidence.get("status")
    evidence_satisfied = evidence_status in {"verified", "not_assessed"}
    requested_policy_complete = settings_complete and evidence_satisfied
    if not evidence_satisfied:
        requested_findings = [
            *requested_findings,
            {
                "group": "releases",
                "control": "required_release_verification",
                "scope": "requested_policy",
                "status": evidence_status or "incomplete",
                "reason": "configured_verification_requirements_not_satisfied",
            },
        ]
    return {
        "status": "verified" if requested_policy_complete else "incomplete",
        "plan_digest": plan["plan_digest"],
        "target": plan["target"],
        "operations": results,
        "settings": {
            "status": "verified" if settings_complete else "incomplete",
            "operations_complete": all_operation_checks_pass,
            "local_state": "verified" if local_matches else "changed_or_unavailable",
            "audit_coverage": "complete" if coverage_complete else "incomplete",
        },
        "release_readiness": evidence,
        "requested_policy": {
            "status": "complete" if requested_policy_complete else "incomplete",
            "mismatches": requested_mismatches,
            "findings": requested_findings,
        },
        "inventory": current_snapshot["groups"],
        "local_state": {"status": "verified" if local_matches else "changed_or_unavailable"},
        "local_checkout": current_snapshot["local_checkout"],
        "local_evidence": current_snapshot["local_evidence"],
        "control_observations": current_snapshot["control_observations"],
        "audit_coverage": current_snapshot["audit_coverage"],
        "report_only": current_plan["report_only"],
    }


def audit(adapter: GitHubAdapter, target: RepositoryTarget, *, checkout_path: Path | None = None) -> dict[str, Any]:
    snapshot = _snapshot_with_local_context(adapter, target, Path(checkout_path or Path.cwd()).expanduser())
    return {
        "mode": "audit",
        "target": snapshot["binding"],
        "authorization": snapshot["authorization"],
        "groups": snapshot["groups"],
        "local_checkout": snapshot["local_checkout"],
        "local_evidence": snapshot["local_evidence"],
        "control_observations": snapshot["control_observations"],
        "audit_coverage": snapshot["audit_coverage"],
    }


def create_plan(
    adapter: GitHubAdapter,
    target: RepositoryTarget,
    policy: dict[str, Any],
    *,
    checkout_path: Path | None = None,
) -> dict[str, Any]:
    policy = normalize_policy(policy)
    snapshot = _snapshot_with_local_context(adapter, target, Path(checkout_path or Path.cwd()).expanduser(), policy)
    plan = build_plan(snapshot, policy)
    return validate_plan(plan)


def write_plan_pair(plan: dict[str, Any], output_directory: Path) -> tuple[Path, Path]:
    output_directory = Path(os.path.abspath(os.fspath(output_directory.expanduser())))
    json_path = output_directory / "plan.json"
    markdown_path = output_directory / "plan.md"
    raw_json = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown_text = _plan_markdown(plan)
    if len(raw_json.encode("utf-8")) > 8 * 1024 * 1024:
        raise PlanError("plan JSON exceeds the supported size limit")
    directory_fd = open_private_directory(output_directory, create=True)

    def read_existing(name: str) -> bytes | None:
        try:
            descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PlanError("plan output path must not be a symlink or special file") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise PlanError("existing plan output must be a user-owned private single-link regular file")
            if info.st_size > 8 * 1024 * 1024:
                raise PlanError("existing plan output exceeds the supported size limit")
            chunks = bytearray()
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    return bytes(chunks)
                chunks.extend(chunk)
                if len(chunks) > 8 * 1024 * 1024:
                    raise PlanError("existing plan output exceeds the supported size limit")
        finally:
            os.close(descriptor)

    try:
        existing_json = read_existing("plan.json")
        existing_markdown = read_existing("plan.md")
        if existing_json is not None or existing_markdown is not None:
            if existing_json == raw_json.encode("utf-8") and existing_markdown == markdown_text.encode("utf-8"):
                return json_path, markdown_path
            raise PlanError("plan output JSON or Markdown file already exists with different content")
        created: list[str] = []
        try:
            for name, raw in (("plan.json", raw_json.encode("utf-8")), ("plan.md", markdown_text.encode("utf-8"))):
                descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
                os.fchmod(descriptor, 0o600)
                info = os.fstat(descriptor)
                created.append(name)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    os.close(descriptor)
                    raise PlanError("new plan output is not a private regular file")
                try:
                    view = memoryview(raw)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise PlanError("plan output write was incomplete")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except Exception as exc:
            for name in created:
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1:
                        os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            if isinstance(exc, PlanError):
                raise
            if isinstance(exc, FileExistsError):
                raise PlanError("plan output appeared during exclusive creation; retry after inspection") from exc
            raise
        os.fsync(directory_fd)
        return json_path, markdown_path
    finally:
        os.close(directory_fd)


def _plan_markdown(plan: dict[str, Any]) -> str:
    from .planning import plan_markdown
    return plan_markdown(plan)
