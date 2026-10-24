"""Deterministic layout planning checks for the native tmux canvas."""

from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

import pytest

from ls.core.application_profiles.service import resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_cli import build_parser
from ls.core.terminal_ops.command_canvas import native_home_lines
from ls.core.terminal_ops.command_native_layout import (
    allocate_rows,
    default_weights_for_new_operation,
    plan_native_layout,
    reactivation_order,
)
from ls.core.terminal_ops.command_service import (
    ShellCommandService, _digest, _family_text_bound, _fresh_native_layout, _native_radio_row,
)
from ls.core.terminal_ops.command_state import (
    ShellCommandError, atomic_json, make_owner_binding, process_info,
    process_state, read_json, session_lock,
)


def _operation(operation_id: str, order: int, **extra):
    return {"operation_id": operation_id, "layout_order": order,
            "state": "running", "activity_monotonic": 100.0, **extra}


def test_empty_workspace_explains_controls_and_reserves_footer() -> None:
    lines = native_home_lines({"host": "host\x1b[2J\nforged"}, columns=80, rows=24)
    assert len(lines) == 23
    assert "Workspace ready" in "\n".join(lines)
    assert "F6" in "\n".join(lines)
    assert all(len(line) <= 80 and "\x1b" not in line and "\n" not in line for line in lines)
    assert native_home_lines({}, columns=80, rows=1) == ()


def test_large_native_layout_queue_uses_one_source_stream() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux unavailable")
    with tempfile.TemporaryDirectory(prefix="ls-native-queue-") as work:
        backend = TmuxCommandBackend(socket_root=Path(work) / "sockets")
        endpoint = backend.socket_path("queue")
        try:
            backend._checked(endpoint, ["new-session", "-d", "-s", "queue", "sleep", "30"],
                             mutation=True, allow_create=True)
            args = []
            for number in range(400):
                if args:
                    args.append(";")
                args.extend(["set-option", "-t", "$0", "@localsetup_shell_generation", str(number)])
            assert sum(len(part) + 1 for part in args) > 16384
            rejected = backend._run(endpoint, args, mutation=True)
            assert rejected.returncode != 0 and "command too long" in rejected.stderr
            backend._checked_layout_queue(str(endpoint), args)
            assert backend._checked(endpoint, ["show-options", "-v", "-t", "$0",
                                               "@localsetup_shell_generation"]).strip() == "399"
        finally:
            if backend._endpoint_state(endpoint) == "live":
                backend._checked(endpoint, ["kill-server"], mutation=True)


def test_large_layout_queue_rejects_non_generated_tokens_before_dispatch(monkeypatch) -> None:
    backend = TmuxCommandBackend()
    monkeypatch.setattr(backend, "_run", lambda *_a, **_kw: pytest.fail("invalid queue was sent"))
    with pytest.raises(ShellCommandError) as failure:
        backend._checked_layout_queue("unused", ["set-option", "@field", "x" * 9000 + "\nrun-shell false"])
    assert failure.value.code == "invalid_canvas_queue" and failure.value.effect == "none"


@pytest.mark.parametrize("condition", ["unqualified", "input", "unknown_workload"])
def test_native_repair_preserves_uncertainty_when_qualification_fails(tmp_path, monkeypatch, condition) -> None:
    service = ShellCommandService(tmp_path / "state")
    state = {"mode": "stable", "selected_operation_id": None, "weights": {},
             "columns": 120, "rows": 24, "desired_generation": 2,
             "applied_generation": 1, "page": 0, "layout_state": "unknown"}
    session = {"layout_mode": "canvas-v3", "native_layout": state}
    before = json.dumps(session, sort_keys=True)
    native = {"state": "unknown" if condition == "unqualified" else "ready",
              "controls": {"input_active": condition == "input"}}
    operations = [{"state": "running", "effect_certainty": "unknown"}] if condition == "unknown_workload" else []
    monkeypatch.setattr(service, "_verify", lambda _session: native)
    monkeypatch.setattr(service, "_strict_ops", lambda *_a: operations)
    monkeypatch.setattr(service, "_apply_native_layout_locked", lambda *_a, **_kw: pytest.fail("unsafe repair"))
    with pytest.raises(ShellCommandError):
        service._repair_native_layout_locked(tmp_path, session)
    assert json.dumps(session, sort_keys=True) == before and not (tmp_path / "session.json").exists()


def test_weighted_rows_keep_every_visible_pane_at_minimum() -> None:
    operations = [_operation("a", 0), _operation("b", 1), _operation("c", 2)]
    rows = allocate_rows(operations, {"a": 1, "b": 2, "c": 1}, rows=23)

    assert rows == {"a": 5, "b": 8, "c": 5}
    assert min(rows.values()) >= 3
    assert sum(rows.values()) + 1 + 4 == 23
    assert allocate_rows(operations, {}, rows=10) is None


def test_compact_stack_counts_dormant_rows_and_keeps_last_active_open() -> None:
    operations = [_operation("old", 0, activity_monotonic=1.0),
                  _operation("done", 1, state="completed"),
                  _operation("last", 2, activity_monotonic=1.0, native_min_rows=9)]
    state = {"mode": "follow", "compact_idle": True, "rows": 24, "columns": 120,
             "selected_operation_id": "last", "operation_order": ["old", "done", "last"]}
    plan = plan_native_layout(operations, state, now=100.0)
    assert plan["collapsed_sections"] == [{"operation_id": "old", "state": "idle"},
                                           {"operation_id": "done", "state": "completed"}]
    assert [(item["operation_id"], item["rows"]) for item in plan["assignments"]
            if item["visible"]] == [("last", 18)]
    assert plan["selected_operation_id"] == "last"
    hidden_active = plan_native_layout(
        operations, dict(state, mode="stable", visible_operation_ids=["last"]), now=1.0)
    assert hidden_active["collapsed_sections"][0]["state"] == "active"


