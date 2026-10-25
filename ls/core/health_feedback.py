"""Private maintenance feedback and explicit, no-replay GitHub issue handoff."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator

from .config import FeedbackPreferences
from .github_repo.issue_api import GitHubAPI
from .github_repo.issue_writer import extract_handoff, fingerprint_for, issue_body
from .github_repo.model import canonical_json
from .github_repo.policy import parse_target
from .github_repo.state import operation_state_directory, target_operation_lock
from .handoff import render_feedback_draft
from .health import incident_root, localsetup_home
from .lockfile import load_json, save_json, save_text
from .redaction import redact_text, redact_public_text


_EVENT_ID = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PRODUCER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_ISSUES = 500


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _incident(home: Path, event_id: str) -> Path:
    if not _EVENT_ID.fullmatch(event_id):
        raise ValueError("feedback event ID is invalid")
    directory = incident_root(home) / event_id
    if not directory.is_dir() or directory.is_symlink():
        raise FileNotFoundError("feedback event was not found")
    info = directory.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("feedback event directory is not private")
    return directory


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    descriptor = os.open(directory / "feedback.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("private feedback lock is not a user-owned mode 0600 file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _event(directory: Path) -> tuple[dict[str, Any], str]:
    descriptor = os.open(directory / "event.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("feedback event is not a user-owned regular file")
        raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("feedback event exceeds its size bound")
    event = json.loads(raw)
    if (not isinstance(event, dict) or event.get("schema_version") != "localsetup.feedback-incident.v1"
            or event.get("event_id") != directory.name):
        raise ValueError("feedback event schema is invalid")
    return event, hashlib.sha256(raw).hexdigest()


def read_observation_file(path: Path) -> str:
    """Reject special files before reading bounded, explicitly supplied prose."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("observation file must be a regular file")
        text = stream.read(8193)
    if len(text) > 8192:
        raise ValueError("observation file exceeds 8192 bytes")
    return text.decode("utf-8")


def event_details(*, home: Path, event_id: str) -> dict[str, Any]:
    """Read bounded private debugging evidence without preparing publication."""
    directory = _incident(home, event_id)
    with _lock(directory):
        event, digest = _event(directory)
        return {"ok": True, "event": event, "event_digest": digest,
                "visibility": "private", "publication_authorized": False}


def _observations(directory: Path, event_digest: str) -> list[dict[str, Any]]:
    document = load_json(directory / "observations.json")
    items = document.get("items", [])
    if not isinstance(items, list) or len(items) > 20 or any(
        not isinstance(item, dict) or item.get("event_digest") != event_digest
        or not isinstance(item.get("text"), str) or not _PRODUCER.fullmatch(str(item.get("producer", "")))
        for item in items
    ):
        raise ValueError("feedback observations do not match this event")
    return items


def add_observation(*, home: Path, event_id: str, event_digest: str,
                    producer: str, text: str) -> dict[str, Any]:
    directory = _incident(home, event_id)
    if not _DIGEST.fullmatch(event_digest) or not _PRODUCER.fullmatch(producer):
        raise ValueError("observation identity is invalid")
    if len(text.encode("utf-8")) > 8192:
        raise ValueError("observation exceeds its size bound")
    with _lock(directory):
        _current, actual_digest = _event(directory)
        if event_digest != actual_digest:
            raise ValueError("observation event digest changed")
        receipt = load_json(directory / "submission.json")
        if receipt.get("state") in {"dispatching", "unknown", "verified"}:
            raise ValueError("a submitted feedback event cannot accept observations")
        items = _observations(directory, event_digest)
        if len(items) >= 20:
            raise ValueError("observation count exceeds its bound")
        observation = {"event_digest": event_digest, "producer": producer,
                       "text": redact_text(text), "digest": _digest({"producer": producer, "text": redact_text(text)})}
        items.append(observation)
        save_json(directory / "observations.json", {"items": items})
        return {"ok": True, "event_id": event_id, "observation_digest": observation["digest"]}


def draft_request(*, home: Path, event_id: str) -> dict[str, Any]:
    directory = _incident(home, event_id)
    with _lock(directory):
        if load_json(directory / "submission.json").get("state") in {"dispatching", "unknown", "verified"}:
            raise ValueError("a submitted feedback draft is immutable")
        event, event_digest = _event(directory)
        observations = _observations(directory, event_digest)
        draft = render_feedback_draft(event, observations)
        save_text(directory / "draft.md", draft)
        return {"ok": True, "event_id": event_id, "event_digest": event_digest,
                "draft_digest": hashlib.sha256(draft.encode("utf-8")).hexdigest(), "draft": draft}


