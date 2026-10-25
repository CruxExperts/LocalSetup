"""Telemetry remains readable, run-bound, and separate from execution authority."""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ls.core import test_telemetry as telemetry
from ls.core.lockfile import save_json
from ls.core.test_pressure import PressureSample


def test_display_survives_fd_capture_and_restores_stream(tmp_path):
    from _pytest._io import TerminalWriter
    from ls.core.test_output import TestOutput
    with (tmp_path / "console").open("w+") as console, (tmp_path / "capture").open("w+") as capture:
        writer = TerminalWriter(console)
        reporter = SimpleNamespace(_tw=writer, _screen_width=120,
            _write_progress_information_filling_space=lambda: None,
            _write_progress_information_if_past_edge=lambda: None)
        display = TestOutput(reporter, tmp_path, interval=3600)
        saved = os.dup(console.fileno())
        try:
            display.start()
            os.dup2(capture.fileno(), console.fileno())
            writer.line("telemetry stays visible")
            writer.flush()
        finally:
            os.dup2(saved, console.fileno())
            os.close(saved)
            display.close()
        console.seek(0)
        capture.seek(0)
        assert console.read() == "telemetry stays visible\n"
        assert capture.read() == ""
        assert writer._file is console
        assert display.output_stream.closed
        assert not display.thread.is_alive()
        assert reporter._screen_width == 120


def test_compact_terminal_styles_preserve_values_and_clip_without_broken_escapes(tmp_path, monkeypatch):
    from ls.core.branding import terminal
    from ls.core.terminal_ops.command_theme import palette
    monkeypatch.setattr(terminal, "selected_presentation", lambda: (palette({}), True))
    monkeypatch.setattr(telemetry, "runtime_root", lambda: tmp_path)
    snapshot = telemetry.Telemetry("localsetup-tests-fixture.scope", tmp_path, {}, 4).data
    plain = telemetry.render(snapshot)
    colored = telemetry.render(snapshot, color=True)
    strip = lambda text: re.sub(r"\x1b\[[0-9;]*m", "", text)
    assert plain.startswith("[LS] v" + telemetry.framework_version() + " tests starting | wrk ?/4 | tgt ? | done ?/?")
    assert "OS thr ?/?" in plain
    assert strip(colored) == plain
    assert [len(row) for row in plain.splitlines()] == [80, 80]
    for width in (1, 4, 17, 60):
        clipped = telemetry.render(snapshot, color=True, width=width)
        assert strip(clipped).splitlines() == [row[:width] for row in plain.splitlines()]
        assert "\x1b" not in strip(clipped)


def test_display_tracks_selected_session_theme_and_disabled_colors(tmp_path, monkeypatch):
    from ls.core.branding import terminal
    from ls.core.terminal_ops.command_theme import theme_definition
    path = tmp_path / "session.json"
    monkeypatch.setenv("LOCALSETUP_TERMINAL_SESSION_RECORD", str(path))
    def select(name, enabled=True):
        save_json(path, {"session_record_path": str(path), "runtime_settings": {
            "theme": theme_definition(name), "colors_enabled": enabled}})
    select("midnight")
    first = terminal.render_parts([("[LS]", "brand")], color=True)
    select("graphite")
    second = terminal.render_parts([("[LS]", "brand")], color=True)
    assert first != second
    assert "\x1b[" in first and "\x1b[" in second
    select("graphite", False)
    assert terminal.render_parts([("[LS]", "brand")], color=True) == "[LS]"
    path.write_text("invalid")
    assert terminal.render_parts([("[LS]", "brand")], color=True) == "[LS]"


