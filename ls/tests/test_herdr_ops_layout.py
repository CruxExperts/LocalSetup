from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from ls.core.herdr_ops import layout
from ls.core.herdr_ops.errors import HerdrOpsError
from ls.core.terminal_ops import shell as terminal_shell


ENDPOINT = "/tmp/herdr-fixture.sock"


def pane(pane_id: str, *, workspace: str = "w1", terminal: str = "term-1", label: str | None = None, tab: str = "w1:t1", agent_status: str | None = "idle"):
    return {"pane_id": pane_id, "terminal_id": terminal, "workspace_id": workspace, "tab_id": tab, "focused": False, "label": label, "agent_status": agent_status, "revision": 0}


def layout_snapshot(workspace: str = "w1", tab: str = "w1:t1"):
    return {"workspace_id": workspace, "tab_id": tab, "zoomed": False, "area": {"x": 0, "y": 0, "width": 100, "height": 40}, "focused_pane_id": "w1:p1", "panes": [], "splits": []}


def _replacement_fixture(state: Path):
    owner_id = "a" * 32
    binding_key = owner_id + "b" * 32
    owner_metadata = {
        "api_version": 1,
        "binding_key": binding_key,
        "repository": "project",
        "controller_id": "controller",
        "host_alias": None,
        "parent_id": "w1:p1",
        "workspace_id": "w1",
        "machine_id": "machine",
    }
    owner = {
        "schema_version": 1,
        "owner_id": owner_id,
        "purpose": "managed_ops",
        "endpoint": str(Path(ENDPOINT).absolute()),
        "workspace_id": "w1",
        "caller_pane_id": "w1:p1",
        "pane_id": "w1:p8",
        "terminal_id": "old-terminal",
        "tab_id": "w1:t1",
        "cwd": "/work/project",
        "ratio": 1 / 3,
        "state": "owned",
        "owner_metadata": owner_metadata,
    }
    layout._write_owner(state, owner)
    owner_path = state / "panes" / f"{owner_id}.json"
    previous_binding = {
        "name": "project-sudo",
        "binding_key": binding_key,
        "backend": "herdr",
        "endpoint": ENDPOINT,
        "parent_id": "w1:p1",
        "pane_id": "w1:p8",
        "terminal_id": "old-terminal",
        "repository": "project",
        "controller_id": "controller",
        "host_alias": None,
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "pane_tty": "/dev/pts/200",
        "shell_pid": 900001,
        "shell_path": "/bin/bash",
        "machine_generation": "old-generation",
        "cwd": "/work/after-command",
        "state": "ready",
        "metadata": {},
    }
    return owner_id, owner_metadata, previous_binding, owner_path


def _missing_generation_snapshot(*extra_panes):
    return {
        "type": "session_snapshot",
        "snapshot": {
            "panes": [pane("w1:p1", terminal="parent-terminal"), *extra_panes],
        },
    }


def test_verify_missing_managed_generation_requires_unique_complete_absence_and_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    owner_id, metadata, previous, owner_path = _replacement_fixture(state)
    original = owner_path.read_bytes()
    monkeypatch.setattr(terminal_shell, "machine_generation", lambda: "new-generation")

    def snapshot_request(_endpoint, method, *_args, **_kwargs):
        if method != "session.snapshot":
            raise AssertionError(method)
        return _missing_generation_snapshot()
    monkeypatch.setattr(layout, "request", snapshot_request)

    layout.verify_missing_managed_pane(
        ENDPOINT, "w1", "w1:p1", "/work/project", owner_id, previous,
        state_dir=state, ratio=1 / 3, owner_metadata=metadata,
    )

    assert owner_path.read_bytes() == original


def test_verify_missing_managed_generation_rejects_reappearing_terminal_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    owner_id, metadata, previous, _owner_path = _replacement_fixture(state)
    monkeypatch.setattr(terminal_shell, "machine_generation", lambda: "new-generation")

    def snapshot_request(_endpoint, method, *_args, **_kwargs):
        if method != "session.snapshot":
            raise AssertionError(method)
        return _missing_generation_snapshot(pane("w1:p9", terminal="old-terminal"))
    monkeypatch.setattr(layout, "request", snapshot_request)

    with pytest.raises(HerdrOpsError) as caught:
        layout.verify_missing_managed_pane(
            ENDPOINT, "w1", "w1:p1", "/work/project", owner_id, previous,
            state_dir=state, ratio=1 / 3, owner_metadata=metadata,
        )

    assert caught.value.code == "replacement_generation_present"


