"""Actual two-client native dialog isolation, delivery, resize and cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from ls.core.application_profiles.service import resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_service import ShellCommandService
from ls.core.terminal_ops.command_state import ShellCommandError, make_owner_binding
from ls.core.terminal_ops.dialog_model import Field, Form


@pytest.mark.parametrize("mode", ["submit", "disconnect"])
def test_native_dialog_is_private_to_primary_and_preserves_input_on_resize(mode: str) -> None:
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="ls-dialog-native-") as temporary:
        work = Path(temporary)
        backend = TmuxCommandBackend(config_file="/dev/null", socket_root=work / "sockets")
        capability = backend.capability()
        if capability.get("state") != "available" or capability.get("version") != "tmux 3.4":
            pytest.skip("native dialog qualification requires tmux 3.4")
        service = ShellCommandService(work / "state", backend=backend, source_root=root)
        owner = make_owner_binding(owner_pid=os.getpid(), agent_id="native-dialog-test")
        profile = resolve_profile(source_root=root, home=work / "home")
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"TMUX", "TMUX_PANE"} and not key.endswith("_SUDO_KEY")}
        environment["TERM"] = "xterm-256color"
        outer = str(work / "viewer.sock")
        handle = None
        thread = None
        live = [True]
        result = []
        delivered = []

        def tmux(*args: str) -> str:
            return subprocess.run(["tmux", "-f", "/dev/null", "-S", outer, *args], env=environment,
                                  text=True, capture_output=True, timeout=5, check=True).stdout

        def eventually(condition, timeout=8):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                value = condition()
                if value:
                    return value
                time.sleep(0.05)
            raise AssertionError("Native dialog condition did not converge.")

        def call(method, *args, **kwargs):
            end = time.monotonic() + 5
            while True:
                try:
                    return method(*args, **kwargs)
                except ShellCommandError as error:
                    if error.code != "busy" or error.effect != "none" or time.monotonic() >= end:
                        raise
                    time.sleep(0.05)

        try:
            created = call(service.ensure, cwd=work, settings=profile["settings"],
                           settings_digest=profile["digest"], owner=owner, profile_name=profile["name"],
                           profile_base=profile["base_profile"], layout_mode="canvas-v3")
            handle = created["handle"]
            command = [sys.executable, str(root / "ls/tools/localsetup.py"), "--source-root", str(root),
                       "shell", "--state-root", str(work / "state"), "attach", "--session", created["session_alias"]]
            first = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-x", "120", "-y", "40",
                         "-s", "dialog", shlex.join(command)).strip()
            tmux("set-option", "-g", "status", "off")
            eventually(lambda: "Workspace ready" in tmux("capture-pane", "-p", "-t", first))

            def primary_qualified():
                directory, session, _ = service._load(handle)
                pins, unknown = service._pins(directory, remove_dead=False)
                primary = [pin for pin in pins if pin.get("primary_viewport") is True]
                return (not unknown and len(primary) == 1
                        and service._native_primary_client(directory, session, primary[0].get("viewer_pid")))

            eventually(primary_qualified)
            second = tmux("new-window", "-P", "-F", "#{pane_id}", "-t", "dialog", shlex.join(command)).strip()
            eventually(lambda: "Workspace ready" in tmux("capture-pane", "-p", "-t", second))

            def consume(response):
                assert response.values == {"secret": b"NativeDialogCanary8", "notes": b"resize preserved"}
                delivered.append(True)
                return True

            form = Form("Private native form probe", (Field("secret", "Password", kind="password", required=True),
                                                       Field("notes", "Notes", kind="text", required=True)))
            for deadline, valid in ((float("nan"), True), (time.monotonic() + 5, False)):
                with pytest.raises(ShellCommandError, match="could not be admitted"):
                    service.dialog(handle, operation_id="invalid-admission", form=form, deadline=deadline,
                                   validate_current=lambda: valid, deliver=consume, owner=owner)
                assert service._load(handle)[1]["native_layout"]["overlay_lock"] is None

            def request():
                try:
                    result.append(service.dialog(handle, operation_id="native-form-test", form=form,
                                                 deadline=time.monotonic() + 15, validate_current=lambda: live[0],
                                                 deliver=consume, owner=owner))
                except Exception as error:
                    result.append(error)

            thread = threading.Thread(target=request)
            thread.start()
            try:
                eventually(lambda: "Private native form probe" in tmux("capture-pane", "-p", "-t", first))
            except AssertionError:
                directory, observed, _ = service._load(handle)
                pins, unknown = service._pins(directory, remove_dead=False)
                raise AssertionError({
                    "result": repr(result), "first": tmux("capture-pane", "-p", "-t", first),
                    "second": tmux("capture-pane", "-p", "-t", second), "pins_unknown": unknown,
                    "pins": [{key: pin.get(key) for key in ("pid", "starttime", "viewer_pid", "primary_viewport")} for pin in pins],
                    "overlay": observed["native_layout"].get("overlay_lock"),
                }) from None
            assert "Private native form probe" not in tmux("capture-pane", "-p", "-t", second)
            directory, session, _ = service._load(handle)
            original_lock = session["native_layout"]["overlay_lock"].copy()
            assert type(original_lock["client_starttime"]) is int
            assert type(original_lock["client_tty_nr"]) is int
            with pytest.raises(ShellCommandError, match="already owns an overlay"):
                service._native_open_text_overlay(directory / "session.json", original_lock["client_pid"], "Competing", ["No"])
            with pytest.raises(ShellCommandError, match="already owns an overlay"):
                service._native_open_menu(directory / "session.json", original_lock["client_pid"])
            assert service._load(handle)[1]["native_layout"]["overlay_lock"] == original_lock
            with pytest.raises(ShellCommandError, match="already owns an overlay"):
                service.dialog(handle, operation_id="competing", form=form, deadline=time.monotonic() + 5,
                               validate_current=lambda: True, deliver=lambda response: False, owner=owner)
            tmux("send-keys", "-t", first, "-l", "NativeDialogCanary8")
            tmux("send-keys", "-t", first, "Tab")
            tmux("send-keys", "-t", first, "-l", "resize preserved")
            eventually(lambda: "resize preserved" in tmux("capture-pane", "-p", "-t", first))
            tmux("resize-window", "-t", first, "-x", "90", "-y", "30")
            eventually(lambda: "resize preserved" in tmux("capture-pane", "-p", "-t", first))
            assert "NativeDialogCanary8" not in tmux("capture-pane", "-p", "-t", first)
            if mode == "disconnect":
                tmux("kill-pane", "-t", first)
            else:
                tmux("send-keys", "-t", first, "C-s")
            thread.join(timeout=8)
            assert not thread.is_alive()
            assert len(result) == 1 and isinstance(result[0], dict), result
            if mode == "submit":
                assert result[0]["state"] == "delivered" and result[0]["ok"] is True
                assert delivered == [True]
            else:
                assert result[0]["state"] == "unavailable" and delivered == []
            assert result[0]["cleanup_pending"] is False
            assert service._load(handle)[1]["native_layout"]["overlay_lock"] is None
            for pane in ((first, second) if mode == "submit" else (second,)):
                assert "NativeDialogCanary8" not in tmux("capture-pane", "-p", "-t", pane)
            for path in (work / "state").rglob("*"):
                if stat.S_ISREG(path.lstat().st_mode):
                    assert b"NativeDialogCanary8" not in path.read_bytes(), path.name
        finally:
            live[0] = False
            try:
                tmux("kill-server")
            except subprocess.SubprocessError:
                pass
            if thread is not None:
                thread.join(timeout=5)
            if handle is not None:
                closed = call(service.close, handle, owner=owner)
                if closed.get("cleanup_state") != "removed":
                    eventually(lambda: call(service.close, handle, owner=owner).get("cleanup_state") == "removed")