def test_outside_session_theme_read_does_not_create_preferences(tmp_path, monkeypatch):
    from ls.core.branding import terminal
    monkeypatch.delenv("LOCALSETUP_TERMINAL_SESSION_RECORD", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    colors, enabled = terminal.selected_presentation()
    assert enabled and colors["accent"].startswith("#")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_kind", ["deep", "fifo_replacement"])
def test_bad_theme_context_cannot_block_or_crash_output(tmp_path, monkeypatch, bad_kind):
    from ls.core.branding import terminal
    path = tmp_path / "session.json"
    path.write_text("[" * 1200 + "0" + "]" * 1200)
    path.chmod(0o600)
    monkeypatch.setenv("LOCALSETUP_TERMINAL_SESSION_RECORD", str(path))
    if bad_kind == "fifo_replacement":
        original = os.open
        def replace_before_open(filename, flags, *args, **kwargs):
            if filename == path:
                assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
                path.unlink()
                os.mkfifo(path, 0o600)
            return original(filename, flags, *args, **kwargs)
        monkeypatch.setattr(terminal.os, "open", replace_before_open)
    assert terminal.render_parts([("[LS]", "brand")], color=True) == "[LS]"


def test_snapshot_lifecycle_metrics_and_cleanup_are_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(telemetry, "framework_version", lambda: "9.8.7")
    value = telemetry.Telemetry("localsetup-tests-fixture.scope", tmp_path, {}, 4)
    (tmp_path / "pids.current").write_text("9")
    (tmp_path / "pids.max").write_text("1024")
    value.sample(tmp_path, PressureSample(80, 40, 100, 90, 120, 10), "steady")
    result = telemetry.status(tmp_path)
    assert result["status"] == "live"
    assert result["snapshot"]["framework_version"] == "9.8.7"
    assert result["snapshot"]["resources"]["tasks"] == 9
    assert result["snapshot"]["resources"]["inactive_file_bytes"] == 40
    assert result["snapshot"]["resources"]["headroom_bytes"] == 50
    value.finish(0)
    assert telemetry.status(tmp_path)["status"] == "live"
    value.cleaned()
    final = telemetry.status(tmp_path)
    assert final["status"] == "finished"
    assert final["snapshot"]["cleanup"] == "complete"
    assert telemetry.validator().is_valid(final["snapshot"])
    assert telemetry.status(tmp_path / "other")["status"] == "different_project"


def test_old_run_wrong_controller_and_invalid_progress_are_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "runtime_root", lambda: tmp_path)
    value = telemetry.Telemetry("localsetup-tests-fixture.scope", tmp_path, {}, 4)
    value.data["controller"] = {"pid": 111, "starttime": 222}
    envelope = {"schema_version": 1, "run_id": value.data["run_id"],
                "pid": 111, "starttime": 222, "updated_at": time.time(),
                "progress": {"completed": 2}}
    for bad in ({"run_id": "localsetup-tests-old.scope"}, {"pid": 112},
                {"progress": {"completed": -1}}):
        save_json(telemetry.progress_path(), envelope | bad)
        value.merge_progress()
        assert value.data["progress"] is None
    save_json(telemetry.progress_path(), envelope)
    value.merge_progress()
    assert value.data["progress"]["completed"] == 2
    assert "progress_error" not in value.data


def test_atomic_snapshots_and_stale_owner_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "runtime_root", lambda: tmp_path)
    value = telemetry.Telemetry("localsetup-tests-fixture.scope", tmp_path, {}, 1)
    def update():
        for _ in range(25):
            value.write()
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(update)
        while not work.done():
            record = telemetry.read_record(telemetry.status_path())
            assert record["run_id"] == value.data["run_id"]
            assert record["sequence"] >= 1
        work.result()
    monkeypatch.setattr(telemetry, "process_info", lambda _: None)
    assert telemetry.status(tmp_path)["status"] == "stale"


def test_worker_or_nested_pid_cannot_publish_parent_progress(monkeypatch):
    monkeypatch.setenv("LOCALSETUP_TEST_CONTROLLER_PID", "1")
    assert telemetry.publish_progress({"completed": 999}) is False


@pytest.mark.parametrize("unsafe_mode", [False, True])
def test_invalid_progress_cleanup_preserves_workload_result(tmp_path, monkeypatch, unsafe_mode):
    monkeypatch.setattr(telemetry, "runtime_root", lambda: tmp_path)
    value = telemetry.Telemetry("localsetup-tests-fixture.scope", tmp_path, {}, 1)
    save_json(telemetry.progress_path(), {"run_id": value.data["run_id"]})
    if unsafe_mode:
        telemetry.progress_path().chmod(0o644)
    else:
        telemetry.progress_path().write_text("{broken")
    value.finish(0)
    value.cleaned()
    snapshot = telemetry.status(tmp_path)["snapshot"]
    assert (snapshot["state"], snapshot["exit_code"], snapshot["cleanup"]) == ("completed", 0, "complete")
    assert snapshot["progress_cleanup"] == "retained_invalid"
    assert telemetry.progress_path().exists()


@pytest.mark.parametrize("crash_report", [False, True])
def test_fixed_concurrency_crash_or_interrupt_has_no_phantom_running_case(monkeypatch, crash_report):
    from ls.core import test_progress
    records = []
    monkeypatch.setattr(test_progress, "controller_binding", lambda: {"pid": 123})
    monkeypatch.setattr(test_progress, "publish_progress", records.append)
    reporter = test_progress.ProgressReporter(SimpleNamespace(option=SimpleNamespace(numprocesses=1)))
    reporter.total = 3
    reporter.pytest_runtest_logstart("finished", None)
    reporter.pytest_runtest_logreport(SimpleNamespace(when="teardown", nodeid="finished"))
    reporter.pytest_runtest_logstart("abandoned", None)
    if crash_report:
        reporter.pytest_runtest_logreport(SimpleNamespace(when="???", nodeid="abandoned", failed=True))
    reporter.pytest_sessionfinish(None, 1)
    final = records[-1]
    assert (final["completed"], final["unresolved"], final["running"], final["pending"]) == (1, 1, 0, 1)
    assert final["workers"]["active"] == 0
