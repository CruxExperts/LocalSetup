from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

from .adapter import GitHubAdapter, GitHubError
from .model import RepositoryTarget


def _error_reason(exc: GitHubError) -> str:
    if exc.status in {401, 403}:
        return "authentication_or_permission_unavailable"
    if exc.status == 404:
        return "endpoint_or_feature_not_available"
    if exc.status in {409, 422}:
        return "control_rejected_or_conflicted"
    if exc.status is None:
        return "request_outcome_or_capability_unknown"
    return "remote_read_failed"


def _capture(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"available": True, "value": call()}
    except GitHubError as exc:
        result = {"available": False, "reason": _error_reason(exc)}
        if exc.status is not None:
            result["http_status"] = exc.status
        return result


def _capture_evidence(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"available": True, "value": call()}
    except GitHubError as exc:
        result = {"available": False, "reason": _error_reason(exc), "operation": exc.operation}
        if exc.status is not None:
            result["http_status"] = exc.status
        return result


REMOTE_CONTROL_IDS: dict[str, tuple[str, ...]] = {
    "identity_discovery": (
        "description", "homepage", "topics", "visibility", "default_branch", "template_status",
        "social_preview_state", "feature_links",
    ),
    "collaboration": (
        "issues", "discussions", "projects", "wiki", "merge_methods", "default_squash_message",
        "branch_update_suggestions", "auto_merge", "delete_branch_after_merge", "commit_comments",
        "web_commit_signoff",
    ),
    "git_governance": (
        "branch_rulesets", "tag_rulesets", "effective_branch_rules", "legacy_branch_protection",
        "required_signatures", "deletion_protection", "non_fast_forward_protection", "linear_history",
        "pull_request_requirements", "approval_requirements", "resolved_conversations", "required_checks",
        "merge_queue_readiness", "bypass_visibility", "release_tag_protection", "required_check_health",
    ),
    "actions_deployment": (
        "allowed_actions", "sha_pinning", "workflow_token_permissions", "pull_request_approval_behavior",
        "workflow_inventory", "workflow_runs", "check_runs", "commit_statuses", "environments",
        "deployment_status", "pages_build_source", "pages_https_enforcement", "pages_custom_domain",
        "pages_health", "workflow_health",
    ),
    "security_supply_chain": (
        "dependency_graph", "sbom", "dependabot_alerts", "automated_security_updates", "code_scanning",
        "secret_scanning", "push_protection", "custom_secret_patterns", "secret_validity_checks",
        "private_vulnerability_reporting", "malware_findings", "artifact_attestations", "provenance",
    ),
    "releases": (
        "immutable_releases", "latest_release_endpoint", "newest_published_release", "release_assets",
        "checksums", "release_workflow_status",
    ),
    "repository_content": (),
}


def _control_id(group: str, name: str) -> str:
    return f"{group}.{name}"


def _write_capability(target: RepositoryTarget, writable: bool) -> str:
    if not writable:
        return "read-only"
    return "read-write" if target.hostname.casefold() == "github.com" else "unknown"


def _control_row(
    control_id: str,
    *,
    target: RepositoryTarget,
    observation: str,
    value: Any = None,
    endpoint: str | None = None,
    method: str | None = None,
    http_status: int | None = None,
    reason: str | None = None,
    pagination: str = "not_applicable",
    authority: str = "repository",
    writable: bool = False,
    capability: str | None = None,
) -> dict[str, Any]:
    group, _ = control_id.split(".", 1)
    return {
        "control_id": control_id,
        "group": group,
        "applicability": "applicable",
        "authority": authority,
        "capability": capability or _write_capability(target, writable),
        "observation": observation,
        "value": value,
        "reason": reason,
        "endpoint": endpoint,
        "method": method,
        "http_status": http_status,
        "pagination": pagination,
    }


def _unknown_row(
    control_id: str,
    target: RepositoryTarget,
    reason: str,
    *,
    endpoint: str | None = None,
    method: str | None = None,
    authority: str = "unknown",
    capability: str = "unknown",
    value: Any = None,
) -> dict[str, Any]:
    return _control_row(
        control_id,
        target=target,
        observation="unknown",
        value=value,
        endpoint=endpoint,
        method=method,
        reason=reason,
        authority=authority,
        capability=capability,
    )