def _api_for_repository(repository: str | None) -> GitHubAPI:
    if not repository:
        raise ValueError("feedback repository is not configured")
    parse_target("github.com", repository)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    api = GitHubAPI(repository=repository, token=token,
                    api_url="https://api.github.com", max_records=_MAX_ISSUES,
                    request_budget=100)
    if not api.enabled():
        raise ValueError("GitHub issue observation requires GH_TOKEN or GITHUB_TOKEN")
    return api


def _api(feedback: FeedbackPreferences) -> GitHubAPI:
    return _api_for_repository(feedback.repository)


def _target(api: GitHubAPI) -> dict[str, Any]:
    observed = api.get_repository()
    if (observed.get("full_name", "").casefold() != api.repository.casefold()
            or type(observed.get("id")) is not int or observed["id"] <= 0):
        raise ValueError("configured feedback repository identity changed")
    return {"hostname": "github.com", "repository": api.repository,
            "repository_id": observed["id"]}


def _decision(api: GitHubAPI, fingerprint: str, body: str, title: str) -> dict[str, Any]:
    issues = api.list_issues("", state="all")
    if len(issues) > _MAX_ISSUES:
        raise ValueError("feedback duplicate inventory exceeds its bound")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for issue in issues:
        if type(issue.get("id")) is not int or type(issue.get("number")) is not int:
            raise ValueError("feedback issue identity is incomplete")
        latest = issue
        initial = extract_handoff(str(issue.get("body") or ""))
        matched = initial is not None and initial.get("fingerprint") == fingerprint
        for comment in ([] if issue.get("comments") == 0 else api.list_issue_comments(issue["number"])):
            handoff = extract_handoff(str(comment.get("body") or ""))
            if handoff is not None and handoff.get("fingerprint") == fingerprint:
                matched, latest = True, comment
        if matched:
            matches.append((issue, latest))
    open_matches = [(issue, latest) for issue, latest in matches if issue.get("state") == "open"]
    if len(open_matches) > 1:
        raise ValueError("multiple open feedback issues share one fingerprint")
    if open_matches:
        issue, latest = open_matches[0]
        return {"action": "noop" if latest.get("body") == body else "comment",
                "target_issue_id": issue["id"], "target_issue_number": issue["number"]}
    if any(latest.get("body") == body for _issue, latest in matches):
        issue = next(issue for issue, latest in matches if latest.get("body") == body)
        return {"action": "noop", "target_issue_id": issue["id"],
                "target_issue_number": issue["number"]}
    return {"action": "create", "target_issue_id": None, "target_issue_number": None}


def _material(directory: Path, feedback: FeedbackPreferences,
              api: GitHubAPI) -> dict[str, Any]:
    event, event_digest = _event(directory)
    observations = _observations(directory, event_digest)
    draft = render_feedback_draft(event, observations)
    draft_bytes = (directory / "draft.md").read_bytes()
    if draft_bytes != draft.encode("utf-8"):
        raise ValueError("feedback draft changed; regenerate it before preparing")
    title = draft.splitlines()[0].removeprefix("# ")[:180]
    finding = {"category": "workflow", "check_type": redact_public_text(str(event["code"])),
               "title": title, "affected_paths": [], "region": redact_public_text(str(event["operation"])),
               "body": draft, "severity": "medium"}
    body = issue_body(finding)
    fingerprint = fingerprint_for(finding)
    target = _target(api)
    decision = _decision(api, fingerprint, body, title)
    material = {"schema_version": "localsetup.feedback-plan.v1", "event_id": event["event_id"],
                "event_digest": event_digest,
                "draft_digest": hashlib.sha256(draft_bytes).hexdigest(),
                "observations_digest": _digest(observations),
                "config_digest": _digest(feedback.__dict__),
                "target": target, "title": title, "body": body,
                "labels": sorted(set(feedback.labels)), "fingerprint": fingerprint,
                **decision}
    material["plan_digest"] = _digest(material)
    return material


