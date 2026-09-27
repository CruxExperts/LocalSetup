"""Canonical write interfaces for the typed repository operation contract."""

from __future__ import annotations

from typing import Any


_REPO_EDIT_FLAGS = {
    "description": "--description",
    "homepage": "--homepage",
    "has_issues": "--enable-issues",
    "has_projects": "--enable-projects",
    "has_wiki": "--enable-wiki",
    "allow_squash_merge": "--enable-squash-merge",
    "allow_merge_commit": "--enable-merge-commit",
    "allow_rebase_merge": "--enable-rebase-merge",
    "allow_auto_merge": "--enable-auto-merge",
    "delete_branch_on_merge": "--delete-branch-on-merge",
}


def _native_interface(argv: list[str], flags: list[str], reason: str) -> dict[str, Any]:
    return {
        "transport": "gh_repo_edit",
        "command": "gh repo edit",
        "argv_template": argv,
        "environment": {"GH_HOST": "{plan.target.hostname}"},
        "method": None,
        "endpoint_template": None,
        "target_binding": "plan.target",
        "required_flags": flags,
        "selection_reason": reason,
    }


def _api_interface(method: str, endpoint: str, reason: str, *, has_body: bool = True) -> dict[str, Any]:
    flags = ["--hostname", "--method", "--header"]
    argv = [
        "gh", "api", "--hostname", "{plan.target.hostname}", "--method", method,
        "--header", "Accept: application/vnd.github+json",
        "--header", "X-GitHub-Api-Version: 2026-03-10",
    ]
    if has_body:
        flags.append("--input")
        argv.extend(["--input", "-"])
    argv.append(endpoint)
    return {
        "transport": "gh_api",
        "command": "gh api",
        "argv_template": argv,
        "environment": {},
        "method": method,
        "endpoint_template": endpoint,
        "target_binding": "plan.target",
        "required_flags": flags,
        "selection_reason": reason,
    }


def describe_operation_interface(kind: str, current: Any, desired: Any, resource_id: int | None) -> dict[str, Any]:
    """Derive the only supported command/API interface for an operation."""
    if kind in {"repository_visibility", "repository_default_branch"}:
        if kind == "repository_visibility":
            flag, value = "--visibility", desired
        else:
            flag, value = "--default-branch", desired.get("default_branch") if isinstance(desired, dict) else None
        return _native_interface(
            ["gh", "repo", "edit", "{plan.target.requested_full_name}", flag, str(value)],
            [flag],
            "gh repo edit has a dedicated flag that exactly represents this single-field setting",
        )

    if kind == "repository_patch" and isinstance(current, dict) and isinstance(desired, dict):
        if set(desired) <= set(_REPO_EDIT_FLAGS):
            argv = ["gh", "repo", "edit", "{plan.target.requested_full_name}"]
            flags: list[str] = []
            for key in sorted(desired):
                flag = _REPO_EDIT_FLAGS[key]
                flags.append(flag)
                value = desired[key]
                if isinstance(value, bool):
                    argv.append(flag if value else f"{flag}=false")
                else:
                    argv.extend([flag, str(value)])
            return _native_interface(
                argv,
                sorted(flags),
                "gh repo edit has an exact flag for every field in this combined repository patch",
            )
        return _api_interface(
            "PATCH", "repos/{owner}/{repo}",
            "the combined patch includes fields without an exact gh repo edit equivalent; preserve one atomic REST patch",
        )

    if kind == "topics_replace" and isinstance(current, list) and isinstance(desired, list):
        before, after = set(current), set(desired)
        argv = ["gh", "repo", "edit", "{plan.target.requested_full_name}"]
        flags: list[str] = []
        for topic in sorted(after - before):
            flags.append("--add-topic")
            argv.extend(["--add-topic", topic])
        for topic in sorted(before - after):
            flags.append("--remove-topic")
            argv.extend(["--remove-topic", topic])
        return _native_interface(
            argv, sorted(set(flags)),
            "gh repo edit add-topic/remove-topic flags exactly represent the topic set difference",
        )

    api: dict[str, tuple[str, str, str]] = {
        "ruleset_upsert": (
            "POST" if resource_id is None else "PUT",
            "repos/{owner}/{repo}/rulesets" if resource_id is None else f"repos/{{owner}}/{{repo}}/rulesets/{resource_id}",
            "gh ruleset documents read/check commands only; ruleset writes use the documented REST endpoint",
        ),
        "actions_repository_policy": ("PUT", "repos/{owner}/{repo}/actions/permissions", "the typed operation requires a complete REST permissions payload"),
        "actions_workflow_policy": ("PUT", "repos/{owner}/{repo}/actions/permissions/workflow", "no dedicated gh command expresses this workflow-permission operation"),
        "actions_selected_policy": ("PUT", "repos/{owner}/{repo}/actions/permissions/selected-actions", "no dedicated gh command expresses this selected-actions operation"),
        "pages_create": ("POST", "repos/{owner}/{repo}/pages", "no dedicated gh command expresses this Pages creation operation"),
        "pages_update": ("PUT", "repos/{owner}/{repo}/pages", "no dedicated gh command expresses this complete Pages update operation"),
        "security_analysis_patch": ("PATCH", "repos/{owner}/{repo}", "security-analysis fields require the typed nested repository patch"),
        "dependabot_alerts_toggle": ("PUT" if desired is True else "DELETE", "repos/{owner}/{repo}/vulnerability-alerts", "no dedicated gh command expresses this security toggle"),
        "automated_security_fixes_toggle": ("PUT" if desired is True else "DELETE", "repos/{owner}/{repo}/automated-security-fixes", "no dedicated gh command expresses this security toggle"),
        "private_vulnerability_reporting_toggle": ("PUT" if desired is True else "DELETE", "repos/{owner}/{repo}/private-vulnerability-reporting", "no dedicated gh command expresses this security toggle"),
        "immutable_releases_toggle": ("PUT" if desired is True else "DELETE", "repos/{owner}/{repo}/immutable-releases", "no dedicated gh command expresses this release-setting toggle"),
    }
    try:
        method, endpoint, reason = api[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported typed operation interface: {kind}") from exc
    return _api_interface(
        method,
        endpoint,
        reason,
        has_body=kind not in {
            "dependabot_alerts_toggle", "automated_security_fixes_toggle",
            "private_vulnerability_reporting_toggle", "immutable_releases_toggle",
        },
    )
