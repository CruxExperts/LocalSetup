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


def issue_body(finding: dict[str, Any]) -> str:
    handoff = {"schema_version": "qc.issue-handoff.v1", "fingerprint": fingerprint_for(finding), "finding": finding}
    return f"{finding['body']}\n\n```qc.issue-handoff.v1\n{json.dumps(handoff, indent=2, sort_keys=True)}\n```\n"


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
    target = fingerprint_for(finding)
    for issue in issues:
        handoff = extract_handoff(str(issue.get("body", "")))
        if handoff and handoff.get("fingerprint") == target:
            return issue
    return None


def duplicate_is_unchanged(issue: dict[str, Any], finding: dict[str, Any]) -> bool:
    handoff = extract_handoff(str(issue.get("body", "")))
    prior = handoff.get("finding") if handoff else None
    return isinstance(prior, dict) and prior == finding




def write_issues(
    findings: list[dict[str, Any]],
    labels: list[str],
    *,
    dry_run: bool = False,
    api: GitHubAPI | None = None,
    policy_mode: str = "legacy",
) -> list[dict[str, Any]]:
    api = api or GitHubAPI.from_env()
    existing = api.list_open_issues("qc-patrol") if api.enabled() and not dry_run else []
    results: list[dict[str, Any]] = []
    for finding in findings:
        title = f"[QC][{finding['category']}] {finding['title']}"
        allowed, policy_reason = issue_policy(finding, mode=policy_mode)
        if not allowed:
            results.append({"action": "skip", "title": title, "fingerprint": fingerprint_for(finding), "policy_reason": policy_reason})
            continue
        duplicate = find_duplicate(existing, finding)
        if duplicate:
            observations = api.list_issue_comments(int(duplicate["number"]))
            latest = duplicate
            for observation in reversed(observations):
                handoff = extract_handoff(str(observation.get("body", "")))
                if handoff and handoff.get("fingerprint") == fingerprint_for(finding):
                    latest = observation
                    break
            action = "unchanged" if duplicate_is_unchanged(latest, finding) else "comment"
            if action == "comment" and api.enabled() and not dry_run:
                api.comment_issue(int(duplicate["number"]), issue_body(finding))
        else:
            action = "create"
            if api.enabled() and not dry_run:
                created = api.create_issue(title, issue_body(finding), labels_for(finding, labels))
                existing.append(created)
        results.append({"action": action, "title": title, "fingerprint": fingerprint_for(finding), "policy_reason": policy_reason})
    return results
