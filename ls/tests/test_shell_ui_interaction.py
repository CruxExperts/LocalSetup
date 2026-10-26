"""Regression checks for fresh native canvas choices and safe inspection."""

from contextlib import contextmanager
from pathlib import Path
import json
import fcntl
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import time
import termios

import pytest

from ls.core.terminal_ops.command_native_layout import plan_native_layout
from ls.core.terminal_ops.command_native_backend import NativeCanvasV3
from ls.core.terminal_ops.command_service import (
    ShellCommandService, _fresh_native_layout, _native_canvas_settings,
)
from ls.core.terminal_ops.command_state import ShellCommandError, atomic_json, read_json


def operation(name: str, order: int, *, activity: float = 1) -> dict:
    return {"operation_id": name, "layout_order": order, "state": "running",
            "activity_monotonic": activity, "native_min_rows": 3}


def test_fresh_preferences_read_root_values_and_legacy_remains_explicit() -> None:
    runtime = {"layout": {"native_canvas": {"default_columns": 120,
                                              "default_rows": 24}},
               "startup_mode": "follow", "inspection_enabled": False,
               "inspection_duration_seconds": 22, "reflow_debounce_ms": 135,
               "double_escape_ms": 650}
    settings = _native_canvas_settings(runtime)
    state = _fresh_native_layout(runtime)
    assert (state["mode"], state["grouped_sections"]) == ("follow", True)
    assert (settings["inspection_enabled"], settings["inspection_duration_seconds"],
            settings["reflow_debounce_ms"], settings["double_escape_ms"]) == (
                False, 22, 135, 650)


def test_inspection_uses_configured_timer_and_reflow_delay(tmp_path: Path, monkeypatch) -> None:
    runtime = {"layout": {"native_canvas": {"default_columns": 120,
                                              "default_rows": 24}},
               "inspection_duration_seconds": 22, "reflow_debounce_ms": 135}
    state = _fresh_native_layout(runtime)
    state.update(mode="stable", operation_order=["a", "b"],
                 visible_operation_ids=["a"], selected_operation_id="a")
    path = tmp_path / "session.json"
    session = {"handle": "fixture", "layout_mode": "canvas-v3",
               "native_layout": state, "runtime_settings": runtime}
    atomic_json(path, session)
    service = ShellCommandService(tmp_path / "state")
    monkeypatch.setattr("ls.core.terminal_ops.command_service.time.monotonic", lambda: 100.0)
    monkeypatch.setattr(service, "_plan_native_operations", lambda *_args: {
        "assignments": [{"operation_id": "a", "visible": True, "rows": 19},
                        {"operation_id": "b", "visible": False, "rows": 3}]})
    service._begin_native_inspection_locked(tmp_path, session,
                                            [operation("a", 0), operation("b", 1)],
                                            "b", defer=True)
    saved = read_json(path)["native_layout"]
    assert saved["inspection_until_monotonic"] == 122.0
    assert saved["manual_due_monotonic"] == pytest.approx(100.135)


def test_ordered_groups_keep_one_row_per_job_and_equal_stable_shares() -> None:
    jobs = [operation(name, index) for index, name in enumerate("abcde")]
    state = {"grouped_sections": True, "compact_idle": True, "mode": "stable",
             "rows": 30, "columns": 120, "max_sections": 8,
             "operation_order": list("abcde"), "visible_operation_ids": ["b", "e"],
             "selected_operation_id": "e", "weights": {"b": 1, "e": 9}}
    plan = plan_native_layout(jobs, state, now=100)
    assert [(item["kind"], item.get("operation_ids", item.get("operation_id")), item["rows"])
            for item in plan["sections"]] == [
                ("summary", ["a"], 1), ("operation", "b", 11),
                ("summary", ["c", "d"], 2), ("operation", "e", 10)]
    assert [group["summary_index"] for group in plan["collapsed_groups"]] == [0, 1]


