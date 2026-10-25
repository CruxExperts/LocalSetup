"""Bounded observations, completed-pane expiry and literal help arguments."""
from copy import deepcopy

import pytest

from ls.core.cli_help import help_aliases
from ls.core.terminal_ops.command_activity import Activity, observation
from ls.core.terminal_ops.command_activity_codex import CodexObserver
from ls.core.terminal_ops.command_native_layout import plan_native_layout
from ls.core.terminal_ops.ui_patterns import close_clicked


def test_observations_discard_payloads_and_reasoning_and_handle_reordered_completion():
    item = {"id": "call", "type": "mcpToolCall", "tool": "lookup", "status": "completed",
            "arguments": {"token": "secret"}, "result": "private response"}
    value = observation("root", {"item": item})
    assert value == {"thread": "root", "id": "call", "kind": "tool", "name": "lookup", "state": "Completed"}
    assert observation("root", {"item": {"type": "reasoning", "text": "private"}}) is None
    activity = Activity(maximum=2)
    assert activity.accept(value)
    assert not activity.accept(value)
    assert not activity.accept(value | {"state": "Running"})
    assert activity.accept(value | {"id": "second"})
    assert activity.accept(value | {"id": "third"})
    assert len(activity.items) == 2 and activity.dropped == 1


def test_observer_cannot_issue_mutations_or_query_unrelated_threads():
    observer = CodexObserver("root", {}, ".")
    for method, params in [("turn/start", {}), ("thread/resume", {}),
                           ("thread/items/list", {"threadId": "other"}),
                           ("thread/list", {"useStateDbOnly": True})]:
        with pytest.raises(PermissionError):
            observer.request(method, params)


def test_older_enrollment_cannot_overwrite_restarted_observer(tmp_path):
    from types import SimpleNamespace
    from ls.core.terminal_ops.command_activity_cli import start
    from ls.core.terminal_ops.command_state import atomic_json, read_json
    path = tmp_path / "session.json"
    old = {"thread": "root", "generation": 0, "state": "unavailable", "operation_handle": "old"}
    current = {"handle": "family", "endpoint": "fixture", "native_session_id": "$0",
               "client_activity_binding": old}
    atomic_json(path, current)
    newer = old | {"generation": 1, "state": "observing", "operation_handle": "new"}
    def run(*args, **kwargs):
        atomic_json(path, current | {"client_activity_binding": newer})
        return {"state": "completed", "operation_handle": "old"}
    service = SimpleNamespace(_load=lambda *_a: (tmp_path, deepcopy(current), None),
        _authorize=lambda *_a: None, run=run,
        backend=SimpleNamespace(_checked=lambda *_a: str(tmp_path)))
    result = start(service, "family", "root", {})
    assert result == {"state": "observing", "operation_handle": "new"}
    assert read_json(path)["client_activity_binding"] == newer


@pytest.mark.parametrize("status", ["starting", "live", "reconnecting"])
def test_interrupted_observation_becomes_stale_without_changing_saved_evidence(tmp_path, monkeypatch, status):
    from ls.core.terminal_ops import command_activity_cli as cli
    from ls.core.terminal_ops.command_state import atomic_json, read_json
    path = tmp_path / "client-activity.json"
    atomic_json(path, {"session": "family", "status": status, "updated_at": 1})
    monkeypatch.setattr(cli.time, "time", lambda: 20)
    assert cli.read_activity(tmp_path, {"handle": "family"})["status"] == "stale"
    assert read_json(path)["status"] == status


def test_snapshot_pagination_advances_without_losing_bursts_or_completion_updates():
    observer = CodexObserver("root", {}, ".")
    seen = []
    def entry(identity, status="completed"):
        return {"turnId": "turn", "item": {"id": identity, "type": "commandExecution", "status": status}}
    def request(method, params):
        seen.append((method, params))
        if method == "thread/list": return {"data": [], "nextCursor": None}
        if method == "thread/items/read": return {"data": [entry("running")]}
        if params["sortDirection"] == "desc":
            return {"data": [entry("running", "inProgress")], "backwardsCursor": "initial", "nextCursor": "old"}
        if params["cursor"] == "initial":
            return {"data": [entry("a"), entry("b")], "nextCursor": "tail"}
        return {"data": [entry("c")], "nextCursor": None}
    observer.request = request
    _labels, first, gap = observer.snapshot()
    assert gap and any(item["item"]["status"] == "completed" for _thread, item in first)
    _labels, following, _gap = observer.snapshot()
    assert [item["item"]["id"] for _thread, item in following] == ["a", "b", "c"]
    assert observer.cursors["root"] == "tail"
    assert observer.pending == {}


