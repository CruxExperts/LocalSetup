"""Renderer-only replacement in an isolated native canvas fixture."""

from __future__ import annotations

import os
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

import pytest

import ls.core.terminal_ops.command_ui_refresh as refresh_module
from ls.core.application_profiles.service import resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_service import ShellCommandService
from ls.core.terminal_ops.command_state import ShellCommandError, atomic_json, make_owner_binding, read_json
from ls.core.terminal_ops.command_ui_refresh import refresh_native_renderers


def test_refresh_replaces_only_renderers_in_attached_native_canvas() -> None:
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="ls-ui-refresh-") as temporary:
        work = Path(temporary)
        backend = TmuxCommandBackend(config_file="/dev/null", socket_root=work / "sockets")
        if backend.capability().get("version") != "tmux 3.4":
            pytest.skip("native renderer qualification requires tmux 3.4")
        service = ShellCommandService(work / "state", backend=backend, source_root=root)
        owner = make_owner_binding(owner_pid=os.getpid(), agent_id="ui-refresh-test")
        profile = resolve_profile(source_root=root, home=work / "home")
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"TMUX", "TMUX_PANE"} and not key.endswith("_SUDO_KEY")}
        environment["TERM"] = "xterm-256color"
        outer = str(work / "viewer.sock")
        handle = None
        operation_handle = None

        def tmux(*args: str) -> str:
            return subprocess.run(["tmux", "-f", "/dev/null", "-S", outer, *args],
                                  env=environment, capture_output=True, text=True,
                                  timeout=5, check=True).stdout

        def eventually(predicate, *, timeout: float = 8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(.05)
            raise AssertionError("UI refresh fixture did not converge")

        def call(method, *args, **kwargs):
            deadline = time.monotonic() + 5
            while True:
                try:
                    return method(*args, **kwargs)
                except ShellCommandError as exc:
                    if exc.code not in {"busy", "ui_refresh_busy"} or time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)

        try:
            created = call(service.ensure, cwd=work, settings=profile["settings"],
                           settings_digest=profile["digest"], owner=owner,
                           profile_name=profile["name"], profile_base=profile["base_profile"],
                           layout_mode="canvas-v3")
            handle = created["handle"]
            command = [sys.executable, str(root / "ls/tools/localsetup.py"), "--source-root", str(root),
                       "shell", "--state-root", str(work / "state"), "attach", "--session", created["session_alias"]]
            viewer = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-x", "120", "-y", "40",
                          "-s", "refresh", shlex.join(command)).strip()
            eventually(lambda: "Workspace ready" in tmux("capture-pane", "-p", "-t", viewer))
            eventually(lambda: (
                (layout := service._load(handle)[1]["native_layout"])["layout_state"] == "applied"
                and not layout["pending"]
            ))
            launched = call(service.run, handle, operation_id="silent-workload", expanded_min_rows=4,
                            argv=[sys.executable, "-u", "-c",
                                  "import time; print('REFRESH-WORKLOAD'); time.sleep(30)"],
                            cwd=work, owner=owner)
            operation_handle = launched["operation_handle"]
            eventually(lambda: (
                (layout := service._load(handle)[1]["native_layout"])["layout_state"] == "applied"
                and not layout["pending"]
                and any("REFRESH-WORKLOAD" in line for line in
                        call(service.screen, operation_handle, owner=owner)["lines"])
            ))
            before = service._load(handle)[1]
            observed = service._verify(before)
            assert observed["state"] == "ready"
            assert observed["session"]["attached"] == 1
            task_before = next(pane for window in observed["windows"] for pane in window["panes"]
                               if pane["role"] == "operation")
            process_before = service._strict_ops(service._load(handle)[0], before)[0]
            assert process_before["runner_pid"] == task_before["pane_pid"]
            content_before = backend._checked(before["endpoint"],
                                              ["capture-pane", "-p", "-t", task_before["pane_id"]])
            assert "REFRESH-WORKLOAD" in content_before
            first = call(refresh_native_renderers, service, handle, owner=owner)
            assert first["state"] == "refreshed"
            after = service._load(handle)[1]
            assert after["handle"] == handle
            assert after["status_pane_id"] == before["status_pane_id"]
            assert after["summary_pane_id"] == before["summary_pane_id"]
            assert after["status_pane_pid"] != before["status_pane_pid"]
            assert after["summary_pane_pid"] != before["summary_pane_pid"]
            assert "ui_refresh" not in after
            assert service._verify(after)["state"] == "ready"
            task_after = next(pane for window in service._verify(after)["windows"] for pane in window["panes"]
                              if pane["role"] == "operation")
            process_after = service._strict_ops(service._load(handle)[0], after)[0]
            assert (task_after["pane_id"], task_after["pane_pid"], task_after["pane_tty"]) == (
                task_before["pane_id"], task_before["pane_pid"], task_before["pane_tty"])
            assert (process_after["runner_pid"], process_after["runner_starttime"]) == (
                process_before["runner_pid"], process_before["runner_starttime"])
            content_after = backend._checked(after["endpoint"],
                                             ["capture-pane", "-p", "-t", task_after["pane_id"]])
            assert "REFRESH-WORKLOAD" in content_after
            assert "REFRESH-WORKLOAD" in tmux("capture-pane", "-p", "-t", viewer)
            def cli_refresh():
                command = [sys.executable, str(root / "ls/tools/localsetup.py"), "--source-root", str(root),
                           "shell", "--state-root", str(work / "state"), "refresh-ui", "--handle", handle,
                           "--owner-pid", str(owner["pid"]), "--owner-starttime", str(owner["starttime"]),
                           "--agent-id", owner["agent_id"]]
                result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
                payload = json.loads(result.stdout)
                if not payload.get("ok"):
                    raise ShellCommandError(payload["code"], payload["message"], effect=payload["effect"])
                assert result.returncode == 0
                return payload
            second = call(cli_refresh)
            assert second["state"] == "refreshed"
            assert service._load(handle)[1]["ui_generation"] != after["ui_generation"]
        finally:
            try:
                tmux("kill-server")
            except subprocess.SubprocessError:
                pass
            if operation_handle is not None:
                call(service.cancel, operation_handle, owner=owner)
                eventually(lambda: call(service.status, operation_handle, owner=owner).get("process_scope") == "collected")
            if handle is not None:
                closed = call(service.close, handle, owner=owner)
                if closed.get("cleanup_state") != "removed":
                    eventually(lambda: call(service.close, handle, owner=owner).get("cleanup_state") == "removed")