def test_grouped_summary_shows_purpose_location_and_semantic_state(monkeypatch) -> None:
    from ls.core.terminal_ops import command_canvas
    from ls.core.terminal_ops.command_theme import theme_definition
    theme = theme_definition("midnight")
    session = {"state": "ready", "runtime_settings": {"theme": theme},
               "native_title_states": {"b": "Failed"},
               "native_layout": {"grouped_sections": True,
                                 "collapsed_groups": [{"operation_ids": ["b"]}],
                                 "collapsed_sections": [{"operation_id": "b", "state": "idle",
                                                         "pane_label": "builder /project"}]}}
    output: list[bytes] = []
    monkeypatch.setattr(command_canvas, "read_json", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(command_canvas, "_terminal_size", lambda: (80, 1))
    monkeypatch.setattr(command_canvas.os, "write", lambda _fd, data: output.append(data))
    monkeypatch.setattr(command_canvas.time, "sleep", lambda _delay: (_ for _ in ()).throw(StopIteration))
    with pytest.raises(StopIteration):
        command_canvas.run_native_summaries(Path("/tmp/unused/session.json"))
    rendered = output[0]
    assert b"builder /project" in rendered and b"[Failed]" in rendered
    error = theme["roles"]["error"]
    rgb = ";".join(str(int(error[index:index + 2], 16)) for index in (1, 3, 5))
    assert f"38;2;{rgb}".encode() in rendered


def test_grouped_page_respects_renderer_capacity_with_larger_profile_limit() -> None:
    jobs = [operation(f"job{index}", index) for index in range(16)]
    state = {"grouped_sections": True, "compact_idle": True, "mode": "stable",
             "rows": 100, "columns": 120, "max_sections": 16,
             "operation_order": [job["operation_id"] for job in jobs],
             "visible_operation_ids": [job["operation_id"] for job in jobs],
             "selected_operation_id": "job0", "weights": {}}
    plan = plan_native_layout(jobs, state, now=100)
    assert plan["page_count"] == 2
    assert len(plan["sections"]) == 8
    assert plan["blocked"] is None


def test_inspection_preserves_upper_geometry_when_bottom_has_no_space() -> None:
    jobs = [operation("a", 0), operation("b", 1)]
    state = {"grouped_sections": True, "compact_idle": True, "mode": "stable",
             "rows": 24, "columns": 120, "max_sections": 8,
             "operation_order": ["a", "b"], "visible_operation_ids": ["a", "b"],
             "selected_operation_id": "b", "inspection_target_id": "b",
             "inspection_preserved_rows": {"a": 19}, "weights": {}}
    plan = plan_native_layout(jobs, state, now=100)
    assert [(item["kind"], item["rows"]) for item in plan["sections"]] == [
        ("operation", 19), ("summary", 1)]
    assert [item["operation_id"] for item in plan["assignments"] if item["visible"]] == ["a"]


def test_inspection_cannot_replay_unknown_layout_ack(tmp_path: Path, monkeypatch) -> None:
    runtime = {"layout": {"native_canvas": {"default_columns": 120,
                                              "default_rows": 24}},
               "inspection_enabled": True}
    state = _fresh_native_layout(runtime)
    state.update(mode="stable", operation_order=["a", "b"],
                 visible_operation_ids=["a"], selected_operation_id="a",
                 desired_generation=1, applied_generation=0,
                 layout_state="unknown", pending=True,
                 requested_plan={"generation": 1, "page": 0, "page_count": 1,
                                 "collapsed_sections": [{"operation_id": "b", "state": "idle"}],
                                 "collapsed_groups": [{"kind": "summary", "summary_index": 0,
                                                       "operation_ids": ["b"], "rows": 1}]})
    path = tmp_path / "session.json"
    session = {"handle": "fixture", "layout_mode": "canvas-v3", "native_layout": state,
               "runtime_settings": runtime}
    atomic_json(path, session)

    class Backend:
        calls = 0

        def apply_native_layout(self, *_args):
            self.calls += 1
            raise AssertionError("A second native layout dispatch is forbidden")

    backend = Backend()
    service = ShellCommandService(tmp_path / "state", backend=backend)
    jobs = [operation("a", 0), operation("b", 1)]
    monkeypatch.setattr(service, "_verify", lambda *_args: {
        "state": "ready", "applied_generation": 1})
    monkeypatch.setattr(service, "_native_canvas_operations", lambda *_args: jobs)
    monkeypatch.setattr(service, "_native_inventory", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service, "_native_plan_readback_matches", lambda *_args: True)
    service._begin_native_inspection_locked(tmp_path, session, jobs, "b")
    after = read_json(path)["native_layout"]
    assert backend.calls == 0
    assert after["applied_generation"] == 1
    assert after["desired_generation"] == 2
    assert after["collapsed_groups"][0]["operation_ids"] == ["b"]


def test_retirement_unknown_viewer_detach_is_never_replayed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "session.json"
    session = {"handle": "fixture", "layout_mode": "canvas-v3", "state": "retiring",
               "native_layout": {"grouped_sections": True, "columns": 120},
               "retirement_armed_at": 1, "runtime_settings": {"auto_close_delay_seconds": 30},
               "endpoint": "/tmp/unused-tmux.sock"}
    atomic_json(path, session)

    class Backend:
        calls = 0

        def _clients(self, _session):
            return [{"pid": 123, "activity_at": 1}]

        def detach_native_client(self, _session, _pid):
            self.calls += 1
            raise ShellCommandError("detach_ack_unknown", "Lost acknowledgement.", effect="unknown")

    backend = Backend()
    service = ShellCommandService(tmp_path / "state", backend=backend, clock=lambda: 100)
    monkeypatch.setattr(service, "_strict_ops", lambda *_args: [])
    monkeypatch.setattr(service, "_terminal_ops_collected", lambda *_args: True)
    monkeypatch.setattr(service, "_verify", lambda *_args: {
        "state": "ready", "session": {"attached": 1}, "controls": {"copy_mode": False}})
    monkeypatch.setattr(service, "_pins", lambda *_args, **_kwargs: ([{"viewer_pid": 123}], False))
    service._native_retirement_tick_locked(tmp_path, session)
    assert backend.calls == 1
    saved = read_json(path)
    assert saved["cleanup_state"] == "unknown"
    assert saved["retirement_detaches"]["123"]["state"] == "unknown"
    service._native_retirement_tick_locked(tmp_path, saved)
    assert backend.calls == 1


def test_retirement_reconciles_absent_lost_ack_with_another_reader(tmp_path: Path,
                                                                 monkeypatch) -> None:
    path = tmp_path / "session.json"
    session = {"handle": "fixture", "layout_mode": "canvas-v3", "state": "retiring",
               "native_layout": {"grouped_sections": True, "columns": 120},
               "retirement_armed_at": 1, "runtime_settings": {"auto_close_delay_seconds": 30},
               "endpoint": "/tmp/unused-tmux.sock"}
    atomic_json(path, session)

    class Backend:
        calls = 0
        clients = [{"pid": 123, "activity_at": 1}, {"pid": 456, "activity_at": 95}]

        def _clients(self, _session):
            return self.clients

        def detach_native_client(self, _session, pid):
            self.calls += 1
            self.clients = [client for client in self.clients if client["pid"] != pid]
            raise ShellCommandError("detach_ack_unknown", "Lost acknowledgement.", effect="unknown")

    backend = Backend()
    service = ShellCommandService(tmp_path / "state", backend=backend, clock=lambda: 100)
    monkeypatch.setattr(service, "_strict_ops", lambda *_args: [])
    monkeypatch.setattr(service, "_terminal_ops_collected", lambda *_args: True)
    monkeypatch.setattr(service, "_verify", lambda *_args: {
        "state": "ready", "session": {"attached": len(backend.clients)},
        "controls": {"copy_mode": False}})
    monkeypatch.setattr(service, "_pins", lambda *_args, **_kwargs: (
        [{"viewer_pid": 123}, {"viewer_pid": 456}], False))
    service._native_retirement_tick_locked(tmp_path, session)
    assert backend.calls == 1
    saved = read_json(path)
    assert saved["retirement_detaches"]["123"]["state"] == "unknown"
    service._native_retirement_tick_locked(tmp_path, saved)
    saved = read_json(path)
    assert backend.calls == 1
    assert saved["retirement_detaches"]["123"]["state"] == "verified"
    assert saved["cleanup_state"] == "waiting_viewers"


def test_nonprimary_exit_requires_promoted_exact_viewer(tmp_path: Path, monkeypatch) -> None:
    from ls.core.terminal_ops import command_service
    path = tmp_path / "session.json"
    atomic_json(path, {"handle": "fixture", "layout_mode": "canvas-v3",
                       "session_record_path": str(path), "state": "ready"})

    class Backend:
        calls: list[int] = []
        def _clients(self, _session):
            return [{"pid": 456, "flags": {"ignore-size", "attached"}}]
        def detach_native_client(self, _session, pid):
            self.calls.append(pid)

    backend = Backend()
    service = ShellCommandService(tmp_path / "state", backend=backend)
    pins = [{"pid": 123, "starttime": 10, "viewer_pid": None,
             "primary_viewport": False}]
    monkeypatch.setattr(service, "_pins", lambda *_args, **_kwargs: (pins, False))
    monkeypatch.setattr(command_service, "process_info", lambda pid: (
        {"pid": 123, "starttime": 10, "ppid": 1} if pid == 123 else
        {"pid": 456, "starttime": 20, "ppid": 123} if pid == 456 else None))
    with pytest.raises(ShellCommandError, match="exact attached viewer"):
        service._native_control_event(path, 456, "exit")
    assert backend.calls == []
    pins[0]["viewer_pid"] = 456
    result = service._native_control_event(path, 456, "exit")
    assert result["state"] == "detached"
    assert backend.calls == [456]


def _two_viewer_escape_fixture(tmp_path: Path, monkeypatch):
    from ls.core.terminal_ops import command_service
    runtime = {"layout": {"native_canvas": {"default_columns": 120,
                                                 "default_rows": 24}},
               "double_escape_ms": 500}
    state = _fresh_native_layout(runtime)
    path = tmp_path / "session.json"
    session = {"handle": "fixture", "layout_mode": "canvas-v3",
               "session_record_path": str(path), "state": "ready",
               "runtime_settings": runtime, "native_layout": state}
    atomic_json(path, session)

    class Backend:
        detached: list[int] = []
        def _clients(self, _session):
            return [{"pid": pid, "flags": {"ignore-size", "attached"}}
                    for pid in (456, 457)]
        def detach_native_client(self, _session, pid):
            self.detached.append(pid)

    backend = Backend()
    service = ShellCommandService(tmp_path / "state", backend=backend)
    pins = [{"pid": 123, "starttime": 10, "viewer_pid": 456,
             "primary_viewport": True},
            {"pid": 124, "starttime": 11, "viewer_pid": 457,
             "primary_viewport": False}]
    processes = {123: {"starttime": 10, "ppid": 1},
                 124: {"starttime": 11, "ppid": 1},
                 456: {"starttime": 20, "ppid": 123},
                 457: {"starttime": 21, "ppid": 124}}
    controls = {"copy_mode": False}
    moment = [100.0]
    monkeypatch.setattr(service, "_pins", lambda *_args, **_kwargs: (pins, False))
    monkeypatch.setattr(command_service, "process_info", lambda pid: processes.get(pid))
    monkeypatch.setattr(service, "_verify", lambda *_args: {
        "state": "ready", "controls": controls})
    monkeypatch.setattr(command_service.time, "monotonic", lambda: moment[0])
    return service, path, backend, controls, moment


def test_double_escape_is_armed_per_exact_viewer(tmp_path: Path, monkeypatch) -> None:
    service, path, backend, _controls, moment = _two_viewer_escape_fixture(tmp_path, monkeypatch)
    assert service._native_control_event(path, 456, "escape")["state"] == "escape_armed"
    moment[0] = 100.1
    assert service._native_control_event(path, 457, "escape")["state"] == "escape_armed"
    assert backend.detached == []
    moment[0] = 100.2
    assert service._native_control_event(path, 457, "escape")["state"] == "detached"
    assert backend.detached == [457]
    moment[0] = 100.3
    assert service._native_control_event(path, 456, "escape")["state"] == "detached"
    assert backend.detached == [457, 456]


def test_merged_escape_pair_detaches_only_qualified_viewer(tmp_path: Path,
                                                            monkeypatch) -> None:
    from ls.core.terminal_ops.command_native_backend import native_control_bindings
    service, path, backend, controls, _moment = _two_viewer_escape_fixture(tmp_path, monkeypatch)
    bindings = native_control_bindings(read_json(path))
    assert "M-Escape" in bindings and "escape_pair" in bindings["M-Escape"][-1]
    assert "M-Escape" not in native_control_bindings({"session_record_path": str(path),
                                                     "native_layout": {"grouped_sections": False}})
    controls["copy_mode"] = True
    assert service._native_control_event(path, 457, "escape_pair")["state"] == "escape_consumed"
    assert backend.detached == []
    controls["copy_mode"] = False
    assert service._native_control_event(path, 457, "escape_pair")["state"] == "detached"
    assert backend.detached == [457]


def test_grouped_pane_menu_uses_purpose_location_and_state(tmp_path: Path, monkeypatch) -> None:
    from ls.core.terminal_ops import command_service
    runtime = {"layout": {"native_canvas": {"default_columns": 120,
                                                 "default_rows": 24}}}
    state = _fresh_native_layout(runtime)
    state["selected_operation_id"] = "build"
    path = tmp_path / "session.json"
    atomic_json(path, {"handle": "fixture", "layout_mode": "canvas-v3",
                       "session_record_path": str(path), "state": "ready",
                       "runtime_settings": runtime, "native_layout": state})
    captured: list[dict] = []

    class Backend:
        def show_native_menu(self, _session, _pid, _title, entries, *, on_spawn):
            captured.extend(entries)
            return {"state": "dismissed", "verified": True, "selected_index": 0}

    service = ShellCommandService(tmp_path / "state", backend=Backend())
    monkeypatch.setattr(command_service, "process_info", lambda _pid: {
        "starttime": 10, "tty_nr": 1})
    monkeypatch.setattr(service, "_verify", lambda *_args: {"state": "ready"})
    monkeypatch.setattr(service, "_native_primary_client", lambda *_args: True)
    monkeypatch.setattr(service, "_native_inventory", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service, "_native_canvas_operations", lambda *_args: [
        {"operation_id": "build", "pane_label": "root · build · /project",
         "state": "failed"}])
    def close(_directory, current, native_state):
        native_state["overlay_lock"] = None
        current["native_layout"] = native_state
        atomic_json(path, current)
        return True
    monkeypatch.setattr(service, "_native_close_overlay_locked", close)
    dispatched: list[tuple[str, str | None]] = []
    def apply(_path, _pid, action, *, operation_id=None):
        assert read_json(path)["native_layout"]["overlay_lock"] is None
        dispatched.append((action, operation_id))
        return {"state": "selected", "ok": True}
    monkeypatch.setattr(service, "_native_control_event", apply)
    assert service._native_open_menu(path, 456, section="panes")["state"] == "selected"
    assert any("root · build · /project [Failed]" in entry["label"] for entry in captured)
    assert dispatched == [("select", "build")]


def test_grouped_copy_escape_binding_survives_tmux_readback(tmp_path: Path) -> None:
    from ls.core.terminal_ops.command_backend import TmuxCommandBackend
    from ls.core.terminal_ops.command_native_backend import native_copy_escape_binding
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is unavailable")
    socket = str(tmp_path / "copy.sock")
    record = {"session_record_path": str(tmp_path / "session.json")}
    backend = TmuxCommandBackend(tmux_binary=tmux, config_file="/dev/null")
    subprocess.run([tmux, "-f", "/dev/null", "-S", socket,
                    "new-session", "-d", "-s", "fixture"], check=True,
                   capture_output=True)
    try:
        backend._checked(socket, ["bind-key", "-T", "copy-mode", "Escape",
                                  *native_copy_escape_binding(record, bind=True)], mutation=True)
        backend._checked(socket, ["bind-key", "-T", "copy-mode", "M-Escape",
                                  *native_copy_escape_binding(record, bind=True, pair=True)], mutation=True)
        observed = backend._observer_bindings(socket, "copy-mode")
        assert observed["Escape"] == native_copy_escape_binding(record)
        assert observed["M-Escape"] == native_copy_escape_binding(record, pair=True)
    finally:
        subprocess.run([tmux, "-f", "/dev/null", "-S", socket,
                        "kill-server"], check=False, capture_output=True)


@pytest.mark.parametrize("keys,selected", [
    (b"\t\r", 1),
    (b"\t\x1b[Z\r", 0),
    (b"\x1bOB\x1bOA\r", 0),
    (b"\x1b[<0;5;3M\x1b[<0;5;3m", 1),
    (b"\x1b[<0;95;1M\x1b[<0;95;1m", None),
    (b"\x1b", None),
])
def test_grouped_menu_pty_navigation_and_cancel(tmp_path: Path, monkeypatch,
                                                keys: bytes, selected: int | None) -> None:
    from ls.core.terminal_ops.command_theme import theme_definition
    class Backend:
        tmux_binary = "tmux"
        config_file = "/dev/null"
    native = NativeCanvasV3(Backend())
    monkeypatch.setattr(NativeCanvasV3, "_client", lambda *_args: {
        "name": "/dev/pts/1", "pid": 123})
    def run_popup(_self, _record, _client, argv, **_kwargs):
        program = argv[argv.index("-E") + 3]
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 25, 100, 0, 0))
        env = dict(os.environ, TERM="xterm-256color")
        try:
            child = subprocess.Popen([sys.executable, "-c", program, argv[-2], argv[-1]],
                                     stdin=slave, stdout=slave, stderr=slave,
                                     env=env, start_new_session=True)
            os.close(slave)
            output = b""
            deadline = time.monotonic() + 3
            while b"Appearance" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output += os.read(master, 8192)
            assert b"Appearance" in output
            os.write(master, keys)
            child.wait(timeout=3)
            assert child.returncode == 0
            return {"state": "dismissed", "verified": True}
        finally:
            os.close(master)
    monkeypatch.setattr(NativeCanvasV3, "_run_overlay_client", run_popup)
    record = {"endpoint": str(tmp_path / "fixture.sock"), "status_pane_id": "%1",
              "session_record_path": str(tmp_path / "session.json"),
              "native_layout": {"grouped_sections": True},
              "runtime_settings": {"theme": theme_definition("midnight")}}
    entries = [{"label": label, "action": "menu", "operation_id": slug}
               for label, slug in (("Appearance", "appearance"),
                                   ("Reading/layout", "reading"),
                                   ("Status detail", "status"))]
    outcome = native.show_menu(record, 123, "Menu", entries, on_spawn=lambda *_args: None)
    assert outcome.get("selected_index") == selected
    assert list(tmp_path.glob(".menu-choice-*")) == []


