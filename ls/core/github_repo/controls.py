"""Stable per-control inventory for GitHub repository enhancement."""

from __future__ import annotations

from collections import Counter
from typing import Any


CONTROL_GROUPS: dict[str, tuple[str, ...]] = {
    "identity_discovery": (
        "description", "homepage", "topics", "visibility", "default_branch",
        "template_status", "social_preview_state", "feature_links", "license_and_community_files",
    ),
    "collaboration": (
        "issues", "discussions", "projects", "wiki", "issue_forms",
        "pull_request_templates", "funding_links", "merge_methods",
        "default_squash_message", "branch_update_suggestions", "auto_merge",
        "delete_branch_after_merge", "commit_comments", "web_commit_signoff",
    ),
    "git_governance": (
        "branch_rulesets", "tag_rulesets", "effective_branch_rules",
        "legacy_branch_protection", "required_signatures", "deletion_protection",
        "non_fast_forward_protection", "linear_history", "pull_request_requirements",
        "approval_requirements", "resolved_conversations", "required_checks",
        "merge_queue_readiness", "bypass_visibility", "release_tag_protection",
        "required_check_health", "signed_commit_and_tag_evidence",
    ),
    "actions_deployment": (
        "allowed_actions", "sha_pinning", "workflow_token_permissions",
        "pull_request_approval_behavior", "workflow_inventory", "workflow_runs",
        "check_runs", "commit_statuses", "environments", "deployment_status",
        "pages_build_source", "pages_https_enforcement", "pages_custom_domain",
        "pages_health", "workflow_health",
    ),
    "security_supply_chain": (
        "dependency_graph", "sbom", "dependabot_alerts", "automated_security_updates",
        "grouped_dependency_updates", "code_scanning", "secret_scanning",
        "push_protection", "custom_secret_patterns", "secret_validity_checks",
        "private_vulnerability_reporting", "malware_findings", "artifact_attestations",
        "provenance", "security_policy_route",
    ),
    "releases": (
        "immutable_releases", "latest_release_endpoint", "newest_published_release",
        "release_assets", "checksums", "signed_release_checks",
        "reproducible_install_evidence", "release_workflow_status",
    ),
    "repository_content": (
        "readme_presentation", "status_badges", "installation_route", "support_route",
        "contribution_route", "security_reporting_route", "changelog_version_alignment",
        "pages_site_metadata", "open_graph_metadata", "accessibility_inputs",
        "footer_attribution", "social_preview_image_handoff",
    ),
}

CONTROL_IDS = frozenset(
    f"{group}.{control}"
    for group, controls in CONTROL_GROUPS.items()
    for control in controls
)
CONTROL_GROUP_NAMES = frozenset(CONTROL_GROUPS)

REMOTE_CONTROL_GROUPS: dict[str, tuple[str, ...]] = {
    "identity_discovery": CONTROL_GROUPS["identity_discovery"][:-1],
    "collaboration": (
        "issues", "discussions", "projects", "wiki", "merge_methods",
        "default_squash_message", "branch_update_suggestions", "auto_merge",
        "delete_branch_after_merge", "commit_comments", "web_commit_signoff",
    ),
    "git_governance": CONTROL_GROUPS["git_governance"][:-1],
    "actions_deployment": CONTROL_GROUPS["actions_deployment"],
    "security_supply_chain": tuple(
        control for control in CONTROL_GROUPS["security_supply_chain"]
        if control not in {"grouped_dependency_updates", "security_policy_route"}
    ),
    "releases": tuple(
        control for control in CONTROL_GROUPS["releases"]
        if control not in {"signed_release_checks", "reproducible_install_evidence"}
    ),
    "repository_content": (),
}

REMOTE_CONTROL_IDS = frozenset(
    f"{group}.{control}"
    for group, controls in REMOTE_CONTROL_GROUPS.items()
    for control in controls
)

LOCAL_CONTROL_IDS = CONTROL_IDS - REMOTE_CONTROL_IDS

_ENUMS = {
    "applicability": {"applicable", "not_applicable", "unknown"},
    "authority": {"repository", "inherited", "organization_enterprise", "platform", "local", "ui", "unknown"},
    "capability": {"read-write", "read-only", "unsupported", "unknown", "local-workflow", "ui-handoff"},
    "observation": {"observed", "unavailable", "incomplete", "not_applicable", "unknown"},
}
_REQUIRED_FIELDS = {"control_id", "group", "applicability", "authority", "capability", "observation"}


def validate_control_observations(rows: Any, *, expected_ids: frozenset[str] = CONTROL_IDS) -> dict[str, Any]:
    """Validate exact registry coverage and return a deterministic coverage receipt.

    Explicitly unavailable, unknown, or not-applicable outcomes remain assessed
    rows. An absent/duplicate/malformed row or an incomplete remote observation
    means coverage is incomplete.
    """
    if not isinstance(rows, list):
        return {
            "status": "incomplete",
            "expected_count": len(expected_ids),
            "observed_count": 0,
            "missing_control_ids": sorted(expected_ids),
            "duplicate_control_ids": [],
            "invalid_control_ids": [],
            "incomplete_control_ids": [],
        }

    counts: Counter[str] = Counter()
    invalid: set[str] = set()
    incomplete: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not _REQUIRED_FIELDS <= row.keys():
            invalid.add("<malformed-row>")
            continue
        control_id = row.get("control_id")
        if not isinstance(control_id, str) or control_id not in expected_ids:
            invalid.add(control_id if isinstance(control_id, str) else "<malformed-row>")
            continue
        counts[control_id] += 1
        group, separator, name = control_id.partition(".")
        if (
            not separator
            or row.get("group") != group
            or name not in CONTROL_GROUPS.get(group, ())
            or any(row.get(key) not in allowed for key, allowed in _ENUMS.items())
            or (row.get("applicability") == "not_applicable" and row.get("observation") not in {"not_applicable", "unknown"})
            or (row.get("observation") == "not_applicable" and row.get("applicability") != "not_applicable")
            or not any(key in row for key in ("value", "reason", "evidence", "endpoint"))
        ):
            invalid.add(control_id)
        if row.get("observation") == "incomplete" or row.get("pagination") == "incomplete":
            incomplete.add(control_id)
        if row.get("observation") != "observed" and not isinstance(row.get("reason"), str):
            invalid.add(control_id)
        if row.get("capability") in {"unknown", "unsupported"} and not isinstance(row.get("reason"), str):
            invalid.add(control_id)

    duplicates = sorted(control_id for control_id, count in counts.items() if count > 1)
    missing = sorted(expected_ids - set(counts))
    incomplete_ids = sorted(incomplete)
    status = "complete" if not (missing or duplicates or invalid or incomplete_ids) else "incomplete"
    return {
        "status": status,
        "expected_count": len(expected_ids),
        "observed_count": len(rows),
        "missing_control_ids": missing,
        "duplicate_control_ids": duplicates,
        "invalid_control_ids": sorted(invalid),
        "incomplete_control_ids": incomplete_ids,
    }