@pytest.mark.parametrize(("pid_exists", "expected"), [(False, None), (True, "shell_generation_live")])
def test_verify_missing_requires_old_pid_absence_when_machine_generation_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_exists: bool, expected: str | None,
) -> None:
    state = tmp_path / "state"
    owner_id, metadata, previous, _owner_path = _replacement_fixture(state)
    monkeypatch.setattr(terminal_shell, "machine_generation", lambda: "old-generation")

    def stat_process(path, *args, **kwargs):
        if os.fspath(path) == f"/proc/{previous['shell_pid']}":
            if pid_exists:
                return os.stat_result((0,) * 10)
            raise FileNotFoundError(os.fspath(path))
        return original_stat(path, *args, **kwargs)

    original_stat = os.stat
    monkeypatch.setattr(os, "stat", stat_process)

    def snapshot_request(_endpoint, method, *_args, **_kwargs):
        if method != "session.snapshot":
            raise AssertionError(method)
        return _missing_generation_snapshot()
    monkeypatch.setattr(layout, "request", snapshot_request)

    if expected is None:
        layout.verify_missing_managed_pane(
            ENDPOINT, "w1", "w1:p1", "/work/project", owner_id, previous,
            state_dir=state, ratio=1 / 3, owner_metadata=metadata,
        )
    else:
        with pytest.raises(HerdrOpsError) as caught:
            layout.verify_missing_managed_pane(
                ENDPOINT, "w1", "w1:p1", "/work/project", owner_id, previous,
                state_dir=state, ratio=1 / 3, owner_metadata=metadata,
            )
        assert caught.value.code == expected


@pytest.mark.parametrize(
    ("first_failure", "expected_effect", "retry_succeeds"),
    [("none", "none", True), ("unknown", "unknown", False), ("scope", "unknown", False)],
)
def test_replacement_archives_before_split_and_retries_only_known_no_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_failure: str,
    expected_effect: str, retry_succeeds: bool,
) -> None:
    state = tmp_path / "state"
    owner_id, metadata, previous, owner_path = _replacement_fixture(state)
    original = owner_path.read_bytes()
    calls: list[str] = []
    split_attempts = 0

    def fake_request(_endpoint, method, params=None, **_kwargs):
        nonlocal split_attempts
        calls.append(method)
        if method == "session.snapshot":
            return _missing_generation_snapshot()
        if method == "pane.get":
            pane_id = params["pane_id"]
            if pane_id == "w1:p1":
                return {"pane": pane(pane_id, terminal="parent-terminal", label="parent")}
            if pane_id == "w1:p9":
                return {"pane": pane(pane_id, terminal="new-terminal", label="project-sudo")}
            raise HerdrOpsError("not_found", "pane not found")
        if method == "pane.layout":
            return {"layout": layout_snapshot()}
        if method == "pane.split":
            split_attempts += 1
            if split_attempts == 1:
                if first_failure == "scope":
                    return {"pane": pane("w1:p9", workspace="w2", terminal="new-terminal")}
                raise HerdrOpsError("split_result", "first split attempt", first_failure)
            return {"pane": pane("w1:p9", terminal="new-terminal", label="project-sudo")}
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    monkeypatch.setattr(terminal_shell, "machine_generation", lambda: "new-generation")

    with pytest.raises(HerdrOpsError) as caught:
        layout.ensure_owned_pane(
            ENDPOINT, "w1", "w1:p1", "/work/project", owner_id,
            state_dir=state, ratio=1 / 3, purpose="managed_ops",
            owner_metadata=metadata, previous_binding=previous,
        )
    assert caught.value.effect == expected_effect
    failed = json.loads(owner_path.read_text(encoding="utf-8"))
    assert failed["state"] == ("failed" if expected_effect == "none" else "unknown")
    assert failed["replacement"]["state"] == ("failed" if expected_effect == "none" else "unknown")
    generation_id = failed["generation_id"]
    archive_path = state / failed["replacement"]["owner_archive"]
    assert archive_path.read_bytes() == original
    assert failed["replacement"]["previous_owner_sha256"] == hashlib.sha256(original).hexdigest()

    if retry_succeeds:
        result = layout.ensure_owned_pane(
            ENDPOINT, "w1", "w1:p1", "/work/project", owner_id,
            state_dir=state, ratio=1 / 3, purpose="managed_ops",
            owner_metadata=metadata, previous_binding=previous,
        )
        assert result["pane"]["pane_id"] == "w1:p9"
        owned = json.loads(owner_path.read_text(encoding="utf-8"))
        assert owned["generation_id"] == generation_id
        assert owned["replacement"]["state"] == "owned"
        assert owned["replacement"]["owner_archive"] == failed["replacement"]["owner_archive"]
        assert split_attempts == 2
    else:
        with pytest.raises(HerdrOpsError) as repeated:
            layout.ensure_owned_pane(
                ENDPOINT, "w1", "w1:p1", "/work/project", owner_id,
                state_dir=state, ratio=1 / 3, purpose="managed_ops",
                owner_metadata=metadata, previous_binding=previous,
            )
        assert repeated.value.code == "replacement_owner_conflict"
        assert split_attempts == 1