@pytest.mark.parametrize("transient", ["copy", "dialog", "drag", "inspection"])
def test_consumed_escape_disarms_secondary_without_layout_authority(
        tmp_path: Path, monkeypatch, transient: str) -> None:
    service, path, backend, controls, moment = _two_viewer_escape_fixture(tmp_path, monkeypatch)
    assert service._native_control_event(path, 457, "escape")["state"] == "escape_armed"
    session = read_json(path)
    field = {"dialog": "overlay_lock", "drag": "divider_drag",
             "inspection": "inspection_restore"}.get(transient)
    transient_state = ({"dialog": {"token": "primary", "pid": 123,
                                    "starttime": 10, "client_pid": 456},
                        "drag": {"started_monotonic": 99, "prior_weights": {}},
                        "inspection": {"mode": "stable"}}.get(transient))
    if field:
        session["native_layout"][field] = transient_state
        atomic_json(path, session)
    else:
        controls["copy_mode"] = True
    moment[0] = 100.1
    assert service._native_control_event(path, 457, "escape")["state"] == "escape_consumed"
    saved = read_json(path)
    assert "457" not in saved.get("native_escape_arms", {})
    if field:
        assert saved["native_layout"][field] == transient_state
        saved["native_layout"][field] = None
        atomic_json(path, saved)
    else:
        controls["copy_mode"] = False
    moment[0] = 100.2
    assert service._native_control_event(path, 457, "escape")["state"] == "escape_armed"
    assert backend.detached == []