def test_simultaneous_resumptions_move_each_job_below_previous_active_work() -> None:
    order = reactivation_order(["a", "b", "c"], [(10.0, "a"), (11.0, "b"), (10.0, "a")])
    assert order == ["c", "a", "b"]
    assert reactivation_order(order, [(11.0, "b")]) == order
    assert reactivation_order(order, []) == order


def test_compact_pages_obey_section_limit_and_manual_browsing() -> None:
    operations = [_operation(f"job{index}", index, state="completed", native_min_rows=9)
                  for index in range(9)]
    state = {"mode": "follow", "compact_idle": True, "max_sections": 3,
             "rows": 24, "columns": 120, "selected_operation_id": "job8"}
    plan = plan_native_layout(operations, state, now=100.0)
    assert (plan["page"], plan["page_count"]) == (2, 3)
    assert plan["page_operation_ids"] == ["job6", "job7", "job8"]
    manual = plan_native_layout(operations, dict(state, page=0, manual_page=True), now=100.0)
    assert manual["page_operation_ids"] == ["job0", "job1", "job2"]
    assert len(manual["collapsed_sections"]) == 3
    assert not any(item["visible"] for item in manual["assignments"])
    assert manual["selected_operation_id"] is None
    resized = plan_native_layout(operations, dict(state, rows=12), now=100.0)
    assert resized["page_operation_ids"] == ["job8"]
    assert next(item["rows"] for item in resized["assignments"] if item["visible"]) == 9


def test_stable_mode_uses_persisted_visibility_and_never_reorders_on_output() -> None:
    operations = [_operation("first", 0), _operation("second", 1),
                  _operation("background", 2, activity_monotonic=99.9)]
    state = {
        "mode": "stable", "rows": 24, "columns": 120,
        "operation_order": ["first", "second", "background"],
        "visible_operation_ids": ["first", "second"],
        "selected_operation_id": "first", "weights": {"first": 1, "second": 2},
    }

    plan = plan_native_layout(operations, state, now=100.0)

    assert plan["candidate_operation_ids"] == ["first", "second"]
    assert [item["operation_id"] for item in plan["assignments"]] == [
        "first", "second", "background"]
    assert [item["operation_id"] for item in plan["assignments"] if item["visible"]] == [
        "first", "second"]
    assert all(item["rows"] >= 3 for item in plan["assignments"])


def test_follow_keeps_selection_and_requested_input_visible_after_quiet_period() -> None:
    operations = [
        _operation("selected", 0, activity_monotonic=1.0),
        _operation("input", 1, activity_monotonic=1.0, input_required=True),
        {"operation_id": "completed", "layout_order": 2, "state": "completed",
         "activity_monotonic": 1.0},
    ]
    state = {"mode": "follow", "rows": 12, "columns": 120,
             "operation_order": ["selected", "input", "completed"],
             "selected_operation_id": "selected", "weights": {}}

    plan = plan_native_layout(operations, state, now=100.0)

    assert plan["candidate_operation_ids"] == ["selected", "input"]
    assert plan["selected_operation_id"] == "selected"
    assert all(item["rows"] >= 3 for item in plan["assignments"])


def test_fixed_pages_keep_stable_anchor_and_equal_share_newcomer_weights() -> None:
    operations = [_operation(f"op{index}", index) for index in range(1, 5)]
    state = {"mode": "fixed", "rows": 10, "columns": 120,
             "operation_order": [item["operation_id"] for item in operations],
             "visible_operation_ids": [item["operation_id"] for item in operations],
             "selected_operation_id": "op1", "page": 1,
             "page_anchor_operation_id": "op3", "weights": {}}

    plan = plan_native_layout(operations, state, now=100.0)

    assert plan["page_count"] == 2
    assert plan["page"] == 1
    assert [item["operation_id"] for item in plan["assignments"] if item["visible"]] == [
        "op3", "op4"]
    assert plan["selected_operation_id"] == "op3"
    assert all(item["rows"] >= 3 for item in plan["assignments"])
    assert default_weights_for_new_operation({"a": 1, "b": 2}, ["a", "b"], "c") == {
        "a": 2, "b": 4, "c": 3}


def test_ensure_layout_is_implicit_until_an_explicit_layout_is_given() -> None:
    assert build_parser().parse_args(["ensure"]).layout is None
    assert build_parser().parse_args(["ensure", "--layout", "canvas-v3"]).layout == "canvas-v3"


def test_mode_footer_uses_physical_width_and_clickable_sections() -> None:
    session = {"native_layout": {"mode": "follow", "columns": 500, "rows": 24,
                                  "desired_generation": 2, "applied_generation": 1,
                                  "page": 0, "pending": True, "weights": {}}}
    row, segments = _native_radio_row(session, columns=80)

    assert row.startswith("(*)follow ( )stable ( )fixed < > [panes] [layout] [logs] [?]")
    assert row.endswith(" !pending")
    assert len(row) < 80
    assert [entry[2] for entry in segments] == [
        "mode", "mode", "mode", "page_step", "page_step",
        "panes_menu", "layout_menu", "show_logs", "help",
    ]


