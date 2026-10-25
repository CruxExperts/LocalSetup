"""Versioned, runner-owned activity canvas qualification."""

from __future__ import annotations

import os
import fcntl
import hashlib
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import subprocess
import struct
import sys
import tempfile
import termios
import threading
import time

import pytest

from ls.core.application_profiles.service import load_tmux_runtime_defaults, resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_canvas import Section, _mouse_step, _sections, page_step, pages, render, safe_text, selected_page
from ls.core.terminal_ops.command_runner import _canvas_capture, _canvas_input
from ls.core.terminal_ops.command_service import ShellCommandService, _family_history_ceiling
from ls.core.terminal_ops.command_state import ShellCommandError, atomic_json, make_owner_binding, process_info, read_json


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _eventually(predicate, timeout: float = 7) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.05)
    return bool(predicate())


def _busy_retry(action, timeout: float = 7):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except ShellCommandError as exc:
            if exc.code != "busy" or time.monotonic() >= deadline:
                raise
            time.sleep(.05)


def test_canvas_pack_order_minima_and_literal_text() -> None:
    runtime = load_tmux_runtime_defaults(source_root=SOURCE_ROOT, name="agent-shell")["settings"]
    with pytest.raises(ShellCommandError, match="native text budget"):
        _family_history_ceiling({**runtime, "layout": {**runtime["layout"], "canvas_columns": 200}})
    now = time.monotonic()
    sections = [Section("a", "A", "running", 1, now - 6, 12, False, "one\x1b[31m\r\ntwo"),
                Section("b", "B", "running", 2, now - 6, 10),
                Section("c", "C", "running", 3, now, 10)]
    packed = pages(sections, 23, now=now)
    assert [[item.operation_id for item in page.sections] for page in packed] == [["a", "b", "c"]]
    assert packed[0].heights == (1, 1, 21)
    assert len(render(packed[0], columns=80, rows=24, page_index=0, page_count=1)) == 24
    assert safe_text(sections[0].output) == "one?[31m\ntwo"
    assert _mouse_step(2, 23, rows=24, page_index=0, page_count=2) == -1
    assert _mouse_step(28, 23, rows=24, page_index=0, page_count=2) == 1
    assert _mouse_step(2, 22, rows=24, page_index=0, page_count=2) == 0
    assert len(render(packed[0], columns=80, rows=1, page_index=0, page_count=1)) == 1
    assert "Page left" in render(packed[0], columns=80, rows=1, page_index=0, page_count=1)[0]
    active = [Section("a", "A", "running", 4, now, 12), sections[2]]
    weighted = pages(active, 39, now=now)[0]
    assert weighted.heights == (18, 21)
    assert selected_page((weighted,), active, {"canvas_follow_active": True}) == 0
    completed = [Section("old", "Old", "completed", 1, now - 100, 10),
                 Section("new", "New", "completed", 2, now - 100, 12)]
    assert pages(completed, 23, now=now + 1000)[0].heights == (1, 22)
    assert pages(completed[:1], 23, now=now + 1000)[0].heights == (23,)
    with pytest.raises(ValueError):
        pages([Section("bad", "Bad", "running", 1, now, 1)], 23, now=now)