@contextmanager
def _preview_terminal(program: str, arguments: list[str]):
    master, slave = pty.openpty()
    child = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 25, 100, 0, 0))
        child = subprocess.Popen([sys.executable, "-c", program, *arguments],
                                 stdin=slave, stdout=slave, stderr=slave,
                                 env=dict(os.environ, TERM="xterm-256color"),
                                 start_new_session=True)
        os.close(slave)
        slave = -1
        yield master, child
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        if slave >= 0:
            os.close(slave)
        os.close(master)


def _read_preview_marker(master: int, child: subprocess.Popen, marker: bytes) -> bytes:
    output = b""
    deadline = time.monotonic() + 2
    while marker not in output and time.monotonic() < deadline:
        if not select.select([master], [], [], .1)[0]:
            continue
        try:
            chunk = os.read(master, 8192)
        except OSError as exc:
            pytest.fail(f"preview child closed before {marker!r}: "
                        f"returncode={child.poll()}, output={output[-4096:]!r}, read_error={exc}")
        if not chunk:
            break
        output += chunk
    assert marker in output, (
        f"preview child did not render {marker!r}: "
        f"returncode={child.poll()}, output={output[-4096:]!r}")
    return output


def test_candidate_preview_renders_truecolor_semantic_foregrounds(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    from ls.core.terminal_ops.command_theme import theme_definition
    theme = theme_definition("midnight")

    class Backend:
        tmux_binary = "tmux"
        config_file = "/dev/null"

    native = NativeCanvasV3(Backend())
    monkeypatch.setattr(NativeCanvasV3, "_client", lambda *_args: {"name": "/dev/pts/1", "pid": 1})
    captured: list[list[str]] = []
    def capture(_self, _record, _client, argv, **_kwargs):
        captured.append(argv)
        return {"state": "dismissed", "verified": True}
    monkeypatch.setattr(NativeCanvasV3, "_run_overlay_client", capture)
    record = {"endpoint": "/tmp/fixture.sock", "status_pane_id": "%1",
              "session_record_path": "/tmp/fixture-preview/session.json",
              "runtime_settings": {"theme": theme}, "_preview_theme": theme}
    native.show_overlay(record, 1, "Preview", ["sample"], on_spawn=lambda *_args: None)
    argv = captured[0]
    program = argv[argv.index("-E") + 3]
    roles = json.loads(argv[-2])
    assert roles["success"] == theme["roles"]["success"]
    with _preview_terminal(program, argv[-2:]) as (master, child):
        output = _read_preview_marker(master, child, b"Enter applies this theme")
        os.write(master, b"q")
        child.wait(timeout=2)
        # The footer and truecolor swatches can arrive in separate PTY reads.
        # Capture the renderer's completed frame before checking its colors.
        while select.select([master], [], [], .05)[0]:
            try:
                output += os.read(master, 8192)
            except OSError:
                break
        assert child.returncode == 0, f"preview child exited: output={output[-4096:]!r}"
        success = theme["roles"]["success"]
        rgb = ";".join(str(int(success[index:index + 2], 16)) for index in (1, 3, 5))
        assert f"38;2;{rgb}".encode() in output
        assert b"Completed" in output


@pytest.mark.parametrize("key,accepted", [(b"\r", True), (b"\x1b", False)])
def test_candidate_preview_enter_applies_escape_cancels(tmp_path: Path, monkeypatch,
                                                        key: bytes, accepted: bool) -> None:
    from ls.core.terminal_ops.command_theme import theme_definition
    theme = theme_definition("midnight")

    class Backend:
        tmux_binary = "tmux"
        config_file = "/dev/null"

    native = NativeCanvasV3(Backend())
    monkeypatch.setattr(NativeCanvasV3, "_client", lambda *_args: {"name": "/dev/pts/1", "pid": 1})

    def run_popup(_self, _record, _client, argv, **_kwargs):
        program = argv[argv.index("-E") + 3]
        with _preview_terminal(program, argv[-2:]) as (master, child):
            output = _read_preview_marker(master, child, b"Enter applies this theme")
            os.write(master, key)
            child.wait(timeout=2)
            assert child.returncode == 0, f"preview child exited: output={output[-4096:]!r}"
            return {"state": "dismissed", "verified": True}

    monkeypatch.setattr(NativeCanvasV3, "_run_overlay_client", run_popup)
    record = {"endpoint": str(tmp_path / "fixture.sock"), "status_pane_id": "%1",
              "session_record_path": str(tmp_path / "session.json"),
              "runtime_settings": {"theme": theme}, "_preview_theme": theme}
    outcome = native.show_overlay(record, 1, "Preview", [], on_spawn=lambda *_args: None)
    assert outcome["accepted"] is accepted
    assert list(tmp_path.glob(".theme-preview-*")) == []


def test_preview_commit_requires_verified_cleanup(tmp_path: Path, monkeypatch) -> None:
    from ls.core.terminal_ops import command_live_configuration
    service = ShellCommandService(tmp_path / "state")
    path = tmp_path / "session.json"
    atomic_json(path, {"handle": "fixture"})
    applied: list[dict] = []
    def apply(_service, _handle, *, changes, client_pid):
        applied.append({"changes": changes, "client_pid": client_pid})
        return {"state": "applied", "ok": True}
    monkeypatch.setattr(command_live_configuration, "apply_presentation", apply)
    monkeypatch.setattr(service, "_native_open_text_overlay",
                        lambda *_args, **_kwargs: {"accepted": False, "cleanup_verified": True})
    service._native_control_event(path, 123, "theme_preview", operation_id="midnight")
    assert applied == []
    monkeypatch.setattr(service, "_native_open_text_overlay",
                        lambda *_args, **_kwargs: {"accepted": True, "cleanup_verified": False})
    with pytest.raises(ShellCommandError, match="verified cleanup"):
        service._native_control_event(path, 123, "theme_preview", operation_id="midnight")
    assert applied == []
    monkeypatch.setattr(service, "_native_open_text_overlay",
                        lambda *_args, **_kwargs: {"accepted": True, "cleanup_verified": True})
    result = service._native_control_event(path, 123, "theme_preview", operation_id="midnight")
    assert result["preview_accepted"] is True
    assert len(applied) == 1 and applied[0]["client_pid"] == 123


def test_inspection_timer_waits_for_copy_or_dialog_then_restores(tmp_path: Path, monkeypatch) -> None:
    runtime = {"layout": {"native_canvas": {"default_columns": 120,
                                              "default_rows": 24}},
               "inspection_duration_seconds": 15}
    state = _fresh_native_layout(runtime)
    saved = {"mode": "fixed", "page": 2, "manual_page": True,
             "selected_operation_id": "a", "page_anchor_operation_id": "a",
             "visible_operation_ids": ["a", "new"], "weights": {"a": 2, "new": 1},
             "expanded_operation_ids": ["a"]}
    state.update(mode="stable", inspection_restore=saved,
                 inspection_target_id="b", inspection_until_monotonic=10,
                 selected_operation_id="b", visible_operation_ids=["a", "b"],
                 desired_generation=1, applied_generation=1,
                 layout_state="applied", pending=False)
    path = tmp_path / "session.json"
    session = {"handle": "fixture", "layout_mode": "canvas-v3",
               "runtime_settings": runtime, "native_layout": state}
    atomic_json(path, session)
    service = ShellCommandService(tmp_path / "state")
    assert service._native_inspection_tick_locked(
        tmp_path, session, now=11, controls={"copy_mode": True}) is False
    assert read_json(path)["native_layout"]["inspection_until_monotonic"] == 26
    session = read_json(path)
    session["native_layout"]["overlay_lock"] = {
        "token": "t", "pid": 1, "starttime": 1, "client_pid": 1}
    atomic_json(path, session)
    assert service._native_inspection_tick_locked(
        tmp_path, session, now=27, controls={"copy_mode": False}) is False
    assert read_json(path)["native_layout"]["inspection_until_monotonic"] == 42
    session = read_json(path)
    session["native_layout"]["overlay_lock"] = None
    atomic_json(path, session)
    assert service._native_inspection_tick_locked(
        tmp_path, session, now=42, controls={"copy_mode": False}) is True
    restored = read_json(path)["native_layout"]
    assert (restored["mode"], restored["page"], restored["selected_operation_id"]) == (
        "fixed", 2, "a")
    assert restored["visible_operation_ids"] == ["a", "new"]
    assert restored["weights"] == {"a": 2, "new": 1}
    assert restored["inspection_restore"] is None
    assert restored["manual_change_pending"] is True


def test_owner_death_retirement_waits_for_drain_and_known_viewers(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "session.json"
    session = {"handle": "fixture", "layout_mode": "canvas-v3", "state": "ready",
               "native_layout": {"grouped_sections": True, "columns": 120},
               "owner": {"pid": 12}, "retirement_eligible_at": 60,
               "runtime_settings": {"auto_close_completed": True,
                                    "auto_close_delay_seconds": 30}}
    atomic_json(path, session)

    class Backend:
        calls = 0

        def retire_session(self, *_args, **_kwargs):
            self.calls += 1

        def _clients(self, _session):
            return [{"pid": 123, "activity_at": 100}]

    backend = Backend()
    service = ShellCommandService(tmp_path / "state", backend=backend, clock=lambda: 100)
    jobs = [{"state": "running", "output_drain": "pending"}]
    monkeypatch.setattr(service, "_strict_ops", lambda *_args: jobs)
    monkeypatch.setattr(service, "_terminal_ops_collected", lambda *_args: True)
    monkeypatch.setattr("ls.core.terminal_ops.command_service.owner_liveness", lambda *_args: "dead")
    monkeypatch.setattr(service, "_verify", lambda *_args: {
        "state": "ready", "session": {"attached": 1}})
    monkeypatch.setattr(service, "_native_inventory", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service, "_pins", lambda *_args, **_kwargs: ([], True))
    service._native_retirement_tick_locked(tmp_path, session)
    assert backend.calls == 0
    jobs[:] = [{"state": "completed", "output_drain": "available"}]
    service._native_retirement_tick_locked(tmp_path, session)
    assert backend.calls == 0
    assert read_json(path)["cleanup_state"] == "unknown"
    monkeypatch.setattr(service, "_pins", lambda *_args, **_kwargs: ([{"viewer_pid": 123}], False))
    session = read_json(path)
    service._native_retirement_tick_locked(tmp_path, session)
    assert backend.calls == 1
    assert read_json(path)["state"] == "retiring"