def test_follow_loop_coalesces_burst_and_retains_event_until_floor(tmp_path, monkeypatch) -> None:
    runtime = {"layout": {"native_canvas": {
        "default_columns": 120, "default_rows": 24, "minimum_pane_rows": 3,
        "anchor_rows": 1, "quiet_seconds": 10,
        "resize_debounce_ms": 250, "automatic_view_min_seconds": 2,
    }}}
    state = _fresh_native_layout(runtime)
    state.update(selected_operation_id="a", operation_order=["a"],
                 desired_generation=1, pending=True, layout_state="desired",
                 last_activation_monotonic=100.0,
                 last_automatic_change_monotonic=100.0)
    path = tmp_path / "session.json"
    atomic_json(path, {"state": "ready", "layout_mode": "canvas-v3",
                       "session_record_path": str(path), "native_layout": state,
                       "runtime_settings": runtime, "handle": "controlled-loop"})
    service = ShellCommandService(tmp_path / "state", backend=object(),
                                  source_root=Path(__file__).resolve().parents[2])
    operations = [_operation("a", 0, activity_monotonic=100.0)]
    monkeypatch.setattr(service, "_native_canvas_operations", lambda *_args: operations)
    applied: list[tuple[float, str, tuple[str, ...]]] = []
    clock = {"now": 100.0, "b": False, "c": False}

    def fake_apply(_directory, session, *, force=False):
        layout = session["native_layout"]
        _items, plan = service._native_layout_plan(tmp_path, session, now=clock["now"])
        signature = _digest({key: value for key, value in plan.items() if key != "generation"})
        if (layout["applied_generation"] == layout["desired_generation"]
                and layout.get("applied_plan_signature") == signature):
            return {"state": "applied", "unchanged": True}
        layout.update(applied_generation=layout["desired_generation"],
                      applied_plan_signature=signature, pending=False,
                      layout_state="applied")
        session["native_layout"] = layout
        atomic_json(path, session)
        applied.append((clock["now"], layout["selected_operation_id"],
                        tuple(item["operation_id"] for item in plan["assignments"]
                              if item["visible"])))
        return {"state": "applied"}

    def advance(seconds):
        clock["now"] += seconds
        if not clock["b"] and clock["now"] >= 100.25:
            clock["b"] = True
            operations.append(_operation("b", 1, activity_monotonic=100.1))
            current = read_json(path)
            layout = current["native_layout"]
            layout["operation_order"].append("b")
            layout.update(desired_generation=layout["desired_generation"] + 1,
                          pending=True, layout_state="desired",
                          last_activation_monotonic=100.1)
            current.update(native_layout=layout, follow_event_operation_id="b",
                           follow_event_monotonic=100.1)
            atomic_json(path, current)
        if not clock["c"] and clock["now"] >= 100.75:
            clock["c"] = True
            operations.append(_operation("c", 2, activity_monotonic=100.75))
            current = read_json(path)
            layout = current["native_layout"]
            layout["operation_order"].append("c")
            layout.update(desired_generation=layout["desired_generation"] + 1,
                          pending=True, layout_state="desired",
                          last_activation_monotonic=100.75)
            current.update(native_layout=layout, follow_event_operation_id="c",
                           follow_event_monotonic=100.75)
            atomic_json(path, current)
        if clock["now"] >= 102.75:
            current = read_json(path)
            current["state"] = "closed"
            atomic_json(path, current)

    monkeypatch.setattr(service, "_apply_native_layout_locked", fake_apply)
    monkeypatch.setattr("ls.core.terminal_ops.command_service.time.monotonic",
                        lambda: clock["now"])
    monkeypatch.setattr("ls.core.terminal_ops.command_service.time.sleep", advance)

    assert service.run_native_control_loop(path) == 0
    assert len(applied) == 2
    assert applied[0][1:] == ("b", ("a", "b"))
    assert applied[0][0] - 100.1 >= .25
    assert applied[1][1:] == ("c", ("a", "b", "c"))
    assert applied[1][0] - applied[0][0] >= 2.0


@pytest.mark.parametrize('step_percent', [10, 25])
def test_native_menu_adjusts_derived_weights_without_changing_explicit_limit(tmp_path, monkeypatch, step_percent) -> None:
    state = _fresh_native_layout({"layout": {"native_canvas": {}}})
    state.update(mode="fixed", selected_operation_id="a",
                 operation_order=["a", "b", "c"],
                 visible_operation_ids=["a", "b", "c"],
                 weights={"a": 20_000, "b": 2, "c": 10_001})
    path = tmp_path / "session.json"
    atomic_json(path, {"state": "ready", "layout_mode": "canvas-v3",
                       "runtime_settings": {"layout": {"native_canvas": {"ratio_step_percent": step_percent}}},
                       "session_record_path": str(path), "handle": "menu-test",
                       "native_layout": state})
    service = ShellCommandService(tmp_path / "state", backend=object(),
                                  source_root=Path(__file__).resolve().parents[2])
    monkeypatch.setattr(service, "_verify", lambda *_args: {"state": "ready"})
    monkeypatch.setattr(service, "_native_primary_client", lambda *_args: True)
    monkeypatch.setattr(service, "_native_canvas_operations",
                        lambda *_args: [_operation(name, order)
                                       for order, name in enumerate(("a", "b", "c"))])
    monkeypatch.setattr(service, "_native_inventory", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service, "_apply_native_layout_locked",
                        lambda *_args, **_kwargs: {"state": "applied"})

    service.native_control_event(path, 123, "increase_ratio", operation_id="a")
    higher = read_json(path)["native_layout"]["weights"]
    expected = 20_000 + 20_000 * step_percent // 100
    assert higher == {"a": expected, "b": 2, "c": 10_001}
    service.native_control_event(path, 123, "decrease_ratio", operation_id="a")
    lower = read_json(path)["native_layout"]["weights"]
    assert lower == {"a": expected - round(expected * step_percent / 100), "b": 2, "c": 10_001}
    with pytest.raises(ShellCommandError) as rejected:
        service._native_ratio_values({"a": 20_000}, {"a"})
    assert rejected.value.code == "invalid_native_ratios"


def test_removed_native_operation_is_not_reintroduced_to_layout(tmp_path, monkeypatch) -> None:
    service = ShellCommandService(tmp_path / "state", source_root=Path(__file__).resolve().parents[2])
    operations = [
        {"operation_id": "old", "state": "completed", "cleanup_state": "removed"},
        {"operation_id": "live", "state": "running", "cleanup_state": "none"},
    ]
    monkeypatch.setattr(service, "_strict_ops", lambda *_args: operations)

    planned = service._native_canvas_operations(tmp_path, {"handle": "canvas"})

    assert [item["operation_id"] for item in planned] == ["live"]


