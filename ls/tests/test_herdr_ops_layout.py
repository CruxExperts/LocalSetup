from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ls.core.herdr_ops import layout
from ls.core.herdr_ops.errors import HerdrOpsError


ENDPOINT = "/tmp/herdr-fixture.sock"


def pane(pane_id: str, *, workspace: str = "w1", terminal: str = "term-1", label: str | None = None, tab: str = "w1:t1", agent_status: str | None = "idle"):
    return {"pane_id": pane_id, "terminal_id": terminal, "workspace_id": workspace, "tab_id": tab, "focused": False, "label": label, "agent_status": agent_status, "revision": 0}


def layout_snapshot(workspace: str = "w1", tab: str = "w1:t1"):
    return {"workspace_id": workspace, "tab_id": tab, "zoomed": False, "area": {"x": 0, "y": 0, "width": 100, "height": 40}, "focused_pane_id": "w1:p1", "panes": [], "splits": []}


def test_create_uses_explicit_scope_down_ratio_cwd_and_no_focus(tmp_path: Path, monkeypatch):
    calls = []

    def fake_request(endpoint, method, params=None, **kwargs):
        calls.append((endpoint, method, params, kwargs))
        if method == "pane.get":
            return {"type": "pane_info", "pane": pane(params["pane_id"], terminal="caller")}
        if method == "pane.layout":
            return {"type": "pane_layout", "layout": layout_snapshot()}
        if method == "pane.split":
            return {"type": "pane_info", "pane": pane("w1:p2", terminal="new-terminal", tab="w1:t1")}
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    state = tmp_path / "private-state"
    result = layout.create_owned_pane(ENDPOINT, "w1", "w1:p1", "/work/project", state_dir=state)
    split = next(call for call in calls if call[1] == "pane.split")
    assert split[2] == {
        "workspace_id": "w1",
        "target_pane_id": "w1:p1",
        "direction": "down",
        "ratio": 0.3,
        "cwd": "/work/project",
        "focus": False,
    }
    assert split[3]["mutation"] is True
    assert result["pane"]["pane_id"] == "w1:p2"
    record_path = state / "panes" / f"{result['owner_id']}.json"
    record = json.loads(record_path.read_text())
    assert record["terminal_id"] == "new-terminal"
    assert os.stat(record_path).st_mode & 0o777 == 0o600


def test_create_refuses_protected_sudo_pane_before_any_mutation(tmp_path: Path, monkeypatch):
    calls = []

    def fake_request(endpoint, method, params=None, **kwargs):
        calls.append(method)
        return {"type": "pane_info", "pane": pane("w1:p1", label="sudo")}

    monkeypatch.setattr(layout, "request", fake_request)
    with pytest.raises(HerdrOpsError) as caught:
        layout.create_owned_pane(ENDPOINT, "w1", "w1:p1", "/work", state_dir=tmp_path / "state")
    assert caught.value.code == "protected_pane"
    assert calls == ["pane.get"]


def test_create_rejects_caller_outside_workspace(tmp_path: Path, monkeypatch):
    def fake_request(endpoint, method, params=None, **kwargs):
        return {"type": "pane_info", "pane": pane("w2:p1", workspace="w2")}

    monkeypatch.setattr(layout, "request", fake_request)
    with pytest.raises(HerdrOpsError) as caught:
        layout.create_owned_pane(ENDPOINT, "w1", "w2:p1", "/work", state_dir=tmp_path / "state")
    assert caught.value.code == "scope_mismatch"