def test_replacement_reservation_write_failure_is_unknown_and_never_splits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    owner_id, metadata, previous, owner_path = _replacement_fixture(state)
    original = owner_path.read_bytes()
    calls: list[str] = []

    def fake_request(_endpoint, method, params=None, **_kwargs):
        calls.append(method)
        if method == "session.snapshot":
            return _missing_generation_snapshot()
        if method == "pane.get":
            if params["pane_id"] == "w1:p1":
                return {"pane": pane("w1:p1", terminal="parent-terminal", label="parent")}
            raise HerdrOpsError("not_found", "pane not found")
        if method == "pane.layout":
            return {"layout": layout_snapshot()}
        raise AssertionError(method)

    def fail_owner_write(_root, _record):
        raise HerdrOpsError("owner_write_failed", "simulated owner reservation failure")

    monkeypatch.setattr(layout, "request", fake_request)
    monkeypatch.setattr(terminal_shell, "machine_generation", lambda: "new-generation")
    monkeypatch.setattr(layout, "_write_owner", fail_owner_write)

    with pytest.raises(HerdrOpsError) as caught:
        layout.ensure_owned_pane(
            ENDPOINT, "w1", "w1:p1", "/work/project", owner_id,
            state_dir=state, ratio=1 / 3, purpose="managed_ops",
            owner_metadata=metadata, previous_binding=previous,
        )
    assert caught.value.effect == "unknown"
    assert "pane.split" not in calls
    assert owner_path.read_bytes() == original


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
        if method == "pane.process_info":
            return {"process_info": {"shell_pid": 81, "foreground_processes": [{"pid": 81, "name": "bash"}]}}
        if method == "pane.read":
            return {"read": {"text": "$ "}}
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


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("occupant", ["agent", "command"])
def test_completed_task_cleanup_refuses_new_pane_occupant(tmp_path: Path, monkeypatch, dry_run: bool, occupant: str):
    state = tmp_path / "state"
    state.mkdir()
    task_id = "f" * 32
    layout._write_owner(state, {
        "owner_id": task_id, "purpose": "task", "endpoint": str(Path(ENDPOINT).absolute()),
        "workspace_id": "w1", "pane_id": "w1:p8", "terminal_id": "scratch", "state": "owned",
    })
    task_path = state / "tasks" / f"{task_id}.json"
    task_path.parent.mkdir()
    task_path.write_text(json.dumps({
        "task_id": task_id, "state": "completed", "nonce": "n",
        "receipt": {"kind": "atomic-exit", "task_id": task_id, "nonce": "n", "exit_code": 0},
    }))
    calls = []

    def fake_request(_endpoint, method, params=None, **_kwargs):
        calls.append(method)
        if method == "pane.get":
            return {"pane": pane("w1:p8", terminal="scratch", agent_status="active" if occupant == "agent" else "idle")}
        if method == "pane.process_info":
            assert occupant == "command"
            return {"process_info": {"shell_pid": 81, "foreground_processes": [{"pid": 82, "name": "python"}]}}
        if method == "pane.close":
            return {"type": "pane_closed"}
        raise AssertionError(method)

    monkeypatch.setattr(layout, "request", fake_request)
    with pytest.raises(HerdrOpsError) as caught:
        layout.cleanup_owned_pane(ENDPOINT, task_id, state_dir=state, dry_run=dry_run)
    assert caught.value.code == "pane_busy"
    assert "pane.close" not in calls