def test_native_service_runs_and_closes_real_parked_pane(tmp_path) -> None:
    """Exercise the backend/service result shapes and exact pane lifecycle."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux unavailable")
    with tempfile.TemporaryDirectory(prefix="ls-native-v3-") as work_text:
        work = Path(work_text)
        backend = TmuxCommandBackend(config_file="/dev/null", socket_root=work / "sockets")
        if backend.capability().get("version") != "tmux 3.4":
            pytest.skip("native canvas contract requires tmux 3.4")
        root = Path(__file__).resolve().parents[2]
        service = ShellCommandService(work / "state", backend=backend, source_root=root)
        owner = make_owner_binding(owner_pid=os.getpid(), agent_id="native-v3-test")
        profile = resolve_profile(source_root=root, home=work / "home")

        def call(method, *args, **kwargs):
            deadline = time.monotonic() + 7
            while True:
                try:
                    return method(*args, **kwargs)
                except ShellCommandError as exc:
                    if exc.code != "busy" or exc.effect != "none" or time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)

        created = call(service.ensure, cwd=work, settings=profile["settings"],
                       settings_digest=profile["digest"], owner=owner,
                       profile_name=profile["name"], profile_base=profile["base_profile"])
        assert created["layout_mode"] == "canvas-v3"
        assert _family_text_bound(500, created["effective_settings"]["history_limit"], 512) < 20_000_000
        session_handle = created["handle"]
        reused = call(service.ensure, cwd=work, settings=profile["settings"],
                      settings_digest=profile["digest"], owner=owner,
                      profile_name=profile["name"], profile_base=profile["base_profile"])
        assert reused["handle"] == session_handle and reused["joined"] is True
        endpoint = service._load(session_handle)[1]["endpoint"]
        empty = call(service.layout, session_handle, mode="follow", owner=owner)
        assert empty["state"] == "applied"
        assert service._verify(service._load(session_handle)[1])["state"] == "ready"
        operation_handle = None
        second_handle = None
        closed = False
        try:
            deadline = time.monotonic() + 5
            while True:
                welcome = call(service.screen, session_handle, owner=owner)["lines"]
                if any("Workspace ready" in line for line in welcome):
                    break
                assert time.monotonic() < deadline, welcome
                time.sleep(.05)
            assert any("F6" in line for line in welcome)
            launched = call(service.run, session_handle, operation_id="smoke",
                            expanded_min_rows=4,
                            argv=[sys.executable, "-u", "-c",
                                  "import time; [print(f'LINE-{i:03d}') for i in range(100)]; "
                                  "print('NATIVE-V3'); time.sleep(20)"],
                            cwd=work, owner=owner)
            operation_handle = launched["operation_handle"]
            assert launched["state"] == "running"
            laid_out = call(service.layout, session_handle, mode="stable", owner=owner)
            assert laid_out["state"] == "applied"
            assert laid_out["desired_generation"] == laid_out["applied_generation"]
            observed = service._verify(service._load(session_handle)[1])
            assert observed["state"] == "ready" and observed["session"]["attached"] == 0
            status = next(pane for window in observed["windows"]
                          for pane in window["panes"] if pane["role"] == "status")
            assert status["height"] == 1 and status["y"] + status["height"] == 24
            assert status["in_mode"] is False
            deadline = time.monotonic() + 7
            while True:
                screen = call(service.screen, operation_handle, owner=owner)
                if any("NATIVE-V3" in line for line in screen["lines"]):
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("released native workload never appeared in its PTY")
                time.sleep(.05)
            task_before = next(pane for window in observed["windows"]
                               for pane in window["panes"] if pane["role"] == "operation")
            for columns, height in ((80, 24), (500, 24), (500, 512),
                                    (80, 6), (500, 24)):
                requested_width = call(service.viewport, session_handle, height=height,
                                       width=columns, owner=owner)
                assert requested_width["state"] == "pending"
                deadline = time.monotonic() + 7
                while True:
                    current = service._load(session_handle)[1]
                    layout = current["native_layout"]
                    if (layout["desired_generation"] == layout["applied_generation"]
                            and layout["columns"] == columns and layout["rows"] == height):
                        break
                    if time.monotonic() >= deadline:
                        pytest.fail(f"native viewport never applied {columns}x{height}")
                    time.sleep(.05)
                readback = service._verify(current)
                assert readback["state"] == "ready"
                assert readback["geometry"]["columns"] == columns
                assert readback["geometry"]["rows"] == height
                task = next(pane for window in readback["windows"]
                            for pane in window["panes"] if pane["role"] == "operation")
                parking = next(pane for window in readback["windows"]
                               for pane in window["panes"] if pane["role"] == "placeholder")
                assert (task["pane_id"], task["pane_pid"], task["pane_tty"], task["history_limit"]) == (
                    task_before["pane_id"], task_before["pane_pid"], task_before["pane_tty"],
                    task_before["history_limit"])
                assert (task["width"], task["height"]) == (parking["width"], parking["height"])
            native_history = backend._checked(
                endpoint, ["capture-pane", "-p", "-S", "-1000", "-t", task["pane_id"]])
            assert "LINE-000" in native_history and "NATIVE-V3" in native_history
            tail = call(service.logs, operation_handle, lines=200, max_bytes=8192, owner=owner)
            assert "LINE-000" in tail["output"] and "NATIVE-V3" in tail["output"]
            assert tail["gap"] is False
            backend._checked(endpoint, ["copy-mode", "-t", task["pane_id"]], mutation=True)
            frozen = service._verify(service._load(session_handle)[1])
            assert frozen["state"] == "ready" and frozen["controls"]["copy_mode"] is True
            pending = call(service.viewport, session_handle, height=24, width=100, owner=owner)
            assert pending["state"] == "pending"
            time.sleep(.35)
            during_copy = service._load(session_handle)[1]
            assert during_copy["native_layout"]["applied_generation"] < pending["desired_generation"]
            assert service._verify(during_copy)["geometry"]["columns"] == 500
            backend._checked(endpoint, ["send-keys", "-X", "-t", task["pane_id"], "cancel"],
                             mutation=True)
            deadline = time.monotonic() + 7
            while True:
                resumed = service._load(session_handle)[1]
                resumed_layout = resumed["native_layout"]
                if (resumed_layout["applied_generation"] == resumed_layout["desired_generation"]
                        and resumed_layout["columns"] == 100):
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("native layout did not resume after copy mode")
                time.sleep(.05)
            assert service._verify(resumed)["geometry"]["columns"] == 100
            backend._checked(endpoint, ["resize-pane", "-Z", "-t", task["pane_id"]], mutation=True)
            zoomed = service._verify(service._load(session_handle)[1])
            assert zoomed["state"] == "ready" and zoomed["controls"]["zoomed"] is True
            zoom_pending = call(service.viewport, session_handle, height=24, width=110, owner=owner)
            assert zoom_pending["state"] == "pending"
            time.sleep(.35)
            while_zoomed = service._load(session_handle)[1]
            assert while_zoomed["native_layout"]["applied_generation"] < zoom_pending["desired_generation"]
            backend._checked(endpoint, ["resize-pane", "-Z", "-t", task["pane_id"]], mutation=True)
            deadline = time.monotonic() + 7
            while True:
                unzoomed = service._load(session_handle)[1]
                unzoomed_layout = unzoomed["native_layout"]
                if (unzoomed_layout["applied_generation"] == unzoomed_layout["desired_generation"]
                        and unzoomed_layout["columns"] == 110):
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("native layout did not resume after zoom")
                time.sleep(.05)
            assert service._verify(unzoomed)["geometry"]["columns"] == 110
            second = call(service.run, session_handle, operation_id="second",
                          expanded_min_rows=4,
                          argv=[sys.executable, "-u", "-c", "import time; time.sleep(20)"],
                          cwd=work, owner=owner)
            second_handle = second["operation_handle"]
            assert second["state"] == "running"
            fixed = call(service.layout, session_handle, mode="fixed",
                         ratios={"smoke": 10_000, "second": 1}, owner=owner)
            assert fixed["state"] == "applied"
            third = call(service.run, session_handle, operation_id="third",
                         expanded_min_rows=4,
                         argv=[sys.executable, "-u", "-c", "import time; time.sleep(20)"],
                         cwd=work, owner=owner)
            assert third["state"] == "running"
            third_handle = third["operation_handle"]
            third_path = service._paths(service._load(session_handle)[0], "third")["record"]
            assert third_path.exists()
            assert service._load(session_handle)[1]["native_layout"]["weights"] == {
                "smoke": 20_000, "second": 2, "third": 10_001,
            }
            assert service._verify(service._load(session_handle)[1])["state"] == "ready"
            page_view = call(service.viewport, session_handle, height=8, width=120, owner=owner)
            assert page_view["state"] == "pending"
            deadline = time.monotonic() + 7
            while True:
                page_state = service._load(session_handle)[1]["native_layout"]
                if page_state["desired_generation"] == page_state["applied_generation"]:
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("native eight-row viewport did not settle")
                time.sleep(.05)
            first_page = call(service.layout, session_handle, mode="fixed", page=0, owner=owner)
            assert first_page["page_count"] == 3
            before_page = service._verify(service._load(session_handle)[1])
            alpha_before = next(pane for window in before_page["windows"]
                                for pane in window["panes"]
                                if pane.get("role") == "operation" and pane.get("operation_id") == "smoke")
            assert before_page["geometry"]["visible_operation_ids"] == ["smoke"]
            call(service.layout, session_handle, page=1, owner=owner)
            hidden_page = service._verify(service._load(session_handle)[1])
            assert hidden_page["state"] == "ready"
            assert hidden_page["geometry"]["visible_operation_ids"] == ["second"]
            alpha_hidden = next(pane for window in hidden_page["windows"]
                                for pane in window["panes"]
                                if pane.get("role") == "operation" and pane.get("operation_id") == "smoke")
            alpha_placeholder = next(pane for window in hidden_page["windows"]
                                     for pane in window["panes"]
                                     if pane.get("role") == "placeholder" and pane.get("operation_id") == "smoke")
            assert (alpha_hidden["pane_id"], alpha_hidden["pane_pid"], alpha_hidden["pane_tty"]) == (
                alpha_before["pane_id"], alpha_before["pane_pid"], alpha_before["pane_tty"])
            assert (alpha_hidden["width"], alpha_hidden["height"]) == (
                alpha_placeholder["width"], alpha_placeholder["height"])
            call(service.layout, session_handle, page=0, owner=owner)
            restored_page = service._verify(service._load(session_handle)[1])
            assert restored_page["state"] == "ready"
            assert restored_page["geometry"]["visible_operation_ids"] == ["smoke"]
            footer = next(pane for window in restored_page["windows"]
                          for pane in window["panes"] if pane["role"] == "status")
            assert footer["in_mode"] is False
            deadline = time.monotonic() + 7
            while read_json(third_path).get("process_scope") != "tracking":
                if time.monotonic() >= deadline:
                    pytest.fail("third native runner never began tracking")
                time.sleep(.05)
            requested = call(service.cancel, operation_handle, owner=owner)
            assert requested["cancel_state"] == "requested"
            assert call(service.cancel, second_handle, owner=owner)["cancel_state"] == "requested"
            assert call(service.cancel, third_handle, owner=owner)["cancel_state"] == "requested"
            for finished_handle in (operation_handle, second_handle, third_handle):
                deadline = time.monotonic() + 10
                while True:
                    finished = call(service.status, finished_handle, owner=owner)
                    if (finished["state"] == "completed"
                            and finished["output_drain"] in {"available", "unavailable"}):
                        break
                    if time.monotonic() >= deadline:
                        pytest.fail("cancelled native workload did not finish collection")
                    time.sleep(.05)
            # A short command can finish before the follow debounce releases.
            # Its parked pane must survive receipt collection and then become
            # the retained last-active view, rather than disappear unseen.
            call(service.layout, session_handle, mode="follow", owner=owner)
            directory, _session, _ = service._load(session_handle)
            with session_lock(directory, blocking=True):
                record = read_json(directory / "session.json")
                record["native_layout"]["last_automatic_change_monotonic"] = time.monotonic()
                record["native_layout"]["last_automatic_apply_monotonic"] = time.monotonic()
                atomic_json(directory / "session.json", record)
            fast = call(service.run, session_handle, operation_id="fast", expanded_min_rows=4,
                        argv=[sys.executable, "-u", "-c", "print('FAST RESULT')"], cwd=work, owner=owner)
            deadline = time.monotonic() + 5
            while True:
                finished = call(service.status, fast["operation_handle"], owner=owner)
                if finished["state"] == "completed" and finished["process_scope"] == "collected":
                    break
                assert time.monotonic() < deadline
                time.sleep(.02)
            assert finished["cleanup_state"] == "retained"
            while True:
                current = service._load(session_handle)[1]
                native = service._verify(current)
                if (current["native_layout"]["selected_operation_id"] == "fast"
                        and native["geometry"]["visible_operation_ids"] == ["fast"]):
                    break
                assert time.monotonic() < deadline
                time.sleep(.05)
            assert "FAST RESULT" in "\n".join(call(service.screen, fast["operation_handle"], owner=owner)["lines"])
            # An untagged pane makes the inventory uncertain. Close must leave
            # the entire private server intact until that exact extra window
            # is removed by the test that created it.
            intruder = backend._checked(
                endpoint, ["new-window", "-d", "-P", "-F", "#{window_id}",
                           "-t", observed["session"]["id"], "-n", "untagged",
                           "sleep", "86400"], mutation=True).strip()
            assert intruder.startswith("@")
            refused = call(service.close, session_handle, owner=owner)
            assert refused["state"] == "cleanup_pending" and refused["cleanup_state"] == "unknown"
            assert backend._endpoint_state(endpoint) == "live"
            backend._checked(endpoint, ["kill-window", "-t", intruder], mutation=True)
            deadline = time.monotonic() + 3
            while service._verify(service._load(session_handle)[1])["state"] != "ready":
                if time.monotonic() >= deadline:
                    pytest.fail("owned inventory did not recover after removing test window")
                time.sleep(.02)
            close_result = call(service.close, session_handle, owner=owner)
            assert close_result["state"] == "removed", {
                key: close_result.get(key) for key in
                ("state", "cleanup_state", "effect_certainty", "close_error", "observer_state")}
            assert close_result["cleanup_state"] == "removed"
            closed = True
        finally:
            if not closed and backend._endpoint_state(endpoint) == "live":
                # This unique socket was allocated by this test, and no
                # unrelated tmux server can be reached through it.
                backend._checked(endpoint, ["kill-server"], mutation=True)


def test_native_compact_rows_reactivate_below_active_and_repage_on_resize() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux unavailable")
    with tempfile.TemporaryDirectory(prefix="ls-native-stack-") as work_text:
        work = Path(work_text)
        backend = TmuxCommandBackend(config_file="/dev/null", socket_root=work / "sockets")
        root = Path(__file__).resolve().parents[2]
        service = ShellCommandService(work / "state", backend=backend, source_root=root)
        owner = make_owner_binding(owner_pid=os.getpid(), agent_id="compact-stack-test")
        profile = resolve_profile(source_root=root, home=work / "home")

        def call(method, *args, **kwargs):
            deadline = time.monotonic() + 5
            while True:
                try:
                    return method(*args, **kwargs)
                except ShellCommandError as exc:
                    if exc.code != "busy" or exc.effect != "none" or time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)

        created = call(service.ensure, cwd=work, settings=profile["settings"],
                       settings_digest=profile["digest"], owner=owner,
                       profile_name=profile["name"], profile_base=profile["base_profile"])
        handle = created["handle"]
        directory, session, _ = service._load(handle)
        endpoint = session["endpoint"]
        operations = []
        try:
            with session_lock(directory, blocking=True):
                session = read_json(directory / "session.json")
                session["runtime_settings"]["layout"]["native_canvas"]["quiet_seconds"] = 1
                session["runtime_digest"] = _digest(session["runtime_settings"])
                atomic_json(directory / "session.json", session)
            old = call(service.run, handle, operation_id="old", cwd=work, owner=owner,
                       argv=[sys.executable, "-u", "-c",
                             "from pathlib import Path; import time; print('OLD READY'); "
                             "exec(\"while not Path('wake').exists(): time.sleep(.05)\"); "
                             "print('OLD RESUMED'); time.sleep(30)"])
            operations.append(old["operation_handle"])
            active = call(service.run, handle, operation_id="active", cwd=work, owner=owner,
                          argv=[sys.executable, "-u", "-c",
                                "import time; exec(\"while True: print('ACTIVE'); time.sleep(.1)\")"])
            operations.append(active["operation_handle"])
            old_pane = old["native_canvas"]["pane_id"]

            def await_visible(ids):
                deadline = time.monotonic() + 8
                while True:
                    current = service._load(handle)[1]
                    observed = service._verify(current)
                    if (observed["geometry"]["visible_operation_ids"] == ids
                            and current["native_layout"]["layout_state"] == "applied"):
                        return current, observed
                    assert time.monotonic() < deadline, json.dumps({
                        "layout": current["native_layout"], "native_state": observed["state"],
                        "panes": [{key: pane.get(key) for key in
                                   ("pane_id", "role", "window_id", "height", "y", "dead", "generation")}
                                  for window in observed["windows"] for pane in window["panes"]]})
                    time.sleep(.05)

            current, observed = await_visible(["active"])
            original = next(pane for window in observed["windows"] for pane in window["panes"]
                            if pane["pane_id"] == old_pane)
            strip = next(pane for window in observed["windows"] for pane in window["panes"]
                         if pane["role"] == "summary")
            assert strip["height"] == 1 and strip["y"] == 1
            assert current["native_layout"]["collapsed_sections"] == [{"operation_id": "old", "state": "idle"}]
            deadline = time.monotonic() + 3
            while "old  idle" not in backend._checked(endpoint, ["capture-pane", "-p", "-t", strip["pane_id"]]):
                assert time.monotonic() < deadline
                time.sleep(.05)
            (work / "wake").touch()
            current, observed = await_visible(["active", "old"])
            resumed = next(pane for window in observed["windows"] for pane in window["panes"]
                           if pane["pane_id"] == old_pane)
            assert (resumed["pane_pid"], resumed["pane_tty"]) == (original["pane_pid"], original["pane_tty"])
            assert all(pane["height"] >= 9 for window in observed["windows"]
                       if window["window_id"] == current["status_window_id"]
                       for pane in window["panes"] if pane["role"] == "operation")
            call(service.viewport, handle, width=90, height=12, owner=owner)
            current, observed = await_visible(["old"])
            assert current["native_layout"]["page_count"] == 2
            assert observed["geometry"]["columns"] == 90
            call(service.layout, handle, page=0, owner=owner)
            assert service._load(handle)[1]["native_layout"]["mode"] == "stable"
            await_visible(["active"])
            call(service.viewport, handle, width=120, height=30, owner=owner)
            await_visible(["active", "old"])
        finally:
            for operation in operations:
                call(service.cancel, operation, owner=owner)
            deadline = time.monotonic() + 6
            while operations and time.monotonic() < deadline:
                operations = [op for op in operations
                              if call(service.status, op, owner=owner).get("process_scope") != "collected"]
                time.sleep(.05)
            assert not operations
            outcome = call(service.close, handle, owner=owner)
            while outcome.get("cleanup_state") == "waiting_native_exit" and time.monotonic() < deadline:
                time.sleep(.02)
                outcome = call(service.close, handle, owner=owner)
            diagnosis = service._verify(service._load(handle)[1]) if outcome["state"] != "removed" else {}
            if outcome["state"] != "removed":
                with session_lock(directory, blocking=True):
                    current = read_json(directory / "session.json")
                    current["state"] = "closed"
                    atomic_json(directory / "session.json", current)
                if backend._endpoint_state(endpoint) == "live":
                    backend._checked(endpoint, ["kill-server"], mutation=True)
            assert outcome["state"] == "removed", json.dumps({"outcome": outcome, "observed": diagnosis})


def test_uncertain_native_apply_reconciles_readback_without_replaying(tmp_path, monkeypatch) -> None:
    class Backend:
        apply_calls = 0

        def apply_native_layout(self, *_args, **_kwargs):
            self.apply_calls += 1
            raise AssertionError("an uncertain apply must never be replayed")

    backend = Backend()
    service = ShellCommandService(tmp_path / "state", backend=backend,
                                  source_root=Path(__file__).resolve().parents[2])
    plan = {"version": 1, "generation": 1, "mode": "follow",
            "viewport": {"columns": 120, "rows": 24}, "assignments": [],
            "selected_operation_id": None}
    state = {"mode": "follow", "selected_operation_id": None, "weights": {},
             "columns": 120, "rows": 24, "desired_generation": 1,
             "applied_generation": 0, "page": 0, "layout_state": "applying",
             "requested_plan": plan, "pending": True}
    session = {"layout_mode": "canvas-v3", "handle": "handle", "native_layout": state}
    observed = {"state": "ready", "applied_generation": 0, "geometry": {},
                "controls": {}, "inventory": {"windows": []}}
    monkeypatch.setattr(service, "_verify", lambda _session: observed)
    monkeypatch.setattr(service, "_native_canvas_operations", lambda *_args: [])
    monkeypatch.setattr(service, "_native_inventory", lambda *_args, **_kwargs: {})

    unresolved = service._apply_native_layout_locked(tmp_path, session)
    assert unresolved["state"] == "unknown"
    assert session["native_layout"]["layout_state"] == "unknown"
    assert backend.apply_calls == 0

    later = service._change_native_layout_locked(tmp_path, session, [], mode="stable")
    assert later["state"] == "unknown"
    assert session["native_layout"]["layout_state"] == "unknown"
    assert session["native_layout"]["desired_generation"] == 2
    assert backend.apply_calls == 0

    observed.update(
        applied_generation=1,
        geometry={"columns": 120, "rows": 24, "status_window_id": "@0",
                  "visible_operation_ids": []},
        controls={"panes": []},
        windows=[{"window_id": "@0", "metadata": {"generation": "1"},
                  "panes": [{"role": "status", "height": 23, "y": 1,
                             "generation": "1"}]}],
    )
    reconciled = service._apply_native_layout_locked(tmp_path, session)
    assert reconciled["state"] == "pending" and reconciled["reconciled"] is True
    assert session["native_layout"]["applied_generation"] == 1
    assert session["native_layout"]["desired_generation"] == 2
    assert session["native_layout"]["layout_state"] == "desired"
    assert backend.apply_calls == 0


def test_partial_native_swap_stops_new_layout_writes_on_changed_desire(tmp_path, monkeypatch) -> None:
    """A real first swap with lost acknowledgement must not invite another batch."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux unavailable")
    with tempfile.TemporaryDirectory(prefix="ls-native-partial-") as work_text:
        work = Path(work_text)
        backend = TmuxCommandBackend(config_file="/dev/null", socket_root=work / "sockets")
        if backend.capability().get("version") != "tmux 3.4":
            pytest.skip("native canvas contract requires tmux 3.4")
        root = Path(__file__).resolve().parents[2]
        service = ShellCommandService(work / "state", backend=backend, source_root=root)
        owner = make_owner_binding(owner_pid=os.getpid(), agent_id="partial-native-test")
        profile = resolve_profile(source_root=root, home=work / "home")

        def call(method, *args, **kwargs):
            deadline = time.monotonic() + 5
            while True:
                try:
                    return method(*args, **kwargs)
                except ShellCommandError as exc:
                    if exc.code != "busy" or exc.effect != "none" or time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)

        created = call(service.ensure, cwd=work, settings=profile["settings"],
                       settings_digest=profile["digest"], owner=owner,
                       profile_name=profile["name"], profile_base=profile["base_profile"])
        handle = created["handle"]
        endpoint = service._load(handle)[1]["endpoint"]
        original_checked = backend._checked
        try:
            launched = call(service.run, handle, operation_id="alpha",
                            argv=[sys.executable, "-u", "-c", "import time; time.sleep(20)"],
                            cwd=work, owner=owner)
            assert launched["state"] == "running"
            assert call(service.layout, handle, mode="stable", owner=owner)["state"] == "applied"
            before = service._verify(service._load(handle)[1])
            task = next(pane for window in before["windows"] for pane in window["panes"]
                        if pane.get("role") == "operation")
            assert before["geometry"]["visible_operation_ids"] == ["alpha"]
            writes_after_fault: list[tuple[str, ...]] = []
            faulted = False

            def partial_checked(endpoint_arg, args, *, mutation=False, allow_create=False):
                nonlocal faulted
                if (not faulted and mutation and args and args[0] == "swap-pane"
                        and ";" in args):
                    faulted = True
                    original_checked(endpoint_arg, args[:args.index(";")], mutation=True)
                    raise ShellCommandError("qualification_lost_layout_ack",
                                            "The first real pane swap lost its acknowledgement.",
                                            effect="unknown")
                if faulted and mutation:
                    writes_after_fault.append(tuple(args))
                return original_checked(endpoint_arg, args, mutation=mutation,
                                        allow_create=allow_create)

            monkeypatch.setattr(backend, "_checked", partial_checked)
            with pytest.raises(ShellCommandError) as lost:
                call(service.layout, handle, mode="fixed", ratios={"alpha": 1}, owner=owner)
            assert lost.value.effect == "unknown" and faulted
            pending = service._load(handle)[1]["native_layout"]
            assert pending["layout_state"] == "unknown"
            in_flight = pending["requested_plan"]["generation"]
            partial = service._verify(service._load(handle)[1])
            assert partial["state"] == "ready"
            parked = next(pane for window in partial["windows"] for pane in window["panes"]
                          if pane.get("pane_id") == task["pane_id"])
            assert parked["pane_pid"] == task["pane_pid"] and parked["pane_tty"] == task["pane_tty"]
            assert parked["window_id"] != service._load(handle)[1]["status_window_id"]
            assert partial["applied_generation"] < in_flight
            assert call(service.viewport, handle, height=25, width=100, owner=owner)["state"] == "pending"
            call(service.layout, handle, mode="follow", owner=owner)
            with pytest.raises(ShellCommandError) as blocked:
                call(service.run, handle, operation_id="later",
                     argv=[sys.executable, "-u", "-c", "print('must not start')"],
                     cwd=work, owner=owner)
            assert blocked.value.effect == "unknown"
            assert not service._paths(service._load(handle)[0], "later")["record"].exists()
            time.sleep(.4)  # The independent control loop must also reconcile only.
            latest = service._load(handle)[1]
            assert latest["native_layout"]["layout_state"] == "unknown"
            assert latest["native_layout"]["desired_generation"] > in_flight
            assert service._verify(latest)["applied_generation"] < in_flight
            assert writes_after_fault == []
            # Explicit repair builds a fresh generation from the complete
            # tagged inventory; it does not dispatch the rejected workload.
            monkeypatch.setattr(backend, "_checked", original_checked)
            repaired = call(service.doctor, handle, owner=owner, repair=True)
            assert any(item["action"] == "repair_native_layout" and item["result"] == "applied"
                       for item in repaired["attempts"]), repaired
            final = service._verify(service._load(handle)[1])
            restored = next(pane for window in final["windows"] for pane in window["panes"]
                            if pane.get("pane_id") == task["pane_id"])
            assert restored["pane_pid"] == task["pane_pid"] and restored["pane_tty"] == task["pane_tty"]
            assert final["applied_generation"] > in_flight
            assert not service._paths(service._load(handle)[0], "later")["record"].exists()
        finally:
            monkeypatch.setattr(backend, "_checked", original_checked)
            directory, session, _ = service._load(handle)
            with session_lock(directory, blocking=True):
                session = read_json(directory / "session.json")
                session["state"] = "closed"
                atomic_json(directory / "session.json", session)
            if backend._endpoint_state(endpoint) == "live":
                # This fixture allocated the unique socket and cannot close an
                # intentionally unresolved native mutation through the service.
                backend._checked(endpoint, ["kill-server"], mutation=True)
            # Do not remove the state tree while its exact control process can
            # still enter session_lock and recreate directories during exit.
            pid = session["status_pane_pid"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = process_info(pid)
                if (current is None or current["starttime"] != session["status_pane_starttime"]
                        or process_state(pid) == "Z"):
                    break
                time.sleep(.02)
            else:
                pytest.fail("fixture control process did not exit")