@pytest.mark.parametrize("fault", ["lost_ack", "readback", "partial", "final_receipt"])
def test_refresh_faults_have_truthful_unknown_receipts(tmp_path: Path, monkeypatch, fault: str) -> None:
    directory = tmp_path / "session"
    directory.mkdir(mode=0o700)
    record = directory / "session.json"
    session = {
        "handle": "fixture-handle", "machine_generation": "fixture-machine",
        "session_record_path": str(record), "layout_mode": "canvas-v3", "state": "ready",
        "endpoint": str(tmp_path / "fixture.sock"),
        "native_layout": {"layout_state": "applied", "pending": False, "overlay_lock": None},
        "status_pane_id": "%1", "status_pane_pid": 101, "status_pane_starttime": 201,
        "summary_pane_id": "%2", "summary_pane_pid": 102, "summary_pane_starttime": 202,
    }
    atomic_json(record, session)
    dispatches: list[str] = []

    class Backend:
        def _checked(self, _endpoint, args, *, mutation=False):
            assert mutation and args[:4] == ["respawn-pane", "-k", "-t", args[3]]
            dispatches.append(args[3])
            if fault == "lost_ack" and len(dispatches) == 1:
                raise ShellCommandError("native_command_failed", "Lost native acknowledgement.", effect="unknown")
            return ""

        def apply_native_control_theme(self, _session):
            return None

    class Service:
        backend = Backend()

        def _load(self, _handle):
            return directory, read_json(record), None

        def _authorize(self, _session, _owner):
            return {}

        def _strict_ops(self, _directory, _session):
            return []

        def _native_inventory(self, _session, _observed, _operations, *, allow_active=False):
            assert allow_active
            return {}

        def _pins(self, _directory, *, remove_dead=False):
            return [], False

        def _verify(self, current):
            panes = [{"pane_id": current[f"{role}_pane_id"], "pane_pid": current[f"{role}_pane_pid"],
                      "role": role, "dead": False, "input_off": True}
                     for role in ("status", "summary")]
            return {"state": "ready", "session": {"attached": 1},
                    "controls": {"freeze_layout": False}, "windows": [{"panes": panes}]}

    def readback(_service, current, role, _token, _previous):
        if fault == "readback" or (fault == "partial" and role == "summary"):
            raise OSError("synthetic readback transport failure")
        return current[f"{role}_pane_pid"] + 100, current[f"{role}_pane_starttime"] + 100

    monkeypatch.setattr(refresh_module, "_readback", readback)
    monkeypatch.setattr(refresh_module, "_process_matches", lambda *_args, **_kwargs: True)
    if fault == "final_receipt":
        writes = [0]

        def fail_final(path, payload):
            writes[0] += 1
            if writes[0] == 6:
                raise OSError("synthetic final receipt failure")
            atomic_json(path, payload)

        monkeypatch.setattr(refresh_module, "atomic_json", fail_final)

    service = Service()
    if fault == "lost_ack":
        result = refresh_native_renderers(service, "fixture-handle", owner={})
        assert result["state"] == "refreshed"
        assert dispatches == ["%1", "%2"]
        assert "ui_refresh" not in read_json(record)
        return

    with pytest.raises(ShellCommandError) as raised:
        refresh_native_renderers(service, "fixture-handle", owner={})
    assert (raised.value.code, raised.value.effect) == ("ui_refresh_unknown", "unknown")
    assert dispatches == (["%1", "%2"] if fault in {"partial", "final_receipt"} else ["%1"])
    assert read_json(record)["ui_refresh"]["state"] in {"dispatching", "verified"}
    with pytest.raises(ShellCommandError) as repeat:
        refresh_native_renderers(service, "fixture-handle", owner={})
    assert (repeat.value.code, repeat.value.effect) == ("ui_refresh_unknown", "unknown")
    assert len(dispatches) == (2 if fault in {"partial", "final_receipt"} else 1)