def test_statusless_item_completion_is_reread_after_forward_cursor_moves():
    observer = CodexObserver("root", {}, ".")
    item = {"turnId": "turn", "completedAtMs": None,
            "item": {"id": "search", "type": "webSearch"}}
    phase = [0]
    def request(method, params):
        if method == "thread/list": return {"data": []}
        if method == "thread/items/read":
            return {"data": [item | {"completedAtMs": 123 if phase[0] else None}]}
        if params["sortDirection"] == "desc":
            return {"data": [item], "backwardsCursor": "initial"}
        if params["cursor"] == "initial":
            return {"data": [{"turnId": "turn", "item": {"id": str(index), "type": "agentMessage"}}
                             for index in range(32)], "nextCursor": "tail"}
        return {"data": []}
    observer.request = request
    observer.snapshot()
    assert ("root", "turn", "search") in observer.pending
    phase[0] = 1
    _labels, entries, _gap = observer.snapshot()
    assert observer.cursors["root"] == "tail"
    assert observation(*entries[-1])["state"] == "Completed"
    assert not observer.pending


def completed(**changes):
    return {"operation_id": "done", "state": "completed", "process_scope": "collected",
            "output_drain": "available", "finished_monotonic": 0,
            "activity_monotonic": 0, "layout_order": 1, **changes}


def state(**changes):
    return {"mode": "stable", "columns": 120, "rows": 24, "grouped_sections": True,
            "compact_idle": True, "selected_operation_id": "done", **changes}


def test_completed_sections_expire_at_configured_deadline_without_automatic_pin():
    plan = lambda now, **kw: plan_native_layout([completed()], state(), now=now, **kw)
    assert plan(59)["assignments"]
    assert not any(item['visible'] for item in plan(60)["assignments"])
    assert not plan(60)["page_operation_ids"]
    assert plan(60, completed_hide_seconds=120)["assignments"]
    assert not plan(120, completed_hide_seconds=120)["page_operation_ids"]
    assert plan_native_layout([completed(activity_monotonic=50)], state(), now=100)["assignments"]


@pytest.mark.parametrize("changes", [{"state": "running"}, {"state": "draining"},
    {"process_scope": "unknown"}, {"output_drain": "pending"}, {"input_required": True}])
def test_live_or_uncertain_operations_never_expire(changes):
    assert plan_native_layout([completed(**changes)], state(), now=1000)["assignments"]


def test_explicit_unfold_and_inspection_preserve_completed_section():
    pinned = state(expanded_operation_ids=["done"])
    assert plan_native_layout([completed()], pinned, now=1000)["assignments"]
    inspected = state(inspection_target_id="done", inspection_preserved_rows={})
    assert plan_native_layout([completed()], inspected, now=1000)["assignments"]
    pinned["expanded_operation_ids"] = []
    assert not plan_native_layout([completed()], pinned, now=1000)["page_operation_ids"]


def test_close_button_has_exact_click_target_and_help_aliases_preserve_literals():
    assert close_clicked(75, 0, 80)
    assert not close_clicked(73, 0, 80)
    assert not close_clicked(79, 0, 80)
    assert not close_clicked(75, 1, 80)
    assert help_aliases(["shell", "--?"]) == ["shell", "--help"]
    assert help_aliases(["test-run", "--", "-?", "--?"]) == ["test-run", "--", "-?", "--?"]


@pytest.mark.parametrize("alias", ["-?", "--?"])
@pytest.mark.parametrize("commands", [[], ["shell"], ["shell", "layout"], ["config"], ["media"]])
def test_public_cli_help_aliases(alias, commands):
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root / "ls/tools/localsetup.py"), *commands, alias],
                            cwd=root, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize("alias", ["-?", "--?"])
@pytest.mark.parametrize("commands", [[], ["doctor"], ["complete"]])
def test_lscli_help_aliases(alias, commands, capsys):
    from ls.core.agent.cli import main
    with pytest.raises(SystemExit) as result:
        main([*commands, alias])
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out
