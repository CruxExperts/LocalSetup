from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .github_api import GitHubAPI


BLOCK_RE = re.compile(r"```qc.issue-handoff.v1\n(.*?)\n```", re.DOTALL)
KNOWN_CATEGORY_LABELS = {"workflow_security", "release", "docs", "pr_review", "inventory"}


def normalize_title(title: str) -> str:
    return " ".join(title.lower().split())


def fingerprint_for(finding: dict[str, Any]) -> str:
    material = "|".join(
        [
            str(finding.get("category", "")),
            str(finding.get("check_type", "")),
            normalize_title(str(finding.get("title", ""))),
            str((finding.get("affected_paths") or [""])[0]),
            str(finding.get("region", "")),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def extract_handoff(body: str) -> dict[str, Any] | None:
    match = BLOCK_RE.search(body or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def issue_body(finding: dict[str, Any], *, compact: bool = False) -> str:
    stored_finding = {"evidence_identity": finding.get("evidence_identity")} if compact else finding
    handoff = {"schema_version": "qc.issue-handoff.v1", "fingerprint": fingerprint_for(finding), "finding": stored_finding}
    block = f"```qc.issue-handoff.v1\n{json.dumps(handoff, indent=2, sort_keys=True)}\n```"
    if compact:
        block = f"<!--\n{block}\n-->"
    return f"{finding['body']}\n\n{block}\n"


def labels_for(finding: dict[str, Any], base_labels: list[str]) -> list[str]:
    labels = set(base_labels + [f"qc/severity/{finding['severity']}"])
    if finding["category"] in KNOWN_CATEGORY_LABELS:
        labels.add(f"qc/category/{finding['category']}")
    return sorted(labels)


def issue_policy(finding: dict[str, Any], *, mode: str = "legacy") -> tuple[bool, str]:
    check_type = str(finding.get("check_type", ""))
    severity = str(finding.get("severity", ""))
    if check_type == "ai_packet_adjudication":
        return False, "ai_artifact_only"
    if mode == "legacy":
        return True, "legacy_issue_behavior"
    if severity in {"high", "critical"}:
        return True, "deterministic_high_or_critical"
    return False, "deterministic_below_issue_threshold"


def find_duplicate(issues: list[dict[str, Any]], finding: dict[str, Any]) -> dict[str, Any] | None:
    matches = _matching_issues(issues, finding)
    return next((issue for issue in matches if issue.get("state", "open") == "open"), matches[0] if matches else None)


def _matching_issues(issues: list[dict[str, Any]], finding: dict[str, Any]) -> list[dict[str, Any]]:
    target = fingerprint_for(finding)
    matches: list[dict[str, Any]] = []
    for issue in issues:
        handoff = extract_handoff(str(issue.get("body", "")))
        if handoff and handoff.get("fingerprint") == target:
            matches.append(issue)
    return matches


def _latest_handoff(issue: dict[str, Any], fingerprint: str, api: GitHubAPI) -> dict[str, Any]:
    latest = issue
    for observation in reversed(api.list_issue_comments(int(issue["number"]))):
        handoff = extract_handoff(str(observation.get("body", "")))
        if handoff and handoff.get("fingerprint") == fingerprint:
            latest = observation
            break
    return latest


def duplicate_is_unchanged(issue: dict[str, Any], finding: dict[str, Any]) -> bool:
    handoff = extract_handoff(str(issue.get("body", "")))
    prior = handoff.get("finding") if handoff else None
    if isinstance(prior, dict) and isinstance(prior.get("evidence_identity"), dict):
        return prior.get("evidence_identity") == finding.get("evidence_identity")
    return isinstance(prior, dict) and prior == finding




def write_issues(
    findings: list[dict[str, Any]],
    labels: list[str],
    *,
    dry_run: bool = False,
    api: GitHubAPI | None = None,
    policy_mode: str = "legacy",
    title_prefix: str = "QC",
    discovery_label: str | None = None,
    include_closed: bool = False,
    issue_labels: list[str] | None = None,
    compact_handoff: bool = False,
) -> list[dict[str, Any]]:
    api = api or GitHubAPI.from_env()
    can_observe = api.enabled() and not dry_run
    label = discovery_label or "qc-patrol"
    if can_observe and include_closed:
        existing = api.list_issues(label, state="all")
    elif can_observe:
        existing = api.list_open_issues(label)
    else:
        existing = []

    # Provenance handoffs must finish all reads before the first write so an
    # incomplete issue/comment inventory can never produce partial decisions.
    discovered: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    if include_closed and can_observe:
        for finding in findings:
            allowed, _ = issue_policy(finding, mode=policy_mode)
            if not allowed:
                continue
            fingerprint = fingerprint_for(finding)
            matches = _matching_issues(existing, finding)
            duplicate = next(
                (issue for issue in matches if issue.get("state", "open") == "open"),
                None,
            )
            if duplicate is not None:
                # An open issue retains the established update behavior. Closed
                # history does not override its current evidence state.
                latest = _latest_handoff(duplicate, fingerprint, api)
            else:
                histories = [
                    (issue, _latest_handoff(issue, fingerprint, api))
                    for issue in matches
                ]
                identical = next(
                    (
                        (issue, latest)
                        for issue, latest in histories
                        if duplicate_is_unchanged(latest, finding)
                    ),
                    None,
                )
                duplicate, latest = identical or (histories[0] if histories else (None, None))
            discovered[fingerprint] = (duplicate, latest)

    results: list[dict[str, Any]] = []
    for finding in findings:
        title = f"[{title_prefix}][{finding['category']}] {finding['title']}"
        allowed, policy_reason = issue_policy(finding, mode=policy_mode)
        if not allowed:
            results.append({"action": "skip", "title": title, "fingerprint": fingerprint_for(finding), "policy_reason": policy_reason})
            continue
        fingerprint = fingerprint_for(finding)
        if include_closed and can_observe:
            duplicate, latest = discovered.get(fingerprint, (None, None))
        else:
            duplicate = find_duplicate(existing, finding)
            latest = duplicate
            if duplicate is not None and can_observe:
                latest = _latest_handoff(duplicate, fingerprint, api)
        if duplicate:
            action = "unchanged" if duplicate_is_unchanged(latest, finding) else "comment"
            if include_closed and duplicate.get("state") == "closed" and action != "unchanged":
                action = "create"
                if can_observe:
                    applied_labels = issue_labels if issue_labels is not None else labels_for(finding, labels)
                    created = api.create_issue(title, issue_body(finding, compact=compact_handoff), applied_labels)
                    existing.append(created)
            elif action == "comment" and can_observe:
                api.comment_issue(int(duplicate["number"]), issue_body(finding, compact=compact_handoff))
        else:
            action = "create"
            if can_observe:
                applied_labels = issue_labels if issue_labels is not None else labels_for(finding, labels)
                created = api.create_issue(title, issue_body(finding, compact=compact_handoff), applied_labels)
                existing.append(created)
        results.append({"action": action, "title": title, "fingerprint": fingerprint_for(finding), "policy_reason": policy_reason})
    return results