def test_move_rebinds_returned_id_and_cleanup_closes_only_owned_pane(tmp_path: Path, monkeypatch):
    calls = []
    state = tmp_path / "state"
    state.mkdir()
    owner_id = "a" * 32
    record = {
        "schema_version": 1,
        "owner_id": owner_id,
        "purpose": "test",
        "endpoint": str(Path(ENDPOINT).absolute()),
        "workspace_id": "w1",
        "caller_pane_id": "w1:p1",
        "pane_id": "w1:p2",
        "terminal_id": "owned-terminal",
        "tab_id": "w1:t1",
        "state": "owned",
    }
    layout._write_owner(state, record)

    def fake_request(endpoint, method, params=None, **kwargs):
        calls.append((method, params, kwargs))
        if method == "pane.get" and params["pane_id"] == "w1:p2":
            return {"type": "pane_info", "pane": pane("w1:p2", terminal="owned-terminal")}
        if method == "pane.get" and params["pane_id"] == "w2:p5":
            return {"type": "pane_info", "pane": pane("w2:p5", workspace="w2", terminal="destination", tab="w2:t1")}
        if method == "pane.get" and params["pane_id"] == "w2:p9":
            return {"type": "pane_info", "pane": pane("w2:p9", workspace="w2", terminal="owned-terminal", tab="w2:t1")}
        if method == "pane.move":
            moved = pane("w2:p9", workspace="w2", terminal="owned-terminal", tab="w2:t1")
            return {"type": "pane_move", "move_result": {"changed": True, "pane": moved, "target_layout": layout_snapshot("w2", "w2:t1"), "focused_pane_id": "w2:p5"}}
        if method == "pane.close":
            return {"type": "pane_closed"}
        if method == "pane.process_info":
            return {"process_info": {"shell_pid": 81, "foreground_processes": [{"pid": 81, "name": "bash"}]}}
        if method == "pane.read":
            return {"read": {"text": "$ "}}
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    moved = layout.move_owned_pane(ENDPOINT, owner_id, "w2:p5", target_workspace_id="w2", state_dir=state)
    assert moved["pane"]["pane_id"] == "w2:p9"
    move_request = next(call for call in calls if call[0] == "pane.move")
    assert move_request[1]["pane_id"] == "w1:p2"
    assert move_request[1]["destination"]["target_pane_id"] == "w2:p5"
    assert move_request[1]["focus"] is False
    assert json.loads((state / "panes" / f"{owner_id}.json").read_text())["pane_id"] == "w2:p9"

    closed = layout.cleanup_owned_pane(ENDPOINT, owner_id, state_dir=state)
    close_request = next(call for call in calls if call[0] == "pane.close")
    assert close_request[1] == {"pane_id": "w2:p9"}
    assert closed["state"] == "closed"
    assert not any(method == "layout.apply" for method, *_ in calls)


