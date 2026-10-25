"""Private failure feedback and explicit issue submission contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ls.core.config import FeedbackPreferences, load_install_config
from ls.core.health import page_events, record_failure
from ls.core.health_feedback import (
    add_observation, draft_request, prepare_request, submit_request,
)
from ls.core.terminal_ops.command_state import ShellCommandError


class FakeIssueAPI:
    repository = "example/project"

    def __init__(self):
        self.issues = []
        self.comments = {}
        self.posts = 0
        self.fail_after_create = False
        self.fail_without_create = False

    def get_repository(self):
        return {"id": 42, "full_name": self.repository}

    def list_issues(self, _labels, *, state):
        assert state == "all"
        return list(self.issues)

    def list_issue_comments(self, number):
        return list(self.comments.get(number, []))

    def create_issue(self, title, body, labels):
        self.posts += 1
        if self.fail_without_create:
            raise TimeoutError("lost POST acknowledgement")
        issue = {"id": 100 + self.posts, "number": 10 + self.posts,
                 "state": "open", "title": title, "body": body,
                 "labels": labels, "html_url": f"https://github.com/{self.repository}/issues/{10 + self.posts}"}
        self.issues.append(issue)
        if self.fail_after_create:
            raise TimeoutError("lost POST acknowledgement")
        return issue

    def comment_issue(self, number, body):
        self.posts += 1
        comment = {"id": 200 + self.posts, "body": body}
        self.comments.setdefault(number, []).append(comment)
        return comment


def _capture(tmp_path: Path, message: str = "token=ghp_abcdefghijklmnop /home/alice/private"):
    home = tmp_path / "home"
    try:
        raise RuntimeError(message)
    except RuntimeError as error:
        captured = record_failure(repo_root=tmp_path, home=home, target_root=tmp_path,
                                  operation="verify", error=error)
    assert captured is not None
    return home, captured["event_id"]


def test_capture_redacts_private_diagnostics_and_pages_with_cursor(tmp_path: Path):
    home, first = _capture(tmp_path)
    _home, second = _capture(tmp_path, "second failure")
    incident = home / ".local/share/localsetup/state/health/incidents" / first
    event = json.loads((incident / "event.json").read_text(encoding="utf-8"))
    draft = (incident / "draft.md").read_text(encoding="utf-8")
    assert "ghp_abcdefghijklmnop" not in event["message"] + event["traceback"] + draft
    assert "/home/alice/private" not in draft
    assert "[REDACTED_PATH]" in draft
    assert not (incident.parent.parent / "latest.json").exists()
    first_page = page_events(home=home, limit=1)
    second_page = page_events(home=home, after=first_page["next_cursor"], limit=1)
    assert [row["event_id"] for row in first_page["events"] + second_page["events"]] == [first, second]
    assert first_page["has_more"] is True and second_page["has_more"] is False
    assert first_page["bytes"] <= 65_536


def test_event_detail_cli_exposes_private_trace_and_provenance(tmp_path, capsys):
    from ls.core import cli
    home, event_id = _capture(tmp_path)
    assert cli.main(["--home", str(home), "health", "events", "--event", event_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["visibility"] == "private"
    assert payload["publication_authorized"] is False
    assert payload["event"]["event_id"] == event_id
    assert "RuntimeError" in payload["event"]["traceback"]
    assert payload["event"]["target_root"] == str(tmp_path)
    assert len(payload["event_digest"]) == 64


def test_event_detail_rejects_symlink_and_oversized_record(tmp_path):
    from ls.core.health_feedback import event_details
    home, event_id = _capture(tmp_path)
    path = home / ".local/share/localsetup/state/health/incidents" / event_id / "event.json"
    path.write_bytes(b"x" * 65537)
    with pytest.raises(ValueError, match="size bound"):
        event_details(home=home, event_id=event_id)
    path.unlink()
    path.symlink_to(tmp_path / "unrelated")
    with pytest.raises(OSError):
        event_details(home=home, event_id=event_id)


def test_issue_inventory_and_network_requests_are_bounded(monkeypatch):
    from ls.core.github_repo.issue_api import GitHubAPI
    api = GitHubAPI("example/project", "fixture-token", max_records=100)
    pages = []
    def page(_method, _path, **kwargs):
        pages.append(kwargs["params"]["page"])
        return [{"id": n} for n in range(100)]
    monkeypatch.setattr(api, "_request", page)
    with pytest.raises(RuntimeError, match="inventory exceeded"):
        api.list_issues("", state="all")
    assert pages == [1, 2]
    refused = GitHubAPI("example/project", "fixture-token", request_budget=0)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        refused.get_repository()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_issue_transport_never_follows_redirects(monkeypatch, status):
    from types import SimpleNamespace
    from ls.core.github_repo import issue_api
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=status)
    monkeypatch.setattr(issue_api.requests, "request", request)
    api = issue_api.GitHubAPI("example/project", "fixture-token")
    with pytest.raises(RuntimeError, match="redirect refused"):
        api.create_issue("title", "approved body", [])
    assert len(calls) == 1
    assert calls[0][2]["allow_redirects"] is False


def test_entire_prepared_payload_redacts_private_metadata(tmp_path):
    home = tmp_path / "home"
    error = RuntimeError("failed")
    error.code = "/home/alice/private"
    captured = record_failure(repo_root=tmp_path, home=home, target_root=tmp_path,
                              operation="inspect /home/alice/private", error=error)
    api = FakeIssueAPI()
    plan = prepare_request(home=home, event_id=captured["event_id"],
                           feedback=FeedbackPreferences(repository=api.repository), api=api)["plan"]
    assert "/home/alice/private" not in plan["body"]


def test_draft_observations_and_prepared_payload_are_exact(tmp_path: Path):
    home, event_id = _capture(tmp_path)
    initial = draft_request(home=home, event_id=event_id)
    with pytest.raises(ValueError, match="digest"):
        add_observation(home=home, event_id=event_id, event_digest="0" * 64,
                        producer="agent-1", text="reproduced")
    added = add_observation(home=home, event_id=event_id, event_digest=initial["event_digest"],
                            producer="agent-1", text="Observed at /tmp/private token=secret")
    assert len(added["observation_digest"]) == 64
    draft = draft_request(home=home, event_id=event_id)
    assert draft["draft_digest"] == draft_request(home=home, event_id=event_id)["draft_digest"]
    assert "/tmp/private" not in draft["draft"]
    api = FakeIssueAPI()
    feedback = FeedbackPreferences(repository=api.repository, labels=["feedback"])
    prepared = prepare_request(home=home, event_id=event_id, feedback=feedback, api=api)["plan"]
    assert prepared["action"] == "create" and api.posts == 0
    assert prepared["target"] == {"hostname": "github.com", "repository": api.repository, "repository_id": 42}
    assert prepared == prepare_request(home=home, event_id=event_id, feedback=feedback, api=api)["plan"]
    with pytest.raises(ValueError, match="exact prepared"):
        submit_request(home=home, event_id=event_id, approved_plan_digest="0" * 64,
                       feedback=feedback, api=api)
    (home / ".local/share/localsetup/state/health/incidents" / event_id / "draft.md").write_text(
        "tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="regenerate"):
        submit_request(home=home, event_id=event_id, approved_plan_digest=prepared["plan_digest"],
                       feedback=feedback, api=api)
    assert api.posts == 0


def test_duplicate_prepare_chooses_noop_or_comment_without_post(tmp_path: Path):
    home, event_id = _capture(tmp_path)
    api = FakeIssueAPI()
    feedback = FeedbackPreferences(repository=api.repository)
    first = prepare_request(home=home, event_id=event_id, feedback=feedback, api=api)["plan"]
    api.issues.append({"id": 501, "number": 51, "state": "open", "body": first["body"], "title": first["title"]})
    same = prepare_request(home=home, event_id=event_id, feedback=feedback, api=api)["plan"]
    assert same["action"] == "noop" and same["target_issue_id"] == 501
    digest = draft_request(home=home, event_id=event_id)["event_digest"]
    add_observation(home=home, event_id=event_id, event_digest=digest,
                    producer="agent-1", text="new reproduction")
    draft_request(home=home, event_id=event_id)
    changed = prepare_request(home=home, event_id=event_id, feedback=feedback, api=api)["plan"]
    assert changed["action"] == "comment" and changed["target_issue_id"] == 501
    assert api.posts == 0


@pytest.mark.parametrize("remote_effect", ["applied", "absent"])
def test_unknown_post_never_replays_and_reconciles_by_exact_readback(tmp_path: Path, remote_effect: str):
    home, event_id = _capture(tmp_path)
    api = FakeIssueAPI()
    api.fail_after_create = remote_effect == "applied"
    api.fail_without_create = remote_effect == "absent"
    feedback = FeedbackPreferences(repository=api.repository)
    plan = prepare_request(home=home, event_id=event_id, feedback=feedback, api=api)["plan"]
    first = submit_request(home=home, event_id=event_id,
                           approved_plan_digest=plan["plan_digest"], feedback=feedback, api=api)
    assert first["state"] == "unknown" and api.posts == 1
    again = submit_request(home=home, event_id=event_id,
                           approved_plan_digest=plan["plan_digest"], feedback=feedback, api=api)
    assert api.posts == 1
    assert again["state"] == ("verified" if remote_effect == "applied" else "unknown")


def test_feedback_destination_is_optional_and_has_no_credential_field(tmp_path: Path):
    config = tmp_path / "install.json"
    config.write_text(json.dumps({"feedback": {"repository": "example/project",
                                                "labels": ["feedback"], "capture_enabled": True}}),
                      encoding="utf-8")
    loaded = load_install_config(config)
    assert loaded.feedback.repository == "example/project"
    assert "token" not in loaded.feedback.__dict__
    config.write_text(json.dumps({"feedback": {"repository": "example/project", "token": "never"}}),
                      encoding="utf-8")
    with pytest.raises(ValueError):
        load_install_config(config)


def test_core_exception_capture_preserves_original_error(tmp_path: Path, monkeypatch, capsys):
    import ls.core.cli as cli
    monkeypatch.setattr(cli, "_main", lambda _arguments: (_ for _ in ()).throw(ValueError("token=secret failure")))
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    result = cli.main(["--home", str(tmp_path / "home"), "verify"])
    assert result == 2
    assert "localsetup: token=secret failure" in capsys.readouterr().err
    captured = page_events(home=tmp_path / "home")
    assert len(captured["events"]) == 1
    assert "secret" not in captured["events"][0]["message"]


def test_capture_uses_selected_source_and_configured_home_target(tmp_path, monkeypatch):
    from ls.core import cli
    from ls.core.health_feedback import event_details
    config = tmp_path / "config.json"
    home, source, target = (tmp_path / name for name in ("custom-home", "source", "target"))
    source.mkdir()
    config.write_text(json.dumps({"home": str(home), "target_directory": str(target)}))
    monkeypatch.setattr(cli, "_main", lambda _args: (_ for _ in ()).throw(ValueError("failure")))
    assert cli.main(["--source-root", str(source), "verify", "--config", str(config)]) == 2
    event_id = page_events(home=home)["events"][0]["event_id"]
    event = event_details(home=home, event_id=event_id)["event"]
    assert event["source_root"] == str(source)
    assert event["target_root"] == str(target)


def test_unicode_capture_obeys_serialized_byte_limit_and_remains_pageable(tmp_path):
    from ls.core.health_feedback import event_details
    home, event_id = _capture(tmp_path, message="💥" * 12000)
    event = event_details(home=home, event_id=event_id)["event"]
    assert event["diagnostic_truncated"] is True
    assert len(page_events(home=home)["events"]) == 1


@pytest.mark.parametrize("capture,auto_draft", [(True, True), (True, False), (False, True)])
def test_shell_handled_failure_captures_without_changing_json(tmp_path: Path, monkeypatch, capsys, capture, auto_draft):
    import ls.core.terminal_ops.command_cli as shell_cli

    class FailingService:
        def __init__(self, *_args, **_kwargs):
            pass

        def capabilities(self):
            raise ShellCommandError("backend_failed", "token=secret", effect="unknown")

    monkeypatch.setattr(shell_cli, "ShellCommandService", FailingService)
    config = tmp_path / "feedback.json"
    config.write_text(json.dumps({"feedback": {"capture_enabled": capture, "auto_draft": auto_draft}}))
    result = shell_cli.main(["--config", str(config), "capabilities"], source_root=tmp_path, home=tmp_path / "home")
    payload = json.loads(capsys.readouterr().out)
    assert result == 2 and payload["code"] == "backend_failed"
    assert payload["message"] == "token=secret" and payload["effect"] == "unknown"
    events = page_events(home=tmp_path / "home")["events"]
    assert len(events) == int(capture)
    if capture:
        assert events[0]["effect"] == "unknown"
        draft = tmp_path / "home/.local/share/localsetup/state/health/incidents" / events[0]["event_id"] / "draft.md"
        assert draft.exists() is auto_draft
