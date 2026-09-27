from __future__ import annotations

import copy
import json
from typing import Any

from .model import Operation, canonical_ruleset, make_operation, plan_digest
from .policy import policy_digest


_REPO_KEYS = {
    "description", "homepage", "has_issues", "has_projects", "has_wiki", "has_downloads",
    "allow_squash_merge", "allow_merge_commit", "allow_rebase_merge", "allow_auto_merge",
    "delete_branch_on_merge", "squash_merge_commit_title", "squash_merge_commit_message",
    "web_commit_signoff_required",
}
_SECURITY_POLICY_NAMES = {
    "advanced_security": "advanced_security",
    "code_security": "code_security",
    "secret_scanning": "secret_scanning",
    "secret_scanning_push_protection": "secret_scanning_push_protection",
    "secret_scanning_non_provider_patterns": "secret_scanning_non_provider_patterns",
    "secret_scanning_ai_detection": "secret_scanning_ai_detection",
}
_ACTIONS_REPOSITORY_TYPES = {
    "enabled": lambda value: isinstance(value, bool),
    "allowed_actions": lambda value: isinstance(value, str) and value in {"all", "local_only", "selected"},
    "sha_pinning_required": lambda value: isinstance(value, bool),
}
_ACTIONS_WORKFLOW_TYPES = {
    "default_workflow_permissions": lambda value: isinstance(value, str) and value in {"read", "write"},
    "can_approve_pull_request_reviews": lambda value: isinstance(value, bool),
}
_ACTIONS_SELECTED_TYPES = {
    "github_owned_allowed": lambda value: isinstance(value, bool),
    "verified_allowed": lambda value: isinstance(value, bool),
    "patterns_allowed": lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value),
}


def _missing_or_invalid_fields(
    current: Any,
    requested: dict[str, Any],
    validators: dict[str, Any],
) -> list[str]:
    if not isinstance(current, dict):
        return sorted(requested)
    return sorted(
        key for key in requested
        if key not in current or not validators[key](current[key])
    )


def _pages_value_valid(key: str, value: Any) -> bool:
    if key == "build_type":
        return isinstance(value, str) and value in {"legacy", "workflow"}
    if key == "source":
        return (
            isinstance(value, dict)
            and set(value) == {"branch", "path"}
            and isinstance(value.get("branch"), str)
            and bool(value["branch"])
            and isinstance(value.get("path"), str)
            and value.get("path") in {"/", "/docs"}
        )
    if key == "cname":
        return value is None or isinstance(value, str)
    if key == "https_enforced":
        return isinstance(value, bool)
    return False


def _value(snapshot: dict[str, Any], key: str) -> tuple[bool, Any]:
    value = snapshot.get(key)
    if isinstance(value, dict) and value.get("available") is True:
        return True, value.get("value")
    return False, None


def _permission_findings(group: str, control: str, permissions: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{
        "group": group,
        "control": control,
        "status": "report_only",
        "reason": "required_repository_admin_or_token_scope_is_missing_or_not_observable",
        "required_permissions": list(permissions),
    }]


def _ruleset_api_rules(policy: dict[str, Any], current_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules = {row.get("type"): copy.deepcopy(row) for row in current_rules if isinstance(row, dict) and isinstance(row.get("type"), str)}
    boolean_rules = {
        "require_linear_history": ("required_linear_history", {}),
        "require_signed_commits": ("required_signatures", {}),
        "block_deletions": ("deletion", {}),
        "block_force_pushes": ("non_fast_forward", {}),
    }
    for field, (rule_type, parameters) in boolean_rules.items():
        if field not in policy:
            continue
        if policy[field]:
            rules[rule_type] = {"type": rule_type, **({"parameters": parameters} if parameters else {})}
        else:
            rules.pop(rule_type, None)

    pull_fields = {
        "required_approving_review_count": "required_approving_review_count",
        "dismiss_stale_reviews_on_push": "dismiss_stale_reviews_on_push",
        "require_code_owner_review": "require_code_owner_review",
        "require_last_push_approval": "require_last_push_approval",
        "required_review_thread_resolution": "required_review_thread_resolution",
    }
    if any(field in policy for field in pull_fields):
        previous = rules.get("pull_request", {"type": "pull_request", "parameters": {}})
        parameters = {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews_on_push": False,
            "require_code_owner_review": False,
            "require_last_push_approval": False,
            "required_review_thread_resolution": False,
            **dict(previous.get("parameters") or {}),
        }
        for field, parameter in pull_fields.items():
            if field in policy:
                parameters[parameter] = policy[field]
        rules["pull_request"] = {"type": "pull_request", "parameters": parameters}

    if "required_status_checks" in policy:
        checks = policy["required_status_checks"]
        if checks:
            current_check_rule = rules.get("required_status_checks")
            current_parameters = current_check_rule.get("parameters") if isinstance(current_check_rule, dict) and isinstance(current_check_rule.get("parameters"), dict) else {}
            rules["required_status_checks"] = {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": copy.deepcopy(checks),
                    "strict_required_status_checks_policy": policy["strict_required_status_checks"],
                    "do_not_enforce_on_create": current_parameters.get("do_not_enforce_on_create", False),
                },
            }
        else:
            rules.pop("required_status_checks", None)
    return sorted(rules.values(), key=lambda row: str(row.get("type")))