def prepare_request(*, home: Path, event_id: str,
                    feedback: FeedbackPreferences, api: GitHubAPI | None = None) -> dict[str, Any]:
    directory = _incident(home, event_id)
    with _lock(directory):
        receipt = load_json(directory / "submission.json")
        if receipt.get("state") in {"dispatching", "unknown", "verified"}:
            raise ValueError("feedback submission already has an immutable dispatch receipt")
        client = api or _api(feedback)
        plan = _material(directory, feedback, client)
        save_json(directory / "plan.json", plan)
        save_json(directory / "submission.json", {"state": "prepared", "plan_digest": plan["plan_digest"]})
        return {"ok": True, "state": "prepared", "plan": plan}


def _reconcile(api: GitHubAPI, plan: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    action = plan["action"]
    try:
        if action == "create":
            issues = api.list_issues("", state="all")
            matches = [issue for issue in issues if issue.get("body") == plan["body"]]
            if len(matches) == 1:
                receipt.update(state="verified", issue_id=matches[0]["id"],
                               issue_url=matches[0].get("html_url"))
        elif action == "comment":
            comments = api.list_issue_comments(plan["target_issue_number"])
            matches = [item for item in comments if item.get("body") == plan["body"]]
            if len(matches) == 1:
                receipt.update(state="verified", comment_id=matches[0]["id"],
                               issue_id=plan["target_issue_id"])
        elif action == "noop":
            receipt.update(state="verified", issue_id=plan["target_issue_id"])
    except Exception as exc:
        receipt["reconciliation_error"] = redact_text(str(exc))[:500]
    return receipt


def submit_request(*, home: Path, event_id: str, approved_plan_digest: str,
                   feedback: FeedbackPreferences, api: GitHubAPI | None = None) -> dict[str, Any]:
    if not _DIGEST.fullmatch(approved_plan_digest):
        raise ValueError("approved plan digest is invalid")
    directory = _incident(home, event_id)
    with _lock(directory):
        plan = load_json(directory / "plan.json")
        receipt = load_json(directory / "submission.json")
        if not plan or plan.get("plan_digest") != approved_plan_digest or _digest({
            key: value for key, value in plan.items() if key != "plan_digest"}) != approved_plan_digest:
            raise ValueError("approved feedback plan is not the exact prepared payload")
        if receipt.get("plan_digest") != approved_plan_digest:
            raise ValueError("feedback dispatch receipt does not match the approved plan")
        past_dispatch = receipt.get("state") in {"dispatching", "unknown", "verified"}
        client = api or _api_for_repository(plan["target"]["repository"] if past_dispatch else feedback.repository)
        target = _target(client)
        if target != plan.get("target"):
            raise ValueError("configured feedback target changed")
        state_root = localsetup_home(home) / "state"
        target_dir = operation_state_directory(state_root, "github.com", target["repository_id"])
        with target_operation_lock(target_dir):
            if receipt.get("state") in {"dispatching", "unknown"}:
                receipt = _reconcile(client, plan, receipt)
                save_json(directory / "submission.json", receipt)
                return {"ok": receipt["state"] == "verified", **receipt}
            if receipt.get("state") == "verified":
                return {"ok": True, **receipt}
            if receipt.get("state") != "prepared":
                raise ValueError("feedback submission state is not prepared")
            current = _material(directory, feedback, client)
            if current != plan:
                raise ValueError("feedback evidence or remote issue state changed; prepare a new plan")
            if plan["action"] == "noop":
                receipt.update(state="verified", issue_id=plan["target_issue_id"])
                save_json(directory / "submission.json", receipt)
                return {"ok": True, **receipt}
            receipt.update(state="dispatching", action=plan["action"], target=target,
                           title=plan["title"], body=plan["body"], labels=plan["labels"])
            save_json(directory / "submission.json", receipt)
            try:
                if plan["action"] == "create":
                    response = client.create_issue(plan["title"], plan["body"], plan["labels"])
                    if type(response.get("id")) is not int:
                        raise ValueError("GitHub issue response lacks an immutable ID")
                    receipt.update(state="verified", issue_id=response["id"],
                                   issue_url=response.get("html_url"))
                else:
                    response = client.comment_issue(plan["target_issue_number"], plan["body"])
                    if type(response.get("id")) is not int:
                        raise ValueError("GitHub comment response lacks an immutable ID")
                    receipt.update(state="verified", comment_id=response["id"],
                                   issue_id=plan["target_issue_id"])
            except Exception as exc:
                receipt.update(state="unknown", error=redact_text(str(exc))[:500])
            save_json(directory / "submission.json", receipt)
            return {"ok": receipt["state"] == "verified", **receipt}