def test_cleanup_rebinds_after_id_change_and_does_not_restore_stale_geometry(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    owner_id = "b" * 32
    layout._write_owner(
        state,
        {
            "owner_id": owner_id,
            "endpoint": str(Path(ENDPOINT).absolute()),
            "workspace_id": "w1",
            "caller_pane_id": "w1:p1",
            "pane_id": "w1:p2",
            "terminal_id": "owned-terminal",
            "state": "owned",
            "before_layout": {"version": "old"},
        },
    )
    calls = []

    def fake_request(endpoint, method, params=None, **kwargs):
        calls.append((method, params))
        if method == "pane.get" and params["pane_id"] == "w1:p2":
            raise HerdrOpsError("not_found", "pane not found")
        if method == "session.snapshot":
            return {"type": "session_snapshot", "snapshot": {"panes": [pane("w3:p8", workspace="w3", terminal="owned-terminal", tab="w3:t4")]}}
        if method == "pane.close":
            return {"type": "pane_closed"}
        if method == "pane.process_info":
            return {"process_info": {"shell_pid": 81, "foreground_processes": [{"pid": 81, "name": "bash"}]}}
        if method == "pane.read":
            return {"read": {"text": "$ "}}
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    result = layout.cleanup_owned_pane(ENDPOINT, owner_id, state_dir=state)
    assert result["pane_id"] == "w3:p8"
    assert ("pane.close", {"pane_id": "w3:p8"}) in calls
    assert not any(method in {"layout.apply", "layout.set_split_ratio"} for method, _ in calls)


def test_cleanup_dry_run_reads_state_without_creating_state_or_mutating_api(tmp_path: Path, monkeypatch):
    state = tmp_path / "missing"
    with pytest.raises(HerdrOpsError) as caught:
        layout.cleanup_owned_pane(ENDPOINT, "c" * 32, state_dir=state, dry_run=True)
    assert caught.value.code in {"invalid_state_dir", "state_dir_unavailable"}
    assert not state.exists()


def test_ensure_reuses_only_the_same_stable_idle_owned_pane(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    calls = []

    def fake_request(endpoint, method, params=None, **kwargs):
        calls.append((method, params))
        if method == "pane.get":
            return {"type": "pane_info", "pane": pane(params["pane_id"], terminal="caller" if params["pane_id"] == "w1:p1" else "scratch", agent_status="unknown" if params["pane_id"] == "w1:p8" else "idle")}
        if method == "pane.layout":
            return {"type": "pane_layout", "layout": layout_snapshot()}
        if method == "pane.split":
            return {"type": "pane_info", "pane": pane("w1:p8", terminal="scratch")}
        if method == "pane.process_info":
            return {"type": "pane_process_info", "process_info": {"shell_pid": 89, "foreground_processes": [{"pid": 89, "name": "bash"}]}}
        if method == "pane.read":
            return {"type": "pane_read", "read": {"text": "$ "}}
        if method == "session.snapshot":
            return {"type": "session_snapshot", "snapshot": {"agents": []}}
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    owner_id = "d" * 32
    created = layout.ensure_owned_pane(ENDPOINT, "w1", "w1:p1", "/work", owner_id, state_dir=state)
    reused = layout.ensure_owned_pane(ENDPOINT, "w1", "w1:p1", "/work", owner_id, state_dir=state)
    assert created["pane"]["pane_id"] == reused["pane"]["pane_id"] == "w1:p8"
    assert reused["reused"] is True
    assert sum(method == "pane.split" for method, _ in calls) == 1


def test_ensure_refuses_changed_or_busy_owned_pane_without_duplicate_split(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    shell = {"text": "$ "}
    calls = []

    def fake_request(endpoint, method, params=None, **kwargs):
        calls.append(method)
        if method == "pane.get":
            return {"type": "pane_info", "pane": pane(params["pane_id"], terminal="caller" if params["pane_id"] == "w1:p1" else "scratch")}
        if method == "pane.layout":
            return {"type": "pane_layout", "layout": layout_snapshot()}
        if method == "pane.split":
            return {"type": "pane_info", "pane": pane("w1:p8", terminal="scratch")}
        if method == "pane.process_info":
            return {"type": "pane_process_info", "process_info": {"shell_pid": 89, "foreground_processes": [{"pid": 89, "name": "bash"}]}}
        if method == "pane.read":
            return {"type": "pane_read", "read": shell}
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    owner_id = "e" * 32
    layout.ensure_owned_pane(ENDPOINT, "w1", "w1:p1", "/work", owner_id, state_dir=state)
    shell["text"] = "$ echo in progress"
    with pytest.raises(HerdrOpsError) as caught:
        layout.ensure_owned_pane(ENDPOINT, "w1", "w1:p1", "/work", owner_id, state_dir=state)
    assert caught.value.code == "pane_changed"
    assert calls.count("pane.split") == 1


def test_ensure_records_unknown_split_and_never_replays_it(tmp_path: Path, monkeypatch):
    calls = []

    def fake_request(endpoint, method, params=None, **kwargs):
        calls.append(method)
        if method == "pane.get":
            return {"type": "pane_info", "pane": pane("w1:p1", terminal="caller")}
        if method == "pane.layout":
            return {"type": "pane_layout", "layout": layout_snapshot()}
        if method == "pane.split":
            raise HerdrOpsError("response_lost", "unknown", "unknown")
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    state = tmp_path / "state"
    owner_id = "1" * 32
    with pytest.raises(HerdrOpsError) as caught:
        layout.ensure_owned_pane(ENDPOINT, "w1", "w1:p1", "/work", owner_id, state_dir=state)
    assert caught.value.effect == "unknown"
    assert owner_id in caught.value.message
    with pytest.raises(HerdrOpsError) as repeated:
        layout.ensure_owned_pane(ENDPOINT, "w1", "w1:p1", "/work", owner_id, state_dir=state)
    assert repeated.value.code == "owner_conflict"
    assert calls.count("pane.split") == 1


def test_generic_cleanup_requires_task_terminal_exit_receipt(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    owner_id = "f" * 32
    layout._write_owner(
        state,
        {
            "owner_id": owner_id,
            "purpose": "task",
            "endpoint": str(Path(ENDPOINT).absolute()),
            "workspace_id": "w1",
            "caller_pane_id": "w1:p1",
            "pane_id": "w1:p8",
            "terminal_id": "scratch",
            "tab_id": "w1:t1",
            "state": "owned",
        },
    )
    task_path = state / "tasks" / f"{owner_id}.json"
    task_path.parent.mkdir()
    task_path.write_text(json.dumps({"task_id": owner_id, "state": "running", "nonce": "n"}))
    calls = []

    def fake_request(endpoint, method, params=None, **kwargs):
        calls.append((method, params))
        if method == "pane.get":
            return {"type": "pane_info", "pane": pane("w1:p8", terminal="scratch")}
        if method == "pane.close":
            return {"type": "pane_closed"}
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    with pytest.raises(HerdrOpsError) as caught:
        layout.cleanup_owned_pane(ENDPOINT, owner_id, state_dir=state)
    assert caught.value.code == "task_active"
    assert not any(method == "pane.close" for method, _ in calls)

    task_path.write_text(json.dumps({"task_id": owner_id, "state": "failed", "nonce": "n", "receipt": {"kind": "atomic-exit", "task_id": owner_id, "nonce": "n", "exit_code": 9}}))
    result = layout.cleanup_owned_pane(ENDPOINT, owner_id, state_dir=state)
    assert result["state"] == "closed"
    assert ("pane.close", {"pane_id": "w1:p8"}) in calls