def _ruleset_payload(policy: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    old = current or {}
    old_conditions = copy.deepcopy(old.get("conditions") or {})
    conditions = dict(old_conditions)
    ref_name = dict(conditions.get("ref_name") or {})
    ref_name["include"] = list(policy["include"])
    ref_name["exclude"] = list(policy.get("exclude", []))
    conditions["ref_name"] = ref_name
    bypass_actors = old.get("bypass_actors", []) if current is not None else []
    if not isinstance(bypass_actors, list):
        bypass_actors = None
    return {
        "name": policy["name"],
        "target": policy["target"],
        "enforcement": policy.get("enforcement", "active"),
        "conditions": conditions,
        "rules": _ruleset_api_rules(policy, canonical_ruleset(current).get("rules", []) if current is not None else []),
        "bypass_actors": copy.deepcopy(bypass_actors),
    }


def ruleset_reduces_protection(current: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    """Conservatively detect ruleset updates that may enforce less protection."""
    if current is None:
        return desired.get("enforcement") != "active"
    if current.get("enforcement") == "active" and desired.get("enforcement") != "active":
        return True
    if current.get("conditions") != desired.get("conditions"):
        # GitHub ref glob inclusion/exclusion is not a simple subset relation.
        # Isolate any changed enforcement scope rather than guessing whether it
        # widens or narrows the protected refs.
        return True

    old_rules = {
        row.get("type"): row
        for row in current.get("rules", [])
        if isinstance(row, dict) and isinstance(row.get("type"), str)
    }
    new_rules = {
        row.get("type"): row
        for row in desired.get("rules", [])
        if isinstance(row, dict) and isinstance(row.get("type"), str)
    }
    if set(old_rules) - set(new_rules):
        return True

    old_pull = old_rules.get("pull_request")
    new_pull = new_rules.get("pull_request")
    if isinstance(old_pull, dict) and isinstance(new_pull, dict):
        old_parameters = old_pull.get("parameters") if isinstance(old_pull.get("parameters"), dict) else {}
        new_parameters = new_pull.get("parameters") if isinstance(new_pull.get("parameters"), dict) else {}
        if new_parameters.get("required_approving_review_count", 0) < old_parameters.get("required_approving_review_count", 0):
            return True
        for key in (
            "dismiss_stale_reviews_on_push",
            "require_code_owner_review",
            "require_last_push_approval",
            "required_review_thread_resolution",
        ):
            if old_parameters.get(key, False) is True and new_parameters.get(key, False) is False:
                return True

    old_checks = old_rules.get("required_status_checks")
    new_checks = new_rules.get("required_status_checks")
    if isinstance(old_checks, dict) and isinstance(new_checks, dict):
        old_parameters = old_checks.get("parameters") if isinstance(old_checks.get("parameters"), dict) else {}
        new_parameters = new_checks.get("parameters") if isinstance(new_checks.get("parameters"), dict) else {}
        old_items = {json.dumps(item, sort_keys=True, separators=(",", ":")) for item in old_parameters.get("required_status_checks", [])}
        new_items = {json.dumps(item, sort_keys=True, separators=(",", ":")) for item in new_parameters.get("required_status_checks", [])}
        if old_items - new_items:
            return True
        if old_parameters.get("strict_required_status_checks_policy") is True and new_parameters.get("strict_required_status_checks_policy") is False:
            return True
        if old_parameters.get("do_not_enforce_on_create", False) is False and new_parameters.get("do_not_enforce_on_create") is True:
            return True
    return False


def ruleset_policy_is_isolated(policy: dict[str, Any]) -> bool:
    return set(policy) == {"schema_version", "rulesets"} and isinstance(policy.get("rulesets"), list) and len(policy["rulesets"]) == 1


def operation_reduces_protection(kind: str, current: Any, desired: Any) -> bool:
    """Return true when a typed operation removes or weakens a security control."""
    if kind == "ruleset_upsert":
        return ruleset_reduces_protection(current, desired)
    if kind == "repository_visibility":
        return current == "private" and desired == "public"
    if kind == "actions_repository_policy" and isinstance(current, dict) and isinstance(desired, dict):
        if current.get("enabled") is True and desired.get("enabled") is False:
            return True
        restrictions = {"all": 0, "selected": 1, "local_only": 2}
        old, new = current.get("allowed_actions"), desired.get("allowed_actions")
        if old in restrictions and new in restrictions and restrictions[new] < restrictions[old]:
            return True
        if current.get("sha_pinning_required") is True and desired.get("sha_pinning_required") is False:
            return True
    if kind == "actions_workflow_policy" and isinstance(current, dict) and isinstance(desired, dict):
        if current.get("default_workflow_permissions") == "read" and desired.get("default_workflow_permissions") == "write":
            return True
        if current.get("can_approve_pull_request_reviews") is False and desired.get("can_approve_pull_request_reviews") is True:
            return True
    if kind == "actions_selected_policy" and isinstance(current, dict) and isinstance(desired, dict):
        for key in ("github_owned_allowed", "verified_allowed"):
            if current.get(key) is False and desired.get(key) is True:
                return True
        old_patterns, new_patterns = current.get("patterns_allowed"), desired.get("patterns_allowed")
        # Action patterns are GitHub globs. Their inclusion relation cannot be
        # established with ordinary set comparison, so isolate every change.
        if isinstance(old_patterns, list) and isinstance(new_patterns, list) and set(old_patterns) != set(new_patterns):
            return True
    if kind in {"pages_create", "pages_update"} and isinstance(desired, dict):
        if desired.get("https_enforced") is False and (
            current is None or (isinstance(current, dict) and current.get("https_enforced") is True)
        ):
            return True
    if kind == "security_analysis_patch" and isinstance(current, dict) and isinstance(desired, dict):
        if any(current.get(key) is True and desired.get(key) is False for key in desired):
            return True
    if kind in {
        "dependabot_alerts_toggle", "automated_security_fixes_toggle",
        "private_vulnerability_reporting_toggle", "immutable_releases_toggle",
    }:
        return current is True and desired is False
    return False


def protection_reduction_policy_is_isolated(policy: dict[str, Any], operation: dict[str, Any]) -> bool:
    """Require the policy itself to contain only the one protection reduction."""
    kind = operation.get("kind")
    desired = operation.get("desired")
    if kind == "ruleset_upsert":
        return ruleset_policy_is_isolated(policy)
    if kind in {"actions_repository_policy", "actions_workflow_policy"}:
        return (
            set(policy) == {"schema_version", "actions"}
            and set(policy["actions"]) == set(desired)
        )
    if kind == "actions_selected_policy":
        selected = policy.get("actions", {}).get("selected_actions")
        return (
            set(policy) == {"schema_version", "actions"}
            and set(policy["actions"]) in ({"selected_actions"}, {"selected_actions", "allowed_actions"})
            and policy["actions"].get("allowed_actions", "selected") == "selected"
            and isinstance(selected, dict)
            and set(selected) == set(desired)
        )
    if kind in {"pages_create", "pages_update"}:
        page_policy = policy.get("pages")
        return (
            set(policy) == {"schema_version", "pages"}
            and isinstance(page_policy, dict)
            and set(page_policy) - {"enabled"} == set(desired)
            and page_policy.get("enabled", True) is True
        )
    if kind == "security_analysis_patch":
        names = {value: key for key, value in _SECURITY_POLICY_NAMES.items()}
        requested = policy.get("security", {})
        return (
            set(policy) == {"schema_version", "security"}
            and set(requested) == {names[key] for key in desired}
        )
    if kind in {"dependabot_alerts_toggle", "automated_security_fixes_toggle", "private_vulnerability_reporting_toggle"}:
        policy_name = {
            "dependabot_alerts_toggle": "dependabot_alerts",
            "automated_security_fixes_toggle": "automated_security_fixes",
            "private_vulnerability_reporting_toggle": "private_vulnerability_reporting",
        }[kind]
        return set(policy) == {"schema_version", "security"} and set(policy["security"]) == {policy_name}
    if kind == "immutable_releases_toggle":
        return set(policy) == {"schema_version", "releases"} and set(policy["releases"]) == {"immutable"}
    return False


def _security_state(snapshot: dict[str, Any], key: str) -> tuple[bool, Any]:
    repo = snapshot.get("repository")
    if not isinstance(repo, dict):
        return False, None
    security = repo.get("security_and_analysis")
    if not isinstance(security, dict) or key not in security:
        return False, None
    return True, security[key]


def compile_operations(snapshot: dict[str, Any], policy: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    operations: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    authorization = snapshot.get("authorization") or {}
    scopes = authorization.get("scopes")
    can_write = (
        authorization.get("repository_admin") is True
        and (
            authorization.get("scope_visibility") != "reported"
            or (isinstance(scopes, list) and "repo" in scopes)
        )
    )
    if authorization.get("scope_visibility") != "reported":
        findings.append({
            "group": "identity_discovery",
            "control": "token_permission_visibility",
            "status": "informational",
            "reason": "gh_auth_status_does_not_expose_fine_grained_token_permission_grants; GitHub enforces each documented endpoint permission",
        })

    def add(
        group: str,
        kind: str,
        current: Any,
        desired: Any,
        permissions: tuple[str, ...],
        *,
        resource_id: int | None = None,
        risk: str = "moderate",
        verification: str,
        recovery: str,
    ) -> None:
        if current == desired:
            return
        if snapshot["binding"]["hostname"] != "github.com":
            findings.append({
                "group": group,
                "control": kind,
                "status": "report_only",
                "reason": "github_enterprise_endpoint_and_2026_03_10_api_version_compatibility_is_unverified",
            })
            return
        if not can_write:
            findings.extend(_permission_findings(group, kind, permissions))
            return
        risk = "high" if operation_reduces_protection(kind, current, desired) else risk
        operation = make_operation(
            group=group,
            kind=kind,
            resource_id=resource_id,
            current=current,
            desired=desired,
            required_permissions=permissions,
            risk=risk,
            verification=verification,
            recovery=recovery,
        )
        operations.append(operation.to_dict())

    repository = policy.get("repository", {})
    repo_current: dict[str, Any] = {}
    repo_desired: dict[str, Any] = {}
    repo_summary = snapshot.get("repository", {})
    if "visibility" in repository:
        if repo_summary.get("visibility") == repository["visibility"]:
            pass
        elif snapshot["binding"]["hostname"] != "github.com":
            findings.append({"group": "identity_discovery", "control": "visibility", "status": "report_only", "reason": "public_private_visibility_matrix_for_github_enterprise_is_unverified"})
        elif "visibility" not in repo_summary or repo_summary.get("visibility") not in {"public", "private"}:
            findings.append({"group": "identity_discovery", "control": "visibility", "status": "report_only", "reason": "repository_visibility_not_returned_by_documented_API"})
        else:
            add(
                "identity_discovery", "repository_visibility", repo_summary["visibility"], repository["visibility"],
                ("Administration:write",), risk="high",
                verification="GET /repos/{owner}/{repo} visibility",
                recovery="Visibility can disclose repository content publicly; restore visibility only with a separately reviewed policy and exact plan authorization.",
            )
    if "default_branch" in repository:
        desired_branch = repository["default_branch"]
        current_branch = repo_summary.get("default_branch")
        evidence = snapshot.get("requested_default_branch")
        if not isinstance(current_branch, str) or not current_branch:
            findings.append({"group": "identity_discovery", "control": "default_branch", "status": "report_only", "reason": "current_default_branch_not_returned_by_documented_api"})
        elif not isinstance(evidence, dict) or evidence.get("branch") != desired_branch or evidence.get("available") is not True or not isinstance(evidence.get("sha"), str):
            reason = evidence.get("reason") if isinstance(evidence, dict) else None
            findings.append({"group": "identity_discovery", "control": "default_branch", "status": "report_only", "reason": f"requested_default_branch_existence_unavailable:{reason or 'branch_head_not_observed'}"})
        elif current_branch != desired_branch:
            current = {"default_branch": current_branch, "target_branch": desired_branch, "target_branch_sha": evidence["sha"]}
            desired = {"default_branch": desired_branch, "target_branch": desired_branch, "target_branch_sha": evidence["sha"]}
            add(
                "identity_discovery", "repository_default_branch", current, desired,
                ("Administration:write",), risk="high",
                verification="GET /repos/{owner}/{repo} default_branch and GET /repos/{owner}/{repo}/commits/{branch}",
                recovery="Restore only the previously recorded default_branch pointer after confirming the branch still exists.",
            )
    for key in ("description", "homepage"):
        if key in repository:
            if key not in repo_summary:
                findings.append({"group": "identity_discovery", "control": key, "status": "report_only", "reason": "current_value_not_visible_to_api"})
                continue
            old = repo_summary.get(key) or ""
            repo_current[key] = old
            repo_desired[key] = repository[key]
    for feature, enabled in repository.get("features", {}).items():
        key = f"has_{feature}"
        if key not in repo_summary:
            findings.append({"group": "collaboration", "control": feature, "status": "report_only", "reason": "current_value_not_visible_to_api"})
            continue
        repo_current[key] = repo_summary[key]
        repo_desired[key] = enabled
    for key, enabled in repository.get("merge", {}).items():
        if key not in repo_summary:
            findings.append({"group": "collaboration", "control": key, "status": "report_only", "reason": "current_value_not_visible_to_api"})
            continue
        repo_current[key] = repo_summary[key]
        repo_desired[key] = enabled
    if "web_commit_signoff_required" in repository:
        control_rows = snapshot.get("control_observations")
        control = None
        if isinstance(control_rows, list):
            control = next(
                (
                    row for row in control_rows
                    if isinstance(row, dict) and row.get("control_id") == "collaboration.web_commit_signoff"
                ),
                None,
            )
        observed_value = control.get("value") if isinstance(control, dict) else None
        if (
            not isinstance(control, dict)
            or control.get("observation") != "observed"
            or not isinstance(observed_value, bool)
        ):
            reason = control.get("reason") if isinstance(control, dict) else None
            findings.append({
                "group": "collaboration",
                "control": "web_commit_signoff",
                "status": "report_only",
                "reason": reason or "web_commit_signoff_required_current_value_missing_or_invalid",
            })
        else:
            repo_current["web_commit_signoff_required"] = observed_value
            repo_desired["web_commit_signoff_required"] = repository["web_commit_signoff_required"]
    for group_name, allowed_fields in (
        ("identity_discovery", {"description", "homepage"}),
        ("collaboration", _REPO_KEYS - {"description", "homepage"}),
    ):
        desired_fields = {key: value for key, value in repo_desired.items() if key in allowed_fields}
        if desired_fields:
            current_fields = {key: repo_current[key] for key in desired_fields}
            add(
                group_name,
                "repository_patch",
                current_fields,
                desired_fields,
                ("Administration:write",),
                verification="GET /repos/{owner}/{repo} fields in desired",
                recovery="PATCH the previously recorded current field values after reviewing the impact.",
            )

    if "topics" in repository:
        available, topics = _value(snapshot, "topics")
        if not available:
            findings.append({"group": "identity_discovery", "control": "topics", "status": "report_only", "reason": "topic_read_permission_or_endpoint_unavailable"})
        else:
            add(
                "identity_discovery", "topics_replace", topics, repository["topics"], ("Administration:write",),
                verification="GET /repos/{owner}/{repo}/topics",
                recovery="Replace topics with the recorded current topic list.",
            )

    for requested in policy.get("rulesets", []):
        available, all_rulesets = _value(snapshot, "rulesets")
        if not available:
            findings.append({"group": "git_governance", "control": f"ruleset:{requested['name']}", "status": "report_only", "reason": "ruleset_read_permission_or_endpoint_unavailable"})
            continue
        matches = [row for row in all_rulesets if row.get("target") == requested["target"] and row.get("name") == requested["name"]]
        local = [row for row in matches if row.get("source_type") == "Repository"]
        inherited = [row for row in matches if row.get("source_type") != "Repository"]
        if len(local) > 1 or (not local and inherited):
            findings.append({"group": "git_governance", "control": f"ruleset:{requested['name']}", "status": "report_only", "reason": "inherited_or_ambiguous_ruleset_cannot_be_safely_targeted"})
            continue
        if requested.get("enforcement") == "evaluate":
            findings.append({"group": "git_governance", "control": f"ruleset:{requested['name']}", "status": "report_only", "reason": "evaluate_enforcement_is_enterprise_plan_gated"})
            continue
        if local:
            if local[0].get("_details_available") is not True:
                reason = local[0].get("_details_reason", "local_ruleset_detail_not_returned")
                findings.append({
                    "group": "git_governance",
                    "control": f"ruleset:{requested['name']}",
                    "status": "report_only",
                    "reason": f"{reason}; full local ruleset details are required to preserve conditions, rules, and bypass actors",
                })
                continue
            local_conditions = local[0].get("conditions")
            if not isinstance(local[0].get("bypass_actors"), list):
                findings.append({"group": "git_governance", "control": f"ruleset:{requested['name']}", "status": "report_only", "reason": "ruleset_bypass_actors_not_visible; refusing to replace a rule without preserving its access bypass list"})
                continue
            if (not isinstance(local_conditions, dict) or not isinstance(local_conditions.get("ref_name"), dict)
                    or not isinstance(local_conditions["ref_name"].get("include"), list)
                    or not isinstance(local_conditions["ref_name"].get("exclude"), list)
                    or not isinstance(local[0].get("rules"), list)):
                findings.append({"group": "git_governance", "control": f"ruleset:{requested['name']}", "status": "report_only", "reason": "full_local_ruleset_state_not_visible; refusing an update that could replace unknown conditions or rules"})
                continue
        current = canonical_ruleset(local[0]) if local else None
        desired = _ruleset_payload(requested, current)
        add(
            "git_governance", "ruleset_upsert", current, desired, ("Administration:write",),
            resource_id=int(local[0]["id"]) if local and isinstance(local[0].get("id"), int) else None,
            risk="high" if ruleset_reduces_protection(current, desired) else "moderate",
            verification="GET /repos/{owner}/{repo}/rulesets including parent rulesets",
            recovery="Restore the recorded local ruleset payload; review inherited parent policy separately.",
        )

    actions = policy.get("actions", {})
    actions_current: dict[str, Any] = {}
    actions_desired: dict[str, Any] = {}
    available, current_actions = _value(snapshot, "actions_permissions")
    if actions:
        if not available:
            findings.append({"group": "actions_deployment", "control": "actions_repository_policy", "status": "report_only", "reason": "actions_administration_read_permission_or_endpoint_unavailable"})
        else:
            if not isinstance(current_actions, dict):
                current_actions = {}
            for key in ("enabled", "allowed_actions", "sha_pinning_required"):
                if key in actions:
                    if key in current_actions:
                        actions_current[key] = current_actions[key]
                    actions_desired[key] = actions[key]
            if actions_desired:
                invalid = _missing_or_invalid_fields(actions_current, actions_desired, _ACTIONS_REPOSITORY_TYPES)
                if invalid:
                    findings.append({"group": "actions_deployment", "control": "actions_repository_policy", "status": "report_only", "reason": "actions_requested_current_values_missing_or_invalid:" + ",".join(invalid)})
                else:
                    add("actions_deployment", "actions_repository_policy", actions_current, actions_desired, ("Administration:write",), verification="GET /repos/{owner}/{repo}/actions/permissions", recovery="PUT the recorded current Actions repository policy.")
    workflow_fields = {key: actions[key] for key in ("default_workflow_permissions", "can_approve_pull_request_reviews") if key in actions}
    if workflow_fields:
        available, current_workflow = _value(snapshot, "workflow_permissions")
        if not available:
            findings.append({"group": "actions_deployment", "control": "workflow_token_permissions", "status": "report_only", "reason": "actions_administration_read_permission_or_endpoint_unavailable"})
        else:
            if not isinstance(current_workflow, dict):
                current_workflow = {}
            current = {key: current_workflow[key] for key in workflow_fields if key in current_workflow}
            invalid = _missing_or_invalid_fields(current, workflow_fields, _ACTIONS_WORKFLOW_TYPES)
            if invalid:
                findings.append({"group": "actions_deployment", "control": "workflow_token_permissions", "status": "report_only", "reason": "workflow_requested_current_values_missing_or_invalid:" + ",".join(invalid)})
            else:
                add("actions_deployment", "actions_workflow_policy", current, workflow_fields, ("Administration:write",), verification="GET /repos/{owner}/{repo}/actions/permissions/workflow", recovery="Restore the recorded GITHUB_TOKEN permission and approval policy.")
    if "selected_actions" in actions:
        available, current_selected = _value(snapshot, "selected_actions")
        current_mode = current_actions.get("allowed_actions") if isinstance(current_actions, dict) else None
        if not available:
            findings.append({"group": "actions_deployment", "control": "selected_actions", "status": "report_only", "reason": "selected_actions_endpoint_only_reports_state_when_selected_mode_is_active; apply the mode, then generate a fresh plan"})
        else:
            desired_selected = actions["selected_actions"]
            if not isinstance(current_selected, dict):
                current_selected = {}
            current_selected = {key: current_selected[key] for key in desired_selected if key in current_selected}
            invalid = _missing_or_invalid_fields(current_selected, desired_selected, _ACTIONS_SELECTED_TYPES)
            if invalid:
                findings.append({"group": "actions_deployment", "control": "selected_actions", "status": "report_only", "reason": "selected_actions_requested_current_values_missing_or_invalid:" + ",".join(invalid)})
            else:
                add("actions_deployment", "actions_selected_policy", current_selected, desired_selected, ("Administration:write",), verification="GET /repos/{owner}/{repo}/actions/permissions/selected-actions", recovery="Restore the recorded selected Actions policy.")
        if current_mode != "selected" and actions.get("allowed_actions") == "selected":
            findings.append({"group": "actions_deployment", "control": "selected_actions_transition", "status": "report_only", "reason": "selected-action policy must be planned after selected mode is active"})

    pages = policy.get("pages", {})
    if pages:
        available, current_pages = _value(snapshot, "pages")
        if not available:
            findings.append({"group": "actions_deployment", "control": "pages", "status": "report_only", "reason": "pages_read_permission_or_endpoint_unavailable"})
        elif not isinstance(current_pages, dict):
            reason = "pages_absence_is_not_established_by_a_type_valid_current_response" if current_pages is None else "pages_current_response_is_not_an_object"
            findings.append({"group": "actions_deployment", "control": "pages", "status": "report_only", "reason": reason})
        else:
            if pages.get("enabled") is False:
                findings.append({"group": "actions_deployment", "control": "pages.enabled", "status": "report_only", "reason": "deleting_an_existing_pages_site_is_excluded_as_destructive"})
            else:
                update = {key: pages[key] for key in ("build_type", "source", "cname", "https_enforced") if key in pages}
                old = {key: current_pages[key] for key in update if key in current_pages}
                invalid = [key for key in update if key not in old or not _pages_value_valid(key, old[key])]
                if invalid:
                    findings.append({"group": "actions_deployment", "control": "pages", "status": "report_only", "reason": "pages_requested_current_values_missing_or_invalid:" + ",".join(sorted(invalid))})
                elif update:
                    add("actions_deployment", "pages_update", old, update, ("Pages:write", "Administration:write"), risk="high", verification="GET /repos/{owner}/{repo}/pages", recovery="PUT the recorded Pages source, domain, and HTTPS values.")

    security = policy.get("security", {})
    security_current: dict[str, bool] = {}
    security_desired: dict[str, bool] = {}
    security_reducing_requested = any(
        name in _SECURITY_POLICY_NAMES and desired is False
        for name, desired in security.items()
    )
    for name, desired in security.items():
        if name == "dependabot_alerts":
            available, current = _value(snapshot, "vulnerability_alerts")
            kind, control = "dependabot_alerts_toggle", "security_supply_chain"
            permission = ("Administration:write",)
            if available:
                add(control, kind, current, desired, permission, risk="high" if desired is False else "moderate", verification="GET /repos/{owner}/{repo}/vulnerability-alerts", recovery="Restore the recorded dependency-alert state.")
            else:
                findings.append({"group": control, "control": name, "status": "report_only", "reason": "dependabot_alerts_permission_or_plan_unavailable"})
        elif name == "automated_security_fixes":
            available, current = _value(snapshot, "automated_security_fixes")
            if available:
                add("security_supply_chain", "automated_security_fixes_toggle", current, desired, ("Administration:write",), risk="high" if desired is False else "moderate", verification="GET /repos/{owner}/{repo}/automated-security-fixes", recovery="Restore the recorded automated security fix state.")
            else:
                findings.append({"group": "security_supply_chain", "control": name, "status": "report_only", "reason": "automated_security_fixes_permission_or_plan_unavailable"})
        elif name == "private_vulnerability_reporting":
            available, current = _value(snapshot, "private_vulnerability_reporting")
            if not available:
                details = snapshot.get("private_vulnerability_reporting")
                reason = details.get("reason") if isinstance(details, dict) else None
                findings.append({
                    "group": "security_supply_chain",
                    "control": name,
                    "status": "report_only",
                    "reason": f"private_vulnerability_reporting_status_unavailable:{reason or 'unknown'}",
                })
            else:
                add("security_supply_chain", "private_vulnerability_reporting_toggle", current, desired, ("Administration:write",), verification="GET /repos/{owner}/{repo}/private-vulnerability-reporting enabled", recovery="Restore the recorded private vulnerability reporting state.")
        else:
            api_key = _SECURITY_POLICY_NAMES[name]
            available, current = _security_state(snapshot, api_key)
            if not available or current not in {"enabled", "disabled"}:
                findings.append({"group": "security_supply_chain", "control": name, "status": "report_only", "reason": "feature_state_not_returned; plan_or_organization_availability_is_unknown"})
            else:
                security_current[api_key] = current == "enabled"
                security_desired[api_key] = desired

    if security_desired:
        add(
            "security_supply_chain",
            "security_analysis_patch",
            security_current,
            security_desired,
            ("Administration:write",),
            risk="high" if security_reducing_requested else "moderate",
            verification="GET /repos/{owner}/{repo} security_and_analysis",
            recovery="PATCH the recorded status for each changed security feature.",
        )

    if "immutable" in policy.get("releases", {}):
        available, current = _value(snapshot, "immutable_releases")
        desired = policy["releases"]["immutable"]
        if snapshot["binding"]["hostname"] != "github.com":
            findings.append({"group": "releases", "control": "immutable_releases", "status": "report_only", "reason": "GitHub Enterprise compatibility matrix for immutable releases is not established"})
        elif available:
            add("releases", "immutable_releases_toggle", current, desired, ("Administration:write",), risk="high" if desired is False else "moderate", verification="GET /repos/{owner}/{repo}/immutable-releases", recovery="Restore the recorded immutable-release setting after reviewing affected releases.")
        else:
            findings.append({"group": "releases", "control": "immutable_releases", "status": "report_only", "reason": "immutable release status endpoint is not currently observable"})

    social_policy = policy.get("repository_content", {}).get("social_preview")
    if social_policy is not None:
        action = social_policy["action"]
        social_desired = action == "present"
        available, current = _value(snapshot, "social_preview")
        local_preview = snapshot.get("local_evidence", {}).get("social_preview", {})
        local_preview_valid = isinstance(local_preview, dict) and local_preview.get("validation") == "valid"
        # A fresh custom-image Boolean plus unchanged local hash-bound upload
        # file completes the requested handoff. GitHub does not expose remote
        # image bytes, so exact pixel identity remains unverified.
        present_complete = action == "present" and available and current is True and local_preview_valid
        if not present_complete and (not available or current != social_desired or (action == "present" and not local_preview_valid)):
            findings.append({
                "group": "repository_content",
                "control": "social_preview_image_handoff",
                "status": "ui_handoff_required",
                "reason": "social preview image upload and removal are available in repository Settings; no supported REST or GraphQL mutation is documented",
                "desired_custom_preview": social_desired,
                "asset": {
                    key: local_preview.get(key)
                    for key in ("relative_path", "sha256", "size_bytes", "format")
                    if isinstance(local_preview, dict) and key in local_preview
                },
                "url": f"https://{snapshot['binding']['hostname']}/{snapshot['binding']['observed_full_name']}/settings",
                "handoff": "Settings → Social preview → Edit → Upload an image" if social_desired else "Settings → Social preview → Edit → Remove image",
            })

    reducing = [operation for operation in operations if operation_reduces_protection(operation["kind"], operation["current"], operation["desired"])]
    if reducing and (
        len(operations) != 1
        or len(reducing) != 1
        or not protection_reduction_policy_is_isolated(policy, reducing[0])
    ):
        for operation in operations:
            findings.append({
                "group": operation["group"],
                "control": operation["kind"],
                "status": "report_only",
                "reason": "protection_reducing_settings_require_a_single_operation_and_isolated_policy",
            })
        operations = []

    return sorted(operations, key=lambda row: (row["group"], row["id"])), findings


def build_plan(snapshot: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    operations, findings = compile_operations(snapshot, policy)
    groups = snapshot["groups"]
    report_only = []
    for group_name, group in sorted(groups.items()):
        for finding in group.get("report_only", []):
            report_only.append({"group": group_name, "scope": "inventory", **finding})
    report_only.extend({
        "scope": "authorization" if finding.get("status") == "informational" else "requested_policy",
        **finding,
    } for finding in findings)
    for row in snapshot.get("control_observations", []):
        if row.get("observation") in {"unavailable", "incomplete", "unknown", "not_applicable"} or row.get("capability") == "ui-handoff":
            report_only.append({
                "group": row["group"],
                "control": row["control_id"],
                "scope": "inventory",
                "status": row["observation"],
                "reason": row.get("reason", "control was assessed without a conclusive observed value"),
            })
    report_only.sort(key=lambda item: (item.get("group", ""), item.get("control", ""), item.get("reason", "")))
    plan: dict[str, Any] = {
        "schema_version": 4,
        "plan_type": "localsetup.github-repository-plan",
        "target": snapshot["binding"],
        "authorization": snapshot["authorization"],
        "policy_digest": policy_digest(policy),
        "policy": policy,
        "inventory": snapshot["groups"],
        "operations": operations,
        "operation_ids": [operation["id"] for operation in operations],
        "report_only": report_only,
        "local_checkout": snapshot["local_checkout"],
        "local_evidence": snapshot["local_evidence"],
        "control_observations": snapshot["control_observations"],
        "audit_coverage": snapshot["audit_coverage"],
    }
    plan["plan_digest"] = plan_digest(plan)
    return plan


def plan_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# GitHub repository enhancement plan",
        "",
        f"- Target: `{plan['target']['hostname']}/{plan['target']['observed_full_name']}`",
        f"- Repository ID: `{plan['target']['repository_id']}`",
        f"- Actor ID: `{plan['target']['actor_id']}`",
        f"- Policy SHA-256: `{plan['policy_digest']}`",
        f"- Plan SHA-256: `{plan['plan_digest']}`",
        f"- Control coverage: `{plan['audit_coverage']['status']}` ({plan['audit_coverage']['observed_count']}/{plan['audit_coverage']['expected_count']})",
        f"- Checkout binding: `{plan['local_checkout'].get('root_binding', 'unavailable')}`",
        "",
        "## Authorized operation candidates",
        "",
    ]
    if not plan["operations"]:
        lines.append("No writable changes were planned.")
        lines.append("")
    for operation in plan["operations"]:
        lines.extend([
            f"### {operation['group']} / {operation['kind']}",
            "",
            f"- Operation ID: `{operation['id']}`",
            f"- Required permission: {', '.join(operation['required_permissions'])}",
            f"- Risk: `{operation['risk']}`",
            "- Exact interface:",
        ])
        lines.extend(f"    {row}" for row in json.dumps(operation["interface"], ensure_ascii=False, indent=2, sort_keys=True).splitlines())
        lines.extend([
            f"- Verification: {operation['verification']}",
            f"- Recovery: {operation['recovery']}",
            "- Current state:",
        ])
        lines.extend(f"    {row}" for row in json.dumps(operation["current"], ensure_ascii=False, indent=2, sort_keys=True).splitlines())
        lines.append("- Desired state:")
        lines.extend(f"    {row}" for row in json.dumps(operation["desired"], ensure_ascii=False, indent=2, sort_keys=True).splitlines())
        lines.append("")
    lines.extend(["## Report-only findings", ""])
    for item in plan["report_only"]:
        lines.append(f"- **{item.get('group', 'unknown')} / {item.get('control', 'control')}:** {item.get('reason', 'report only')}")
        if item.get("url"):
            lines.append(f"  - Handoff: {item['url']} — {item.get('handoff', '')}")
    if not plan["report_only"]:
        lines.append("No report-only findings.")
    lines.extend(["", "## Control observations", ""])
    for row in plan["control_observations"]:
        state = f"{row['observation']} / {row['capability']} / {row['authority']}"
        reason = f" — {row['reason']}" if row.get("reason") else ""
        lines.append(f"- `{row['control_id']}`: {state}{reason}")
    lines.append("")
    return "\n".join(lines)