def _captured_row(
    control_id: str,
    captured: dict[str, Any],
    target: RepositoryTarget,
    *,
    endpoint: str,
    method: str = "GET",
    writable: bool = False,
    authority: str = "repository",
    reason: str | None = None,
    pagination: str = "not_applicable",
    transform: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    if not captured.get("available"):
        operation = captured.get("operation")
        status = captured.get("http_status")
        incomplete = isinstance(operation, str) and "pagination_limit" in operation
        if status is None and not incomplete:
            incomplete = True
        return _control_row(
            control_id,
            target=target,
            observation="incomplete" if incomplete else "unavailable",
            endpoint=endpoint,
            method=method,
            http_status=status,
            reason=captured.get("reason", "remote_read_unavailable"),
            pagination="incomplete" if incomplete else pagination,
            authority=authority,
            writable=writable,
        )

    raw = captured.get("value")
    paged = False
    if isinstance(raw, dict) and isinstance(raw.get("pagination"), dict) and "value" in raw:
        page_info = raw["pagination"]
        raw = raw["value"]
        paged = True
        pagination = "complete" if page_info.get("complete") is True else "incomplete"
    elif pagination == "complete":
        paged = True
    observation = "incomplete" if pagination == "incomplete" else "observed"
    if transform is not None:
        raw = transform(raw)
    if observation == "incomplete":
        incomplete_reason = "collection_pagination_incomplete"
        reason = f"{reason}:{incomplete_reason}" if reason else incomplete_reason
    return _control_row(
        control_id,
        target=target,
        observation=observation,
        value=raw,
        endpoint=endpoint,
        method=method,
        reason=reason,
        pagination=pagination if paged else "not_applicable",
        authority=authority,
        writable=writable,
    )


def _optional_capture(adapter: GitHubAdapter, method_name: str, *args: Any) -> dict[str, Any]:
    method = getattr(adapter, method_name, None)
    if not callable(method):
        return {"available": False, "reason": "typed_read_method_not_available", "operation": method_name}
    return _capture_evidence(lambda: method(*args))


def _safe_ruleset_row(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: row[key]
        for key in ("id", "name", "target", "source_type", "source", "enforcement")
        if isinstance(row.get(key), (str, int)) and not isinstance(row.get(key), bool)
    }
    rules = row.get("rules") if isinstance(row.get("rules"), list) else []
    if row.get("source_type") == "Repository":
        result["details_available"] = row.get("_details_available") is True
        if row.get("_details_available") is not True:
            result["detail_reason"] = row.get("_details_reason", "repository_ruleset_details_not_returned")
    else:
        result["details_available"] = bool(rules)
        if not rules:
            result["detail_reason"] = "parent_ruleset_list_did_not_include_full_rule_details"
    rule_types: list[str] = []
    required_checks: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            continue
        rule_types.append(rule["type"])
        params = rule.get("parameters")
        checks = params.get("required_status_checks") if isinstance(params, dict) else None
        if isinstance(checks, list):
            required_checks.update(
                item["context"] for item in checks
                if isinstance(item, dict) and isinstance(item.get("context"), str)
            )
    result["rule_types"] = sorted(set(rule_types))
    if required_checks:
        result["required_check_contexts"] = sorted(required_checks)
    bypass = row.get("bypass_actors") if isinstance(row.get("bypass_actors"), list) else None
    if bypass is not None:
        result["bypass_actor_count"] = len(bypass)
        result["bypass_actor_types"] = sorted({
            actor.get("actor_type") for actor in bypass
            if isinstance(actor, dict) and isinstance(actor.get("actor_type"), str)
        })
    return result


def _safe_effective_rule(row: dict[str, Any]) -> dict[str, Any]:
    result = {key: row[key] for key in ("type", "source_type", "source", "enforcement") if isinstance(row.get(key), str)}
    params = row.get("parameters")
    if isinstance(params, dict):
        checks = params.get("required_status_checks")
        if isinstance(checks, list):
            result["required_check_contexts"] = sorted({
                item["context"] for item in checks
                if isinstance(item, dict) and isinstance(item.get("context"), str)
            })
        for key in ("required_approving_review_count", "dismiss_stale_reviews_on_push", "require_code_owner_review", "require_last_push_approval"):
            if isinstance(params.get(key), (str, int, bool)):
                result[key] = params[key]
    return result


def _safe_legacy_protection(value: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    status_checks = value.get("required_status_checks")
    if isinstance(status_checks, dict):
        contexts = status_checks.get("contexts")
        if isinstance(contexts, list):
            output["required_check_contexts"] = sorted(item for item in contexts if isinstance(item, str))
        output["strict"] = status_checks.get("strict") if isinstance(status_checks.get("strict"), bool) else None
    reviews = value.get("required_pull_request_reviews")
    if isinstance(reviews, dict):
        for key in ("required_approving_review_count", "dismiss_stale_reviews", "require_code_owner_reviews", "require_last_push_approval"):
            if isinstance(reviews.get(key), (int, bool)):
                output[key] = reviews[key]
    for key in ("enforce_admins", "allow_force_pushes", "allow_deletions", "required_conversation_resolution"):
        field = value.get(key)
        if isinstance(field, dict) and isinstance(field.get("enabled"), bool):
            output[key] = field["enabled"]
    restrictions = value.get("restrictions")
    if isinstance(restrictions, dict):
        output["restriction_counts"] = {
            key: len(restrictions[key]) for key in ("users", "teams", "apps") if isinstance(restrictions.get(key), list)
        }
    return output


def _ruleset_values(rows: list[dict[str, Any]], target_kind: str) -> tuple[list[dict[str, Any]], str]:
    selected = [_safe_ruleset_row(row) for row in rows if row.get("target") == target_kind]
    source_types = {
        str(row.get("source_type", "")).casefold()
        for row in rows if row.get("target") == target_kind and isinstance(row.get("source_type"), str)
    }
    if source_types and source_types <= {"repository"}:
        authority = "repository"
    elif source_types and source_types <= {"organization", "enterprise"}:
        authority = "inherited"
    elif source_types:
        authority = "unknown"
    else:
        authority = "repository"
    return selected, authority


def _branch_policy_evidence(
    *,
    rulesets: list[dict[str, Any]],
    effective: dict[str, Any],
    legacy: dict[str, Any],
) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    if rulesets.get("available"):
        source_rows = rulesets.get("value")
        if isinstance(source_rows, list):
            sources["rulesets"] = [_safe_ruleset_row(row) for row in source_rows if row.get("target") == "branch"]
    if effective.get("available") and isinstance(effective.get("value"), list):
        sources["effective"] = [_safe_effective_rule(row) for row in effective["value"]]
    if legacy.get("available") and isinstance(legacy.get("value"), dict):
        sources["legacy"] = _safe_legacy_protection(legacy["value"])
    return sources


def _policy_authority(evidence: dict[str, Any]) -> str:
    sources: set[str] = set()
    if "legacy" in evidence:
        sources.add("repository")
    for key in ("rulesets", "effective"):
        rows = evidence.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_type = row.get("source_type")
            if source_type == "Repository":
                sources.add("repository")
            elif source_type in {"Organization", "Enterprise"}:
                sources.add("inherited")
    if sources == {"repository"}:
        return "repository"
    if sources == {"inherited"}:
        return "inherited"
    return "unknown"


def _reported_policy_value(name: str, evidence: dict[str, Any]) -> dict[str, Any]:
    relevant_types = {
        "deletion_protection": {"deletion"},
        "non_fast_forward_protection": {"non_fast_forward"},
        "linear_history": {"required_linear_history"},
        "pull_request_requirements": {"pull_request"},
        "approval_requirements": {"required_approving_review_count", "pull_request"},
        "resolved_conversations": {"required_review_thread_resolution"},
        "required_checks": {"required_status_checks"},
        "bypass_visibility": set(),
    }[name]
    output: dict[str, Any] = {}
    for source_name, rows in evidence.items():
        if isinstance(rows, list):
            selected = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if source_name == "legacy":
                    selected.append(row)
                    continue
                row_types = row.get("rule_types") if isinstance(row.get("rule_types"), list) else []
                if (
                    row.get("type") in relevant_types
                    or relevant_types.intersection(item for item in row_types if isinstance(item, str))
                    or (name == "bypass_visibility" and "bypass_actor_count" in row)
                ):
                    selected.append(row)
            output[source_name] = selected
        elif source_name == "legacy" and isinstance(rows, dict):
            if name in {"deletion_protection", "non_fast_forward_protection", "resolved_conversations"}:
                key = {
                    "deletion_protection": "allow_deletions",
                    "non_fast_forward_protection": "allow_force_pushes",
                    "resolved_conversations": "required_conversation_resolution",
                }[name]
                if key in rows:
                    output[source_name] = {key: rows[key]}
            elif name in {"pull_request_requirements", "approval_requirements"}:
                review_fields = {
                    key: value for key, value in rows.items()
                    if key in {
                        "required_approving_review_count", "dismiss_stale_reviews", "require_code_owner_reviews",
                        "require_last_push_approval",
                    }
                }
                if review_fields:
                    output[source_name] = review_fields
            elif name == "required_checks":
                checks = {key: value for key, value in rows.items() if key in {"required_check_contexts", "strict"}}
                if checks:
                    output[source_name] = checks
            elif name == "bypass_visibility" and "restriction_counts" in rows:
                output[source_name] = {"restriction_counts": rows["restriction_counts"]}
    return output


def _build_control_observations(
    adapter: GitHubAdapter,
    target: RepositoryTarget,
    repo: dict[str, Any],
    captures: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    repo_endpoint = f"/repos/{quote(target.owner, safe='')}/{quote(target.repository, safe='')}"
    rows: dict[str, dict[str, Any]] = {}

    def field(
        name: str,
        key: str,
        *,
        writable: bool = False,
        capability: str | None = None,
        reason: str | None = None,
    ) -> None:
        control_id = _control_id("identity_discovery", name)
        if key not in repo:
            rows[control_id] = _unknown_row(
                control_id, target, reason or f"repository_metadata_field_{key}_not_returned",
                endpoint=repo_endpoint, method="GET",
                capability=capability or (_write_capability(target, writable) if writable else "unknown"),
            )
            return
        rows[control_id] = _control_row(
            control_id, target=target, observation="observed", value=repo[key],
            endpoint=repo_endpoint, method="GET", writable=writable, capability=capability,
        )

    # Repository identity, metadata and feature discovery.
    for name, key in (("description", "description"), ("homepage", "homepage"), ("visibility", "visibility"), ("default_branch", "default_branch"), ("template_status", "is_template")):
        field(name, key, writable=True)
    topic_capture = captures["topics"]
    rows["identity_discovery.topics"] = _captured_row(
        "identity_discovery.topics", topic_capture, target,
        endpoint=f"{repo_endpoint}/topics", writable=True,
    )
    social = captures["social_preview"]
    rows["identity_discovery.social_preview_state"] = _captured_row(
        "identity_discovery.social_preview_state", social, target,
        endpoint="graphql repository.usesCustomOpenGraphImage", method="POST",
        reason="boolean_only_does_not_expose_image_bytes_or_content",
    )
    link_fields = ("issues_url", "pulls_url", "projects_url", "wiki_url", "discussions_url", "homepage")
    present_fields = {key: isinstance(repo.get(key), str) and bool(repo.get(key)) for key in link_fields if key in repo}
    if present_fields:
        rows["identity_discovery.feature_links"] = _control_row(
            "identity_discovery.feature_links", target=target, observation="observed",
            value={"url_field_presence": present_fields, "validity_checked": False},
            endpoint=repo_endpoint, method="GET",
            reason="presence_only_no_link_validity_claim",
        )
    else:
        rows["identity_discovery.feature_links"] = _unknown_row(
            "identity_discovery.feature_links", target, "repository_response_has_no_known_feature_link_fields",
            endpoint=repo_endpoint, method="GET",
        )

    # Collaboration and merge preferences.
    for name, key, writable in (
        ("issues", "has_issues", True),
        ("discussions", "has_discussions", False),
        ("projects", "has_projects", True),
        ("wiki", "has_wiki", True),
        ("auto_merge", "allow_auto_merge", True),
        ("delete_branch_after_merge", "delete_branch_on_merge", True),
    ):
        control_id = _control_id("collaboration", name)
        if key in repo:
            rows[control_id] = _control_row(
                control_id, target=target, observation="observed", value=repo[key],
                endpoint=repo_endpoint, method="GET", writable=writable,
                capability="unknown" if name == "discussions" else None,
                reason="repository_read_is_available_but_rest_update_field_is_unresolved" if name == "discussions" else None,
            )
        else:
            rows[control_id] = _unknown_row(
                control_id, target, f"repository_metadata_field_{key}_not_returned",
                endpoint=repo_endpoint, method="GET",
                capability="unknown" if name == "discussions" else _write_capability(target, writable),
            )
    merge_keys = ("allow_squash_merge", "allow_merge_commit", "allow_rebase_merge")
    squash_keys = ("squash_merge_commit_title", "squash_merge_commit_message")
    for name, keys in (("merge_methods", merge_keys), ("default_squash_message", squash_keys)):
        control_id = _control_id("collaboration", name)
        value = {key: repo[key] for key in keys if key in repo}
        if value:
            rows[control_id] = _control_row(
                control_id, target=target, observation="observed", value=value,
                endpoint=repo_endpoint, method="GET", writable=True,
            )
        else:
            rows[control_id] = _unknown_row(
                control_id, target, f"repository_metadata_fields_{'_'.join(keys)}_not_returned",
                endpoint=repo_endpoint, method="GET", capability=_write_capability(target, True),
            )
    rows["collaboration.branch_update_suggestions"] = _unknown_row(
        "collaboration.branch_update_suggestions", target,
        "repository_update_suggestion_read_field_not_resolved_in_official_matrix",
        endpoint=repo_endpoint, method="GET",
    )
    rows["collaboration.commit_comments"] = _unknown_row(
        "collaboration.commit_comments", target,
        "repository_commit_comment_policy_field_not_resolved_in_official_matrix",
        endpoint=repo_endpoint, method="GET",
    )
    if isinstance(repo.get("web_commit_signoff_required"), bool):
        rows["collaboration.web_commit_signoff"] = _control_row(
            "collaboration.web_commit_signoff", target=target, observation="observed",
            value=repo["web_commit_signoff_required"], endpoint=repo_endpoint, method="GET", writable=True,
        )
    else:
        rows["collaboration.web_commit_signoff"] = _unknown_row(
            "collaboration.web_commit_signoff", target,
            "web_commit_signoff_required_field_not_returned_by_repository_read",
            endpoint=repo_endpoint, method="GET", capability=_write_capability(target, True),
        )

    # Rule collections preserve repository versus inherited provenance and redact actor identities.
    ruleset_capture = captures["rulesets"]
    ruleset_rows = ruleset_capture.get("value") if ruleset_capture.get("available") else None
    if isinstance(ruleset_rows, list):
        for kind, name in (("branch", "branch_rulesets"), ("tag", "tag_rulesets")):
            selected, authority = _ruleset_values(ruleset_rows, kind)
            rows[_control_id("git_governance", name)] = _control_row(
                _control_id("git_governance", name), target=target, observation="observed",
                value=selected, endpoint=f"{repo_endpoint}/rulesets?includes_parents=true", method="GET",
                pagination="complete", authority=authority, writable=True,
                capability="read-only" if authority == "inherited" else None,
                reason="source_type_identifies_repository_or_inherited_rules",
            )
    else:
        for name in ("branch_rulesets", "tag_rulesets"):
            rows[_control_id("git_governance", name)] = _captured_row(
                _control_id("git_governance", name), ruleset_capture, target,
                endpoint=f"{repo_endpoint}/rulesets?includes_parents=true", writable=True,
            )

    default_branch = repo.get("default_branch")
    branch = default_branch if isinstance(default_branch, str) and default_branch else None
    branch_endpoint = f"{repo_endpoint}/rules/branches/{quote(branch, safe='')}" if branch else f"{repo_endpoint}/rules/branches/<default>"
    legacy_endpoint = f"{repo_endpoint}/branches/{quote(branch, safe='')}/protection" if branch else f"{repo_endpoint}/branches/<default>/protection"
    signatures_endpoint = f"{legacy_endpoint}/required_signatures"
    if branch:
        effective = _optional_capture(adapter, "effective_branch_rules", branch)
        legacy = _optional_capture(adapter, "legacy_branch_protection", branch)
        signatures = _optional_capture(adapter, "required_signatures", branch)
        head = _optional_capture(adapter, "branch_head_sha", branch)
    else:
        effective = legacy = signatures = head = {
            "available": False, "reason": "repository_default_branch_not_observed", "operation": "default_branch"
        }
    rows["git_governance.effective_branch_rules"] = _captured_row(
        "git_governance.effective_branch_rules", effective, target,
        endpoint=branch_endpoint, writable=False,
        authority=(
            "repository"
            if effective.get("available") and all(
                row.get("source_type") == "Repository"
                for row in effective.get("value", []) if isinstance(row, dict)
            )
            else "inherited"
            if effective.get("available") and effective.get("value") and all(
                row.get("source_type") in {"Organization", "Enterprise"}
                for row in effective.get("value", []) if isinstance(row, dict)
            )
            else "unknown"
        ),
        transform=lambda value: [_safe_effective_rule(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else None,
    )
    rows["git_governance.legacy_branch_protection"] = _captured_row(
        "git_governance.legacy_branch_protection", legacy, target,
        endpoint=legacy_endpoint, writable=True,
        transform=lambda value: _safe_legacy_protection(value) if isinstance(value, dict) else None,
    )
    rows["git_governance.required_signatures"] = _captured_row(
        "git_governance.required_signatures", signatures, target,
        endpoint=signatures_endpoint, writable=True,
    )
    policy_evidence = _branch_policy_evidence(rulesets=ruleset_capture, effective=effective, legacy=legacy)
    branch_policy_authority = _policy_authority(policy_evidence)
    policy_routes = {
        "deletion_protection": f"{repo_endpoint}/rulesets and {legacy_endpoint}",
        "non_fast_forward_protection": f"{repo_endpoint}/rulesets and {legacy_endpoint}",
        "linear_history": f"{repo_endpoint}/rulesets and {legacy_endpoint}",
        "pull_request_requirements": f"{repo_endpoint}/rulesets and {legacy_endpoint}",
        "approval_requirements": f"{repo_endpoint}/rulesets and {legacy_endpoint}",
        "resolved_conversations": f"{repo_endpoint}/rulesets and {legacy_endpoint}",
        "required_checks": f"{repo_endpoint}/rulesets and {legacy_endpoint}",
        "bypass_visibility": f"{repo_endpoint}/rulesets and {legacy_endpoint}",
    }
    for name in ("deletion_protection", "non_fast_forward_protection", "linear_history", "pull_request_requirements", "approval_requirements", "resolved_conversations", "required_checks", "bypass_visibility"):
        control_id = _control_id("git_governance", name)
        if policy_evidence:
            value = _reported_policy_value(name, policy_evidence)
            rows[control_id] = _control_row(
                control_id, target=target, observation="observed" if value else "unknown",
                value=value, endpoint=policy_routes[name], method="GET", writable=True,
                authority=branch_policy_authority,
                capability=(
                    "read-only" if branch_policy_authority == "inherited"
                    else _write_capability(target, True)
                ),
                reason="source_rows_preserved_without_claiming_absence_from_unavailable_endpoint" if value else "no_visible_rules_for_control",
            )
        else:
            rows[control_id] = _unknown_row(
                control_id, target, "branch_protection_sources_unavailable_or_not_resolved",
                endpoint=policy_routes[name], method="GET", capability=_write_capability(target, True),
            )
    rows["git_governance.merge_queue_readiness"] = _unknown_row(
        "git_governance.merge_queue_readiness", target,
        "graphql_merge_queue_permission_mapping_unresolved",
        endpoint="graphql Repository.mergeQueue(branch)", method="POST",
    )
    tag_rules = ruleset_capture
    tag_rows = ruleset_rows if isinstance(ruleset_rows, list) else None
    if tag_rows is not None:
        safe_tags, tag_authority = _ruleset_values(tag_rows, "tag")
        rows["git_governance.release_tag_protection"] = _control_row(
            "git_governance.release_tag_protection", target=target, observation="observed",
            value=safe_tags, endpoint=f"{repo_endpoint}/rulesets?includes_parents=true",
            method="GET", pagination="complete", authority=tag_authority, writable=True,
            capability="read-only" if tag_authority == "inherited" else None,
        )
        bypass_values = [item for item in safe_tags if item.get("bypass_actor_count") is not None]
        if bypass_values:
            branch_bypass = rows.get("git_governance.bypass_visibility")
            combined_bypass = {"tag_rulesets": bypass_values}
            if isinstance(branch_bypass, dict) and isinstance(branch_bypass.get("value"), dict):
                combined_bypass.update(branch_bypass["value"])
            branch_authority = branch_bypass.get("authority") if isinstance(branch_bypass, dict) else None
            bypass_authority = (
                tag_authority
                if branch_authority is None or branch_authority == tag_authority
                else "unknown"
            )
            rows["git_governance.bypass_visibility"] = _control_row(
                "git_governance.bypass_visibility", target=target, observation="observed",
                value=combined_bypass, endpoint=f"{repo_endpoint}/rulesets?includes_parents=true",
                method="GET", pagination="complete", authority=bypass_authority, writable=True,
                capability="read-only" if bypass_authority == "inherited" else None,
                reason="actor_ids_and_names_redacted",
            )
    else:
        rows["git_governance.release_tag_protection"] = _captured_row(
            "git_governance.release_tag_protection", tag_rules, target,
            endpoint=f"{repo_endpoint}/rulesets?includes_parents=true", writable=True,
        )
    if "git_governance.bypass_visibility" not in rows:
        rows["git_governance.bypass_visibility"] = _unknown_row(
            "git_governance.bypass_visibility", target,
            "ruleset_bypass_actor_details_not_visible_or_not_returned",
            endpoint=f"{repo_endpoint}/rulesets?includes_parents=true",
            method="GET", capability=_write_capability(target, True),
        )

    # Actions, workflow and deployment observations.
    actions_value = captures["actions"].get("value") if captures["actions"].get("available") else {}
    actions_value = actions_value if isinstance(actions_value, dict) else {}
    workflow_value = captures["workflow_permissions"].get("value") if captures["workflow_permissions"].get("available") else {}
    workflow_value = workflow_value if isinstance(workflow_value, dict) else {}
    selected_value = captures["selected_actions"].get("value") if captures["selected_actions"].get("available") else {}
    selected_value = selected_value if isinstance(selected_value, dict) else {}
    for name, value, endpoint, reason in (
        ("allowed_actions", {key: actions_value[key] for key in ("enabled", "allowed_actions") if key in actions_value} | ({"selected_actions": selected_value} if selected_value else {}), f"{repo_endpoint}/actions/permissions", None),
        ("sha_pinning", actions_value.get("sha_pinning_required"), f"{repo_endpoint}/actions/permissions", "sha_pinning_field_not_returned" if "sha_pinning_required" not in actions_value else None),
        ("workflow_token_permissions", workflow_value, f"{repo_endpoint}/actions/permissions/workflow", None),
        ("pull_request_approval_behavior", workflow_value.get("can_approve_pull_request_reviews"), f"{repo_endpoint}/actions/permissions/workflow", "can_approve_pull_request_reviews_not_returned" if "can_approve_pull_request_reviews" not in workflow_value else None),
    ):
        cid = _control_id("actions_deployment", name)
        if not value and name in {"allowed_actions", "workflow_token_permissions"}:
            rows[cid] = _captured_row(cid, captures["actions"] if name == "allowed_actions" else captures["workflow_permissions"], target, endpoint=endpoint, writable=True, reason=reason)
        elif reason is not None:
            rows[cid] = _unknown_row(cid, target, reason, endpoint=endpoint, method="GET", capability=_write_capability(target, True))
        else:
            rows[cid] = _control_row(cid, target=target, observation="observed", value=value, endpoint=endpoint, method="GET", writable=True)

    workflow_inventory = _optional_capture(adapter, "workflow_inventory")
    workflow_runs = _optional_capture(adapter, "workflow_runs_summary", branch) if branch else {"available": False, "reason": "default_branch_unavailable", "operation": "workflow_runs"}
    rows["actions_deployment.workflow_inventory"] = _captured_row(
        "actions_deployment.workflow_inventory", workflow_inventory, target,
        endpoint=f"{repo_endpoint}/actions/workflows", pagination="complete",
    )
    rows["actions_deployment.workflow_runs"] = _captured_row(
        "actions_deployment.workflow_runs", workflow_runs, target,
        endpoint=f"{repo_endpoint}/actions/runs?branch=<default>", pagination="complete",
    )
    branch_sha = head.get("value") if head.get("available") else None
    check_runs = _optional_capture(adapter, "check_runs_summary", branch_sha) if isinstance(branch_sha, str) else {"available": False, "reason": "default_branch_commit_unavailable", "operation": "check_runs"}
    commit_statuses = _optional_capture(adapter, "commit_status_summary", branch_sha) if isinstance(branch_sha, str) else {"available": False, "reason": "default_branch_commit_unavailable", "operation": "commit_statuses"}
    rows["actions_deployment.check_runs"] = _captured_row(
        "actions_deployment.check_runs", check_runs, target,
        endpoint=f"{repo_endpoint}/commits/<default-head>/check-runs", pagination="complete",
        authority="platform",
    )
    rows["actions_deployment.commit_statuses"] = _captured_row(
        "actions_deployment.commit_statuses", commit_statuses, target,
        endpoint=f"{repo_endpoint}/commits/<default-head>/status and /statuses", pagination="complete",
        authority="platform",
    )
    signal_pagination_incomplete = any(
        isinstance(captured.get("value"), dict)
        and isinstance(captured["value"].get("pagination"), dict)
        and captured["value"]["pagination"].get("complete") is False
        for captured in (check_runs, commit_statuses)
    )
    signal_read_unknown = any(
        not captured.get("available") and captured.get("http_status") is None
        for captured in (check_runs, commit_statuses)
    )
    rows["git_governance.required_check_health"] = _control_row(
        "git_governance.required_check_health", target=target,
        observation=(
            "incomplete" if signal_pagination_incomplete
            else "observed" if check_runs.get("available") and commit_statuses.get("available")
            else "incomplete" if signal_read_unknown
            else "unavailable"
        ),
        value={
            "head_sha_observed": isinstance(branch_sha, str),
            "check_runs": check_runs.get("value") if check_runs.get("available") else None,
            "commit_statuses": commit_statuses.get("value") if commit_statuses.get("available") else None,
            "assessment": "not_assessed_without_correlation_to_effective_required_contexts",
        },
        endpoint=f"{repo_endpoint}/commits/<default-head>/check-runs and /status",
        method="GET", reason="signal_inventory_does_not_establish_required_check_health",
        pagination="incomplete" if signal_pagination_incomplete else "not_applicable",
        capability="read-only", authority="platform",
    )
    envs = _optional_capture(adapter, "environments_summary")
    deployments = _optional_capture(adapter, "deployments_summary")
    rows["actions_deployment.environments"] = _captured_row(
        "actions_deployment.environments", envs, target,
        endpoint=f"{repo_endpoint}/deployments/environments", pagination="complete",
    )
    rows["actions_deployment.deployment_status"] = _captured_row(
        "actions_deployment.deployment_status", deployments, target,
        endpoint=f"{repo_endpoint}/deployments and /deployments/{{id}}/statuses", pagination="complete",
        authority="platform",
    )
    pages = _optional_capture(adapter, "pages_read")
    page_value = pages.get("value") if pages.get("available") else None
    if isinstance(page_value, dict):
        page_map = {
            "pages_build_source": "build_type",
            "pages_https_enforcement": "https_enforced",
            "pages_custom_domain": "cname",
        }
        for name, key in page_map.items():
            cid = _control_id("actions_deployment", name)
            if key in page_value:
                rows[cid] = _control_row(cid, target=target, observation="observed", value=page_value[key], endpoint=f"{repo_endpoint}/pages", method="GET", writable=True)
            else:
                rows[cid] = _unknown_row(cid, target, f"pages_response_field_{key}_not_returned", endpoint=f"{repo_endpoint}/pages", method="GET", capability=_write_capability(target, True))
    else:
        for name in ("pages_build_source", "pages_https_enforcement", "pages_custom_domain"):
            rows[_control_id("actions_deployment", name)] = _captured_row(
                _control_id("actions_deployment", name), pages, target,
                endpoint=f"{repo_endpoint}/pages", writable=True,
            )
    pages_health = _optional_capture(adapter, "pages_health")
    rows["actions_deployment.pages_health"] = _captured_row(
        "actions_deployment.pages_health", pages_health, target,
        endpoint=f"{repo_endpoint}/pages/health", method="GET", writable=False,
        reason="async_dns_observation_does_not_prove_deployed_site_health",
        authority="platform",
    )
    rows["actions_deployment.workflow_health"] = _captured_row(
        "actions_deployment.workflow_health", workflow_runs, target,
        endpoint=f"{repo_endpoint}/actions/runs?branch=<default>",
        reason="run_status_inventory_is_not_a_workflow_quality_or_health_assessment",
        authority="platform",
    )

    # Security controls expose only feature status and aggregate counts; never alert or pattern detail.
    security_status = repo.get("security_and_analysis")
    security_status = security_status if isinstance(security_status, dict) else {}
    def setting_status(*keys: str) -> Any:
        for key in keys:
            item = security_status.get(key)
            if isinstance(item, dict) and item.get("status") in {"enabled", "disabled"}:
                return item["status"]
        return None
    dep_graph = setting_status("dependency_graph")
    if dep_graph is None:
        rows["security_supply_chain.dependency_graph"] = _unknown_row(
            "security_supply_chain.dependency_graph", target,
            "dependency_graph_setting_not_returned_or_not_visible_to_reader",
            endpoint=repo_endpoint, method="GET", capability=_write_capability(target, True),
        )
    else:
        rows["security_supply_chain.dependency_graph"] = _control_row(
            "security_supply_chain.dependency_graph", target=target, observation="observed",
            value=dep_graph, endpoint=repo_endpoint, method="GET", writable=True,
        )
    sbom = _optional_capture(adapter, "sbom_summary")
    rows["security_supply_chain.sbom"] = _captured_row(
        "security_supply_chain.sbom", sbom, target,
        endpoint=f"{repo_endpoint}/dependency-graph/sbom", method="GET",
        reason="package_count_only_full_sbom_content_redacted",
    )
    dep_count = captures["dependabot_count"]
    dep_enabled = captures["vulnerability_alerts"]
    if dep_enabled.get("available"):
        dep_count_complete = dep_count.get("available") is True
        dep_count_pagination = (
            "complete" if dep_count_complete
            else "incomplete" if "pagination_limit" in str(dep_count.get("operation", ""))
            else "not_applicable"
        )
        rows["security_supply_chain.dependabot_alerts"] = _control_row(
            "security_supply_chain.dependabot_alerts", target=target,
            observation="observed" if dep_count_complete else "incomplete",
            value={
                "enabled": dep_enabled.get("value"),
                "open_alert_count": dep_count.get("value") if dep_count.get("available") else None,
                "alert_count_observation": "observed" if dep_count.get("available") else dep_count.get("reason"),
                "health_assessment": "not_assessed",
            },
            endpoint=f"{repo_endpoint}/vulnerability-alerts and /dependabot/alerts?state=open",
            method="GET", writable=True,
            reason="aggregate_count_does_not_establish_alert_triage_or_scanning_health",
            pagination=dep_count_pagination,
        )
    else:
        rows["security_supply_chain.dependabot_alerts"] = _captured_row(
            "security_supply_chain.dependabot_alerts", dep_enabled, target,
            endpoint=f"{repo_endpoint}/vulnerability-alerts", writable=True,
        )
    automated = captures["automated_security"]
    rows["security_supply_chain.automated_security_updates"] = _captured_row(
        "security_supply_chain.automated_security_updates", automated, target,
        endpoint=f"{repo_endpoint}/automated-security-fixes", writable=True,
    )
    code_count = captures["code_scanning_count"]
    secret_count = _optional_capture(adapter, "secret_scanning_alert_count")
    code_security = setting_status("code_security", "advanced_security")
    code_count_complete = code_count.get("available") is True
    code_count_pagination = (
        "complete" if code_count_complete
        else "incomplete" if "pagination_limit" in str(code_count.get("operation", ""))
        else "not_applicable"
    )
    rows["security_supply_chain.code_scanning"] = _control_row(
        "security_supply_chain.code_scanning", target=target,
        observation=(
            "incomplete" if code_security is not None and not code_count_complete
            else "observed" if code_security is not None or code_count_complete
            else "unavailable"
        ),
        value={
            "feature_status": code_security,
            "open_alert_count": code_count.get("value") if code_count.get("available") else None,
            "alert_count_observation": "observed" if code_count.get("available") else code_count.get("reason"),
            "health_assessment": "not_assessed",
        },
        endpoint=f"{repo_endpoint} security_and_analysis and /code-scanning/alerts?state=open",
        method="GET", writable=True,
        reason="alert_count_does_not_prove_analysis_success_or_feature_entitlement",
        pagination=code_count_pagination,
    )
    for name, keys in (
        ("secret_scanning", ("secret_scanning",)),
        ("push_protection", ("secret_scanning_push_protection",)),
    ):
        status = setting_status(*keys)
        cid = _control_id("security_supply_chain", name)
        if status is None:
            rows[cid] = _unknown_row(
                cid, target, f"security_setting_{keys[0]}_not_returned_or_not_visible",
                endpoint=repo_endpoint, method="GET", capability=_write_capability(target, True),
            )
        else:
            if name == "secret_scanning":
                incomplete = not secret_count.get("available")
                rows[cid] = _control_row(
                    cid, target=target,
                    observation="incomplete" if incomplete else "observed",
                    value={
                        "feature_status": status,
                        "open_alert_count": secret_count.get("value") if secret_count.get("available") else None,
                        "alert_count_observation": "observed" if secret_count.get("available") else secret_count.get("reason"),
                        "health_assessment": "not_assessed",
                    },
                    endpoint=f"{repo_endpoint} security_and_analysis and /secret-scanning/alerts?state=open",
                    method="GET", writable=True,
                    reason="alert_count_does_not_establish_triage_or_scanning_health" if incomplete else None,
                    pagination=(
                        "complete" if secret_count.get("available")
                        else "incomplete" if secret_count.get("operation", "").endswith("pagination_limit")
                        else "not_applicable"
                    ),
                )
            else:
                rows[cid] = _control_row(cid, target=target, observation="observed", value=status, endpoint=repo_endpoint, method="GET", writable=True)
    patterns = _optional_capture(adapter, "custom_secret_pattern_count")
    rows["security_supply_chain.custom_secret_patterns"] = _captured_row(
        "security_supply_chain.custom_secret_patterns", patterns, target,
        endpoint=f"{repo_endpoint}/secret-scanning/custom-patterns", pagination="complete",
        reason="pattern_names_and_contents_redacted",
    )
    rows["security_supply_chain.secret_validity_checks"] = _unknown_row(
        "security_supply_chain.secret_validity_checks", target,
        "validity_check_rest_field_and_endpoint_mapping_unresolved",
        endpoint=repo_endpoint, method="GET",
    )
    rows["security_supply_chain.private_vulnerability_reporting"] = _captured_row(
        "security_supply_chain.private_vulnerability_reporting", captures["private_vulnerability_reporting"], target,
        endpoint=f"{repo_endpoint}/private-vulnerability-reporting", writable=True,
    )
    for name, reason in (
        ("malware_findings", "dependabot_malware_classification_is_not_safely_exposed_as_an_aggregate_field"),
        ("artifact_attestations", "artifact_attestation_verification_requires_a_local_artifact_and_trusted_builder"),
        ("provenance", "provenance_requires_artifact_specific_verification_against_a_trusted_workflow"),
    ):
        rows[_control_id("security_supply_chain", name)] = _unknown_row(
            _control_id("security_supply_chain", name), target, reason,
            authority="unknown", capability="local-workflow",
        )

    # Releases: distinguish the dedicated latest endpoint from newest publication ordering.
    immutable = captures["immutable"]
    rows["releases.immutable_releases"] = _captured_row(
        "releases.immutable_releases", immutable, target,
        endpoint=f"{repo_endpoint}/immutable-releases", writable=True,
    )
    latest = _optional_capture(adapter, "latest_release_summary")
    rows["releases.latest_release_endpoint"] = _captured_row(
        "releases.latest_release_endpoint", latest, target,
        endpoint=f"{repo_endpoint}/releases/latest",
        reason="dedicated_endpoint_excludes_drafts_and_prereleases",
    )
    inventory = _optional_capture(adapter, "release_inventory")
    rows["releases.newest_published_release"] = _captured_row(
        "releases.newest_published_release", inventory, target,
        endpoint=f"{repo_endpoint}/releases", pagination="complete",
        reason="newest_published_at_is_reported_separately_from_dedicated_latest_endpoint",
    )
    assets = _optional_capture(adapter, "release_assets_summary")
    rows["releases.release_assets"] = _captured_row(
        "releases.release_assets", assets, target,
        endpoint=f"{repo_endpoint}/releases/{{id}}/assets", pagination="complete",
        reason="asset_names_and_download_urls_redacted",
    )
    if assets.get("available"):
        assets_value = assets["value"].get("value") if isinstance(assets.get("value"), dict) else None
        assets_page_complete = (
            isinstance(assets["value"].get("pagination"), dict)
            and assets["value"]["pagination"].get("complete") is True
        )
        rows["releases.checksums"] = _control_row(
            "releases.checksums", target=target, observation="observed",
            value={
                "asset_digest_count": assets_value.get("asset_digest_count") if isinstance(assets_value, dict) else None,
                "verification": "digest_presence_only_not_checksum_verification",
            },
            endpoint=f"{repo_endpoint}/releases/{{id}}/assets", method="GET",
            reason=(
                "digest_presence_does_not_verify_downloaded_asset_bytes"
                if assets_page_complete else "asset_digest_collection_pagination_incomplete"
            ),
            pagination="complete" if assets_page_complete else "incomplete",
            capability="read-only",
        )
        if not assets_page_complete:
            rows["releases.checksums"]["observation"] = "incomplete"
    else:
        rows["releases.checksums"] = _captured_row(
            "releases.checksums", assets, target,
            endpoint=f"{repo_endpoint}/releases/{{id}}/assets",
            reason="asset_digest_inventory_unavailable",
        )
    rows["releases.release_workflow_status"] = _unknown_row(
        "releases.release_workflow_status", target,
        "release_to_workflow_and_artifact_attestation_link_not_verified",
        endpoint="local artifact plus GitHub attestation verification",
        capability="local-workflow",
    )

    # The local worker owns the image handoff; the remote boolean above is the only remote row.
    for group, names in REMOTE_CONTROL_IDS.items():
        for name in names:
            control_id = _control_id(group, name)
            if control_id not in rows:
                rows[control_id] = _unknown_row(
                    control_id, target,
                    f"documented_read_collector_not_implemented_for_{name}",
                    capability="unknown",
                )
    return [rows[_control_id(group, name)] for group, names in REMOTE_CONTROL_IDS.items() for name in names]


def _repo_summary(repo: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "description", "homepage", "private", "visibility", "default_branch", "is_template",
        "has_issues", "has_projects", "has_wiki", "has_downloads", "has_pages", "has_discussions",
        "allow_squash_merge", "allow_merge_commit", "allow_rebase_merge", "allow_auto_merge",
        "delete_branch_on_merge", "squash_merge_commit_title", "squash_merge_commit_message",
        "merge_commit_title", "merge_commit_message",
    )
    summary = {key: repo.get(key) for key in keys if key in repo}
    permissions = repo.get("permissions")
    summary["permissions"] = {"admin": permissions.get("admin")} if isinstance(permissions, dict) and "admin" in permissions else None
    security = repo.get("security_and_analysis")
    if isinstance(security, dict):
        summary["security_and_analysis"] = {
            key: value.get("status")
            for key, value in sorted(security.items())
            if isinstance(value, dict) and value.get("status") in {"enabled", "disabled"}
        }
    else:
        summary["security_and_analysis"] = None
    return summary


def _ruleset_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        summary = {
            "id": row.get("id"),
            "name": row.get("name"),
            "target": row.get("target"),
            "source_type": row.get("source_type"),
            "source": row.get("source"),
            "enforcement": row.get("enforcement"),
            "conditions": row.get("conditions") or {},
            "rule_count": len(row.get("rules")) if isinstance(row.get("rules"), list) else None,
            "bypass_actor_count": len(row.get("bypass_actors")) if isinstance(row.get("bypass_actors"), list) else None,
        }
        if row.get("source_type") == "Repository":
            summary["details_available"] = row.get("_details_available") is True
            if row.get("_details_available") is not True:
                summary["detail_reason"] = row.get("_details_reason", "local_ruleset_detail_not_returned")
        output.append(summary)
    return sorted(output, key=lambda item: (str(item.get("source_type")), str(item.get("target")), str(item.get("name")), int(item.get("id") or 0)))


_RULESET_DETAIL_FIELDS = {"id", "name", "target", "enforcement", "conditions", "rules", "bypass_actors"}


def _rulesets_with_local_details(adapter: GitHubAdapter) -> list[dict[str, Any]]:
    rows = adapter.rulesets()
    detailed: list[dict[str, Any]] = []
    for row in rows:
        if row.get("source_type") != "Repository":
            detailed.append(row)
            continue
        ruleset_id = row.get("id")
        if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool) or ruleset_id <= 0:
            detailed.append({**row, "_details_available": False, "_details_reason": "local_ruleset_detail_id_unavailable"})
            continue
        try:
            value = adapter.get_ruleset(ruleset_id)
        except GitHubError as exc:
            detailed.append({
                **row,
                "_details_available": False,
                "_details_reason": f"local_ruleset_detail_{_error_reason(exc)}",
            })
            continue
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), int)
            or isinstance(value.get("id"), bool)
            or value.get("id") <= 0
            or value.get("id") != ruleset_id
            or not _RULESET_DETAIL_FIELDS <= set(value)
            or not isinstance(value.get("conditions"), dict)
            or not isinstance(value.get("rules"), list)
            or not isinstance(value.get("bypass_actors"), list)
            or value.get("name") != row.get("name")
            or value.get("target") != row.get("target")
        ):
            detailed.append({
                **row,
                "_details_available": False,
                "_details_reason": "local_ruleset_detail_incomplete_or_identity_mismatch",
            })
            continue
        detailed.append({**row, **value, "_details_available": True})
    return detailed


def build_snapshot(adapter: GitHubAdapter, target: RepositoryTarget) -> dict[str, Any]:
    auth = adapter.auth_capabilities()
    if not auth.get("authenticated"):
        raise RuntimeError("GitHub CLI is not authenticated for the requested hostname")
    actor = adapter.actor()
    repo = adapter.repository()
    observed_full_name = repo.get("full_name")
    if not isinstance(observed_full_name, str) or "/" not in observed_full_name:
        raise RuntimeError("GitHub did not return a usable observed repository full name")
    if observed_full_name.casefold() != target.full_name.casefold():
        raise RuntimeError("requested repository resolves to a different observed full name; refusing target drift")


    topics = _capture(adapter.topics)
    rulesets = _capture(lambda: _rulesets_with_local_details(adapter))
    actions = _capture(adapter.actions_permissions)
    workflow_permissions = _capture(adapter.workflow_permissions)
    actions_value = actions.get("value") if isinstance(actions.get("value"), dict) else {}
    selected_actions = _capture(adapter.selected_actions) if actions_value.get("allowed_actions") == "selected" else {"available": False, "reason": "selected_actions_not_currently_active"}
    pages = _capture(adapter.pages)
    vulnerability_alerts = _capture(adapter.vulnerability_alerts_enabled)
    automated_security = _capture(adapter.automated_security_fixes_enabled)
    private_vulnerability_reporting = _capture(adapter.private_vulnerability_reporting_enabled)
    dependabot_count = _capture(adapter.dependabot_alert_count)
    code_scanning_count = _capture(adapter.code_scanning_alert_count)
    immutable = _capture(adapter.immutable_releases_enabled)
    releases = _capture(adapter.releases_summary)
    social_preview = _capture(adapter.social_preview_custom)

    safe_repo = _repo_summary(repo)
    binding = {
        "hostname": target.hostname,
        "requested_full_name": target.full_name,
        "repository_id": repo["id"],
        "observed_full_name": observed_full_name,
        "actor_id": actor["id"],
    }
    groups = {
        "identity_discovery": {
            "status": "observed",
            "repository": {key: safe_repo.get(key) for key in ("description", "homepage", "private", "visibility", "default_branch", "is_template") if key in safe_repo},
            "topics": topics,
            "features": {key: safe_repo.get(key) for key in ("has_issues", "has_projects", "has_wiki", "has_downloads", "has_pages", "has_discussions") if key in safe_repo},
            "report_only": [
                {"control": "visibility", "reason": "public and private visibility changes require an isolated plan and separate exact digest and operation authorization"},
                {"control": "default_branch", "reason": "default branch changes are available only as an isolated high-risk operation after confirming the target branch exists"},
            ],
        },
        "collaboration": {
            "status": "observed",
            "features": {key: safe_repo.get(key) for key in ("has_issues", "has_projects", "has_wiki", "has_discussions") if key in safe_repo},
            "merge": {key: safe_repo.get(key) for key in ("allow_squash_merge", "allow_merge_commit", "allow_rebase_merge", "allow_auto_merge", "delete_branch_on_merge", "squash_merge_commit_title", "squash_merge_commit_message") if key in safe_repo},
            "report_only": [
                {"control": "collaborator_access", "reason": "collaborator writes can change access and notify users; no collaborator operation is offered"},
                {"control": "issue_forms_pull_request_templates_funding_links", "reason": "tracked repository content belongs to the local signed Git workflow"},
                {"control": "organization_projects_policy", "reason": "organization policy may inherit or restrict project settings"},
            ],
        },
        "git_governance": {
            "status": "observed" if rulesets.get("available") else "partially_observed",
            "rulesets": {"available": rulesets.get("available"), "items": _ruleset_summary(rulesets["value"]) if rulesets.get("available") else [], "reason": rulesets.get("reason")},
            "report_only": [
                {"control": "organization_enterprise_rulesets", "reason": "parent rulesets are inventoried as inherited and cannot be changed through this repository target"},
                {"control": "branch_tag_signatures_and_release_tag_verification", "reason": "signed commits and tags require local Git evidence and the repository's signing policy"},
                {"control": "required_status_check_health", "reason": "observational evidence depends on current workflow runs and branch targets"},
            ],
        },
        "actions_deployment": {
            "status": "observed" if actions.get("available") or pages.get("available") else "partially_observed",
            "actions": actions,
            "workflow_permissions": workflow_permissions,
            "selected_actions": selected_actions,
            "pages": pages,
            "report_only": [
                {"control": "organization_actions_policy", "reason": "organization restrictions may override repository settings"},
                {"control": "environments_deployments_and_workflow_health", "reason": "environment protection and workflow results are observational and may be inherited"},
            ],
        },
        "security_supply_chain": {
            "status": "observed" if vulnerability_alerts.get("available") else "partially_observed",
            "security_and_analysis": safe_repo.get("security_and_analysis"),
            "dependabot_alerts_enabled": vulnerability_alerts,
            "automated_security_fixes_enabled": automated_security,
            "private_vulnerability_reporting": private_vulnerability_reporting,
            "open_dependabot_alert_count": dependabot_count,
            "open_code_scanning_alert_count": code_scanning_count,
            "report_only": [
                {"control": "private_alert_details", "reason": "private vulnerability details are intentionally redacted; only aggregate counts are exposed"},
                {"control": "plan_gated_security_features", "reason": "feature availability depends on repository visibility, plan, and organization policy"},
                {"control": "malware_protection_and_attestations", "reason": "no repository setting API evidence is included in this runtime slice"},
            ],
        },
        "releases": {
            "status": "observed" if releases.get("available") else "partially_observed",
            "immutable": immutable,
            "inventory": releases,
            "report_only": [
                {"control": "release_signatures_and_attestations", "reason": "verification requires the project's local release policy and trusted signer identity"},
                {"control": "release_tags_and_artifact_recovery", "reason": "tag protection and artifact recovery are not modified by this settings adapter"},
            ],
        },
        "repository_content": {
            "status": "observed" if social_preview.get("available") else "partially_observed",
            "social_preview_custom": social_preview,
            "tracked_content": {"status": "report_only", "reason": "README, badges, licenses, security policy, changelog, Pages content, accessibility, and attribution are tracked Git content"},
            "report_only": [
                {"control": "custom_social_preview", "reason": "GitHub documents social preview image upload and removal in repository Settings; no documented REST or GraphQL mutation is available", "url": f"https://{target.hostname}/{target.full_name}/settings", "handoff": "Settings → Social preview → Edit → Upload an image or Remove image"},
                {"control": "repository_content_review", "reason": "content changes require the normal local validation and signed commit flow"},
            ],
        },
    }
    control_observations = _build_control_observations(
        adapter,
        target,
        repo,
        {
            "topics": topics,
            "rulesets": rulesets,
            "actions": actions,
            "workflow_permissions": workflow_permissions,
            "selected_actions": selected_actions,
            "pages": pages,
            "vulnerability_alerts": vulnerability_alerts,
            "automated_security": automated_security,
            "private_vulnerability_reporting": private_vulnerability_reporting,
            "dependabot_count": dependabot_count,
            "code_scanning_count": code_scanning_count,
            "immutable": immutable,
            "releases": releases,
            "social_preview": social_preview,
        },
    )
    values = {
        "binding": binding,
        "authorization": {
            "authenticated": auth.get("authenticated", False),
            "scope_visibility": auth.get("scope_visibility", "unknown"),
            "scopes": auth.get("scopes"),
            "repository_admin": safe_repo.get("permissions", {}).get("admin") if isinstance(safe_repo.get("permissions"), dict) else None,
        },
        "repository": safe_repo,
        "topics": topics,
        "rulesets": rulesets,
        "actions_permissions": actions,
        "workflow_permissions": workflow_permissions,
        "selected_actions": selected_actions,
        "pages": pages,
        "vulnerability_alerts": vulnerability_alerts,
        "automated_security_fixes": automated_security,
        "private_vulnerability_reporting": private_vulnerability_reporting,
        "immutable_releases": immutable,
        "dependabot_alert_count": dependabot_count,
        "code_scanning_alert_count": code_scanning_count,
        "releases": releases,
        "social_preview": social_preview,
        "groups": groups,
        "control_observations": control_observations,
    }
    return values