def test_canvas_input_partial_backpressure_never_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700)
    intent = inputs / "one.json"
    atomic_json(intent, {"state": "intent", "eof": False})
    intent.with_suffix(".input").write_bytes(b"abcdef")
    sent = bytearray()
    calls = 0
    def partial_write(_fd: int, block: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BlockingIOError()
        amount = min(2, len(block))
        sent.extend(block[:amount])
        return amount
    monkeypatch.setattr("ls.core.terminal_ops.command_runner.os.write", partial_write)
    pending = _canvas_input(19, inputs, None)
    assert pending is not None and read_json(intent)["state"] == "dispatching"
    assert not intent.with_suffix(".input").exists()
    pending = _canvas_input(19, inputs, pending)
    assert pending is not None and bytes(sent) == b"ab"
    while pending is not None:
        pending = _canvas_input(19, inputs, pending)
    assert bytes(sent) == b"abcdef" and read_json(intent)["state"] == "sent"
    assert _canvas_input(19, inputs, None) is None and bytes(sent) == b"abcdef"


def test_canvas_capture_bounded_cursor_gap(tmp_path: Path) -> None:
    log, cursor = tmp_path / "op.log", tmp_path / "op.cursor.json"
    _canvas_capture(log, cursor, b"A" * 4096, limit=4096, activity_monotonic=1)
    for index in range(1, 51):
        _canvas_capture(log, cursor, bytes([65 + index % 26]) * 1024,
                        limit=4096, activity_monotonic=1 + index)
    meta = read_json(cursor)
    assert meta["end_offset"] == 4096 + 50 * 1024 and meta["available"] is True
    assert meta["base_offset"] == meta["end_offset"] - 4096
    assert 0 < meta["base_offset"]  # A historical cursor reports a gap.
    assert log.stat().st_size == 4096
    assert log.read_bytes() == b"".join(bytes([65 + index % 26]) * 1024 for index in range(47, 51))


def test_native_canvas_primary_page_and_resize(tmp_path: Path) -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux unavailable")
    socket_root = Path(tempfile.mkdtemp(prefix="ls-canvas-sock-"))
    backend = TmuxCommandBackend(config_file="/dev/null", socket_root=socket_root)
    if backend.capability().get("version") != "tmux 3.4":
        pytest.skip("canvas native contract requires tmux 3.4")
    profile = resolve_profile(source_root=SOURCE_ROOT, home=tmp_path / "home")
    runtime = load_tmux_runtime_defaults(source_root=SOURCE_ROOT, name="agent-shell")["settings"]
    assert runtime["layout"]["quiet_seconds"] == 5
    runtime = {**runtime, "layout": {**runtime["layout"], "quiet_seconds": 1}}
    state_root = tmp_path / "state"
    service = ShellCommandService(state_root, backend=backend, source_root=SOURCE_ROOT)
    owner = make_owner_binding(owner_pid=os.getpid(), agent_id="canvas-native")
    created = service.ensure(cwd=tmp_path, settings=profile["settings"],
                             settings_digest=profile["digest"], owner=owner,
                             profile_name=profile["name"], profile_base=profile["base_profile"],
                             layout_mode="canvas-v2", runtime_settings=runtime)
    handle = str(created["handle"])
    launched = []
    viewer_process = None
    viewer_master = None
    viewer_bytes = bytearray()
    other_viewers: list[tuple[subprocess.Popen[bytes], int, threading.Event]] = []
    drain_stop = threading.Event()
    def current_screen() -> list[str]:
        current = service._load(handle)[1]
        return backend._checked(current["endpoint"], ["capture-pane", "-p", "-t",
                                                       str(current["status_pane_id"])]).splitlines()
    def drain_viewer() -> None:
        while not drain_stop.is_set() and viewer_master is not None:
            if select.select([viewer_master], [], [], .05)[0]:
                try:
                    viewer_bytes.extend(os.read(viewer_master, 65536))
                    if len(viewer_bytes) > 200_000:
                        del viewer_bytes[:-100_000]
                except OSError:
                    return
    try:
        for name in "abc":
            stop = tmp_path / f"stop-{name}"
            resume = tmp_path / f"resume-{name}"
            finish = tmp_path / f"finish-{name}"
            quiet = tmp_path / f"quiet-{name}"
            script = (f"while [ ! -e {shlex.quote(str(stop))} ]; do printf '{name}-tick\\n'; sleep .3; done; "
                      f"printf '{name}-paused\\n'; "
                      f"while [ ! -e {shlex.quote(str(resume))} ]; do sleep .05; done; "
                      f"printf '{name}-resumed\\n'; "
                      f"while [ ! -e {shlex.quote(str(finish))} ]; do "
                      f"if [ ! -e {shlex.quote(str(quiet))} ]; then printf '{name}-tick\\n'; fi; "
                      "sleep .3; done")
            launched.append(_busy_retry(lambda: service.run(
                handle, name, script=script, cwd=tmp_path, owner=owner,
                expanded_min_rows=12 if name == "a" else None)))
        assert _eventually(lambda: all(service._load_operation(item["operation_handle"])[2].get("child_pid")
                                       for item in launched))
        identities = {name: (record["runner_pid"], record["runner_starttime"],
                             record["child_pid"], record["child_starttime"], record["child_pgrp"])
                      for name, record in ((item["operation_id"], service._load_operation(item["operation_handle"])[2])
                                           for item in launched)}
        session = service._load(handle)[1]
        assert backend.inspect_session(session)["windows"][0]["pane_count"] == 1
        assert session["settings"]["status"] is False
        assert backend.inspect_session(session)["applied_settings"]["status"] is False
        backend.verify_observer_keys(session)
        assert _eventually(lambda: "Page left" in backend.capture_screen(service._load(handle)[1], None, lines=24))

        viewer_master, viewer_slave = pty.openpty()
        fcntl.ioctl(viewer_slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        code = ("from pathlib import Path; "
                "from ls.core.terminal_ops.command_service import ShellCommandService; "
                f"ShellCommandService({str(state_root)!r}, source_root=Path({str(SOURCE_ROOT)!r}))"
                f".attach({handle!r}, human_observer=True)")
        def attach_other() -> tuple[subprocess.Popen[bytes], int]:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            process = subprocess.Popen([sys.executable, "-c", code],
                                       stdin=slave, stdout=slave, stderr=slave,
                                       start_new_session=True,
                                       env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT), "TERM": "xterm-256color"})
            os.close(slave)
            stop = threading.Event()
            def drain() -> None:
                while not stop.is_set():
                    if select.select([master], [], [], .05)[0]:
                        try:
                            os.read(master, 65536)
                        except OSError:
                            return
            threading.Thread(target=drain, daemon=True).start()
            other_viewers.append((process, master, stop))
            return process, master
        viewer_process = subprocess.Popen([sys.executable, "-c", code],
                                          stdin=viewer_slave, stdout=viewer_slave,
                                          stderr=viewer_slave, start_new_session=True,
                                          env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT), "TERM": "xterm-256color"})
        os.close(viewer_slave)
        thread = threading.Thread(target=drain_viewer, daemon=True)
        thread.start()
        assert _eventually(lambda: len(backend._clients(service._load(handle)[1])) == 1), (
            viewer_process.poll(), bytes(viewer_bytes[-8192:]))
        assert _eventually(lambda: "read-only" not in backend._clients(service._load(handle)[1])[0]["flags"])
        assert _eventually(lambda: backend.inspect_session(service._load(handle)[1])["windows"][0]["height"] == 24)
        assert _eventually(lambda: b"Page left" in viewer_bytes)
        client_pid = backend._clients(service._load(handle)[1])[0]["pid"]
        assert _eventually(lambda: any(pin.get("viewer_pid") == client_pid
                                       for pin in service._pins(service._load(handle)[0])[0]))
        assert service._load(handle)[1].get("canvas_follow_active", True) is True
        os.write(viewer_master, b"\x1b[D")
        assert _eventually(lambda: service._load(handle)[1].get("canvas_follow_active") is False), (
            service._load(handle)[1].get("canvas_page"), page_step(service._load(handle)[0] / "session.json", client_pid, -1))
        anchored = service._load(handle)[1]["canvas_anchor"]
        assert anchored in {"a", "b"}
        assert _eventually(lambda: "[ 1/2 ]" in current_screen()[-1])
        footer = current_screen()[-1]
        click_x = footer.index("Page right") + 3
        os.write(viewer_master, f"\x1b[<0;{click_x + 1};24M\x1b[<0;{click_x + 1};24m".encode())
        assert _eventually(lambda: service._load(handle)[1].get("canvas_follow_active") is True)
        os.write(viewer_master, b"\x1b[D")
        assert _eventually(lambda: service._load(handle)[1].get("canvas_follow_active") is False)
        anchored = service._load(handle)[1]["canvas_anchor"]
        time.sleep(.6)
        assert service._load(handle)[1]["canvas_anchor"] == anchored
        assert service._load(handle)[1]["canvas_follow_active"] is False
        os.write(viewer_master, b"OBSERVER_INPUT_MUST_NOT_REACH_WORKLOAD\n")
        time.sleep(.3)
        assert all("OBSERVER_INPUT_MUST_NOT_REACH_WORKLOAD" not in
                   service.logs(item["operation_handle"], owner=owner)["output"]
                   for item in launched)

        for name in "abc":
            (tmp_path / f"stop-{name}").touch()
        def layout_snapshot():
            current = service._load(handle)[1]
            return pages(_sections(service._load(handle)[0], current),
                         current["primary_viewport_height"] - 1,
                         quiet_seconds=current["runtime_settings"]["layout"]["quiet_seconds"])
        # Output scheduling can legitimately reactivate any producer after the
        # one-second quiet threshold. Establish the phase instead of assuming
        # that launch order still makes c the newest activation under CI load.
        assert _eventually(lambda: all(
            f"{item['operation_id']}-paused" in service.logs(item['operation_handle'], owner=owner)['output']
            for item in launched))
        assert _eventually(lambda: all(time.monotonic() - item.last_output >= 1
                           for item in _sections(service._load(handle)[0], service._load(handle)[1])))
        paused = _sections(service._load(handle)[0], service._load(handle)[1])
        collapsed_ids = [item.operation_id for item in sorted(paused,
            key=lambda item: (item.activated, item.operation_id)) if item.operation_id in {'a', 'b'}]
        expected_ids = [*collapsed_ids, 'c']
        (tmp_path / "resume-c").touch()
        assert _eventually(lambda: "c-resumed" in service.logs(launched[2]['operation_handle'], owner=owner)['output'])
        assert _eventually(lambda: max(_sections(service._load(handle)[0], service._load(handle)[1]),
                           key=lambda item: item.activated).operation_id == 'c')
        assert _eventually(lambda: [item.operation_id for item in layout_snapshot()[0].sections] == expected_ids
                           and layout_snapshot()[0].heights == (1, 1, 21), timeout=5), layout_snapshot()
        assert _eventually(lambda: all(
            f" · {name} · " in row for name, row in zip(
                expected_ids, current_screen()[:3])))
        screen_rows = current_screen()
        assert len(screen_rows) == 24
        for name, row in zip(expected_ids, screen_rows[:3]):
            assert f" · {name} · " in row and row.endswith("[Running]")
        assert "Page left" in screen_rows[23]
        assert all(record["state"] == "running" for record in
                   (service._load_operation(item["operation_handle"])[2] for item in launched))
        (tmp_path / "resume-a").touch()
        assert _eventually(lambda: "a-resumed" in service.logs(launched[0]["operation_handle"], owner=owner)["output"])
        assert _eventually(lambda: [item.operation_id for item in layout_snapshot()[0].sections] == ["b", "c", "a"]
                           and layout_snapshot()[0].heights == (1, 10, 12))
        for width, height in ((80, 24), (120, 40), (100, 18), (80, 24)):
            fcntl.ioctl(viewer_master, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
            os.kill(viewer_process.pid, signal.SIGWINCH)
            assert _eventually(lambda: (
                (lambda window: (window["width"], window["height"]) == (width, height))(
                    backend.inspect_session(service._load(handle)[1])["windows"][0])), timeout=5)
            window = backend.inspect_session(service._load(handle)[1])["windows"][0]
            assert (window["width"], window["height"], window["pane_count"]) == (width, height, 1)
            assert _busy_retry(lambda: service.status(handle, owner=owner))["native_state"] == "ready"
            for name, expected in identities.items():
                record = service._load_operation(next(item["operation_handle"] for item in launched
                                                      if item["operation_id"] == name))[2]
                actual = (record["runner_pid"], record["runner_starttime"],
                          record["child_pid"], record["child_starttime"], record["child_pgrp"])
                assert actual == expected
                assert process_info(record["child_pid"])["starttime"] == record["child_starttime"]
            if (width, height) == (120, 40):
                assert layout_snapshot()[0].heights == (1, 17, 21)
            if width == 80 and height == 24:
                assert _eventually(lambda: any(
                    tmp_path.name[-8:] in row and "[Running]" in row for row in current_screen()))
            if height == 18:
                assert len(layout_snapshot()) == 2
                assert "Page left" in current_screen()[-1]
        fcntl.ioctl(viewer_master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 70, 0, 0))
        os.kill(viewer_process.pid, signal.SIGWINCH)
        assert _eventually(lambda: service._load(handle)[1].get("canvas_width_warning") is True)
        assert _eventually(lambda: "Resize to at least 80 columns" in
                           current_screen()[0])
        narrow_rows = current_screen()
        assert len(narrow_rows) == 24 and "Page left" in narrow_rows[-1]
        assert not any("[Running]" in row for row in narrow_rows)
        assert _eventually(lambda: b"Resize to at least 80 columns" in viewer_bytes)
        fcntl.ioctl(viewer_master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.kill(viewer_process.pid, signal.SIGWINCH)
        assert _eventually(lambda: service._load(handle)[1].get("canvas_width_warning") is False)
        (tmp_path / "quiet-c").touch()
        (tmp_path / "quiet-a").touch()
        assert _eventually(lambda: [item.operation_id for item in layout_snapshot()[0].sections] == ["b", "c", "a"]
                           and layout_snapshot()[0].heights == (1, 1, 21), timeout=5)
        cancelled = _busy_retry(lambda: service.cancel(launched[1]["operation_handle"], owner=owner))
        assert cancelled["cancel_state"] == "requested"
        stopped = service.wait(launched[1]["operation_handle"], timeout=8, owner=owner)
        assert stopped["state"] == "completed" and stopped["output_drain"] == "available"

        interactive = _busy_retry(lambda: service.run(
            handle, "interactive", script="printf 'prompt\\n'; read -r answer; printf 'answer=%s\\n' \"$answer\"",
            cwd=tmp_path, owner=owner))
        assert _eventually(lambda: bool(service._load_operation(interactive["operation_handle"])[2].get("child_pid")))
        delivered = service.input(interactive["operation_handle"], "reply", b"ok\n", owner=owner)
        assert delivered["state"] == "sent"
        assert service.input(interactive["operation_handle"], "reply", b"ok\n", owner=owner)["repeated"] is True
        completed_input = service.wait(interactive["operation_handle"], timeout=8, owner=owner)
        assert completed_input["state"] == "completed" and "answer=ok" in completed_input["output"]

        digest_script = ("python3 -c 'import hashlib,sys; data=sys.stdin.buffer.read(); "
                         "print(len(data), hashlib.sha256(data).hexdigest())'")
        for label, payload in (("empty-stdin", b""),
                               ("binary-stdin", b"\x00\x04\x1b\r\nlast-byte"),
                               ("large-stdin", b"x" * (2 * 1024 * 1024))):
            sent = _busy_retry(lambda: service.run(handle, label, script=digest_script,
                                                    cwd=tmp_path, stdin=payload, owner=owner))
            result = service.wait(sent["operation_handle"], timeout=12, owner=owner)
            assert result["state"] == "completed" and result["exit_code"] == 0
            assert f"{len(payload)} {hashlib.sha256(payload).hexdigest()}" in result["output"]

        geometry = _busy_retry(lambda: service.run(
            handle, "geometry", script="trap 'printf WINCH; stty size' WINCH; stty size; while :; do sleep .2; done",
            cwd=tmp_path, owner=owner))
        assert _eventually(lambda: "24 80" in service.logs(geometry["operation_handle"], owner=owner)["output"])
        fcntl.ioctl(viewer_master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.kill(viewer_process.pid, signal.SIGWINCH)
        assert _eventually(lambda: "WINCH40 120" in
                           service.logs(geometry["operation_handle"], owner=owner)["output"], timeout=5)
        fcntl.ioctl(viewer_master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.kill(viewer_process.pid, signal.SIGWINCH)
        assert _eventually(lambda: "WINCH24 80" in
                           service.logs(geometry["operation_handle"], owner=owner)["output"], timeout=5)
        assert _busy_retry(lambda: service.cancel(geometry["operation_handle"], owner=owner))["cancel_state"] == "requested"
        assert service.wait(geometry["operation_handle"], timeout=8, owner=owner)["state"] == "completed"

        storage = _busy_retry(lambda: service.run(
            handle, "storage-failure", script="while :; do printf 'storage-tick\\n'; sleep .05; done",
            cwd=tmp_path, owner=owner))
        storage_path = service._load_operation(storage["operation_handle"])[0]
        paths = service._paths(storage_path, "storage-failure")
        assert _eventually(paths["log"].exists)
        retained_log = paths["log"].with_suffix(".saved")
        paths["log"].rename(retained_log)
        paths["log"].mkdir(mode=0o700)
        try:
            assert _eventually(lambda: paths["cursor"].exists() and
                               read_json(paths["cursor"]).get("available") is False)
            running = service._load_operation(storage["operation_handle"])[2]
            assert running["state"] == "running"
            assert service.logs(storage["operation_handle"], owner=owner)["storage"] is False
            assert process_info(running["child_pid"])["starttime"] == running["child_starttime"]
            assert _busy_retry(lambda: service.cancel(storage["operation_handle"], owner=owner))["cancel_state"] == "requested"
            failed_capture = service.wait(storage["operation_handle"], timeout=8, owner=owner)
            assert failed_capture["state"] == "completed" and failed_capture["output_drain"] == "unavailable"
            assert service.logs(storage["operation_handle"], owner=owner)["storage"] is False
            assert process_info(running["child_pid"]) is None
        finally:
            paths["log"].rmdir()
            retained_log.rename(paths["log"])

        secondary_process, secondary_master = attach_other()
        assert _eventually(lambda: len(backend._clients(service._load(handle)[1])) == 2)
        secondary_pid = next(client["pid"] for client in backend._clients(service._load(handle)[1])
                             if client["pid"] != client_pid)
        current = service._load(handle)[1]
        secondary_pin = next(pin for pin in service._pins(service._load(handle)[0])[0]
                             if pin.get("pid") == secondary_process.pid)
        assert secondary_pin.get("primary_viewport") is not True
        assert page_step(service._load(handle)[0] / "session.json", secondary_pid, -1) is False
        anchor_before = current.get("canvas_anchor")
        os.write(secondary_master, b"\x1b[D")
        time.sleep(.3)
        assert service._load(handle)[1].get("canvas_anchor") == anchor_before
        viewport_before = backend.inspect_session(current)["windows"][0]
        fcntl.ioctl(secondary_master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.kill(secondary_process.pid, signal.SIGWINCH)
        time.sleep(.5)
        viewport_after = backend.inspect_session(service._load(handle)[1])["windows"][0]
        assert (viewport_after["width"], viewport_after["height"]) == (
            viewport_before["width"], viewport_before["height"])
        primary_name = next(client["name"] for client in backend._clients(service._load(handle)[1])
                            if client["pid"] == client_pid)
        backend._checked(session["endpoint"], ["detach-client", "-t", primary_name], mutation=True)
        viewer_process.wait(timeout=3)
        assert _eventually(lambda: len(backend._clients(service._load(handle)[1])) == 1)
        assert page_step(service._load(handle)[0] / "session.json", secondary_pid, -1) is False
        secondary_name = backend._clients(service._load(handle)[1])[0]["name"]
        backend._checked(session["endpoint"], ["detach-client", "-t", secondary_name], mutation=True)
        secondary_process.wait(timeout=3)
        assert _eventually(lambda: not service._pins(service._load(handle)[0])[0])
        replacement_process, replacement_master = attach_other()
        assert _eventually(lambda: len(backend._clients(service._load(handle)[1])) == 1)
        replacement_pid = backend._clients(service._load(handle)[1])[0]["pid"]
        assert _eventually(lambda: any(pin.get("viewer_pid") == replacement_pid and
                                       pin.get("primary_viewport") is True
                                       for pin in service._pins(service._load(handle)[0])[0]))
        assert page_step(service._load(handle)[0] / "session.json", replacement_pid, -1) is True
        backend.verify_observer_keys(service._load(handle)[1])
    finally:
        for name in "abc":
            (tmp_path / f"stop-{name}").touch()
            (tmp_path / f"resume-{name}").touch()
            (tmp_path / f"finish-{name}").touch()
        if viewer_process is not None:
            try:
                clients = backend._clients(service._load(handle)[1])
                for client in clients:
                    backend._checked(service._load(handle)[1]["endpoint"],
                                     ["detach-client", "-t", client["name"]], mutation=True)
            except Exception:
                pass
            try:
                viewer_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                viewer_process.terminate()
                viewer_process.wait(timeout=3)
        drain_stop.set()
        if viewer_master is not None:
            os.close(viewer_master)
        for process, master, stop in other_viewers:
            stop.set()
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=2)
            os.close(master)
        for item in launched:
            try:
                service.wait(item["operation_handle"], timeout=8, owner=owner)
            except Exception:
                pass
        try:
            service.close(handle, owner=owner)
        except Exception:
            pass
        for socket in socket_root.glob("*"):
            subprocess.run([backend.tmux_binary or "tmux", "-S", str(socket), "kill-server"],
                           capture_output=True)
        shutil.rmtree(socket_root)
