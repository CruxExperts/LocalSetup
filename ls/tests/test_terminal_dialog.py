"""Declarative dialog validation and actual private-TTY interaction."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

import pytest

from ls.core.terminal_ops.dialog_model import Choice, Field, Form, FormState


def _state(*fields: Field, now: list[float] | None = None) -> FormState:
    state = FormState(Form("Request", fields), deadline=10, clock=lambda: now[0] if now else 0)
    state.resize(80, 24)
    return state


def test_form_round_trip_uses_literal_inputs_and_deterministic_choice_order() -> None:
    state = _state(
        Field("name", "Name", required=True),
        Field("notes", "Notes", kind="multiline"),
        Field("roles", "Roles", kind="checkbox", choices=(Choice("read", "Read"), Choice("write", "Write"))),
        Field("target", "Target", kind="radio", required=True, choices=(Choice("first", "First"), Choice("second", "Second"))),
        Field("confirm", "Confirmation", kind="confirm", confirmation="ENROLL"),
    )
    state.insert("$(not a command)")
    state.key("enter")
    state.insert("one")
    state.key("enter")
    state.insert("two\tthree")
    state.key("tab")
    state.key("down")
    state.key("space")
    state.key("up")
    state.key("space")
    state.key("tab")
    state.key("space")
    state.key("down")
    state.key("space")
    state.key("tab")
    state.insert("enroll")
    assert state.key("submit") == "editing"
    assert state.error == "The confirmation phrase does not match."
    for _ in range(6):
        state.key("backspace")
    state.insert("ENROLL")
    assert state.key("submit") == "submitted"
    result = state.take_response()
    assert result.values == {
        "name": b"$(not a command)", "notes": b"one\ntwo\tthree", "roles": ("read", "write"),
        "target": ("second",), "confirm": b"ENROLL",
    }
    with pytest.raises(ValueError, match="no undelivered"):
        state.take_response()
    value = result.values["notes"]
    result.dispose()
    assert value == bytearray() and result.values == {}


@pytest.mark.parametrize("bad", ["\x1b[31m", "bad\x00", "a\u202eb", "a\ud800"])
def test_reject_controls_and_invalid_unicode_without_including_values(bad: str) -> None:
    with pytest.raises(ValueError) as error:
        Field("value", bad)
    assert bad not in str(error.value)
    state = _state(Field("secret", "Password", kind="password"))
    assert not state.insert(bad)
    assert state.status == "editing"
    assert state.display(0) == "[hidden]"


def test_secret_redaction_expiry_resize_and_single_transfer() -> None:
    now = [0.0]
    state = _state(Field("secret", "Password", kind="password", required=True), now=now)
    assert state.display(0) == "[hidden]"
    state.insert("a-distinct-canary")
    assert state.display(0) == "[hidden]"
    assert "canary" not in repr(state)
    state.resize(39, 9)
    assert state.key("submit") == "editing"
    state.resize(80, 24)
    assert state.key("submit") == "submitted"
    now[0] = 10
    with pytest.raises(ValueError, match="no undelivered"):
        state.take_response()
    assert state.status == "expired"
    assert all(not value for value in state._buffers)
    assert not state.insert("late")


def test_utf8_limits_paste_and_multiline_caret_preserve_existing_data() -> None:
    state = _state(Field("body", "Body", kind="multiline", max_bytes=15, max_lines=2))
    assert state.insert("abc\n界z")
    state.key("up")
    state.insert("X")
    assert state.display(0) == "abXc\n界z"
    state.key("end")
    assert not state.insert("\nthird\n")
    assert not state.insert("\x13")
    assert not state.insert("界界界")
    assert state.display(0) == "abXc\n界z"
    state.key("submit")
    result = state.take_response()
    assert result.values["body"] == "abXc\n界z".encode()
    result.dispose()


@pytest.mark.parametrize("changes", [
    {"kind": []}, {"kind": "script"}, {"initial": "prefilled", "kind": "password"},
    {"initial": "ENROLL", "kind": "confirm", "confirmation": "ENROLL"},
    {"max_bytes": True}, {"max_bytes": 16385}, {"max_lines": 0}, {"required": "yes"},
    {"validator": "shell command"}, {"kind": "checkbox", "choices": []},
    {"kind": "radio", "choices": [{"value": "x", "label": "X"}], "initial": ["x", "x"]},
])
def test_schema_rejects_unsupported_executable_or_unbounded_fields(changes: dict) -> None:
    with pytest.raises(ValueError):
        Form.from_mapping({"title": "Form", "fields": [{"name": "value", "label": "Value", **changes}]})


def test_small_screen_still_expires_or_cancels_and_selection_is_cleared() -> None:
    now = [0.0]
    state = _state(Field("roles", "Roles", kind="checkbox", choices=(Choice("read", "Read"),)), now=now)
    state.key("space")
    state.resize(1, 1)
    now[0] = 11
    assert state.key("submit") == "expired"
    assert not state.selected(0, "read")
    other = _state(Field("secret", "Secret", kind="password"))
    other.insert("canary")
    other.resize(1, 1)
    assert other.key("cancel") == "cancelled"
    assert not other._buffers[0]


def test_terminal_cleanup_failure_invalidates_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core.terminal_ops import dialog_terminal

    class Terminal:
        def fileno(self) -> int:
            return 99

        def write(self, value: str) -> None:
            if value.endswith("l"):
                raise OSError("terminal disconnected")

        def flush(self) -> None:
            pass

    state = _state(Field("secret", "Secret", kind="password"))
    state.insert("canary")
    original = [1, 2, 3, 4, 5, 6, []]
    restored = []
    monkeypatch.setattr(dialog_terminal.sys, "stdin", Terminal())
    monkeypatch.setattr(dialog_terminal.sys, "stdout", Terminal())
    monkeypatch.setattr(dialog_terminal.os, "isatty", lambda _: True)
    monkeypatch.setattr(dialog_terminal.termios, "tcgetattr", lambda _: original.copy())
    monkeypatch.setattr(dialog_terminal.termios, "tcsetattr", lambda fd, when, attrs: restored.append(attrs))
    monkeypatch.setattr(dialog_terminal.curses, "wrapper", lambda *args: state.key("submit"))
    with pytest.raises(OSError, match="disconnected"):
        dialog_terminal.run_dialog(state)
    assert restored[-1] == original
    assert state.status == "failed" and not state._buffers[0]
    with pytest.raises(ValueError, match="no undelivered"):
        state.take_response()


@pytest.mark.parametrize("mode", ["submit", "cancel", "expire"])
def test_real_terminal_password_paste_resize_and_terminal_restoration(mode: str) -> None:
    """Exercise ncurses with a real PTY, without a desktop or real credentials."""
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    # Both the synthetic value and response stay in the child process memory.
    program = r'''
import fcntl, os, sys, termios, time
from ls.core.terminal_ops.dialog_model import Field, Form, FormState
from ls.core.terminal_ops.dialog_terminal import run_dialog
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
before = termios.tcgetattr(0)
mode = sys.argv[1]
state = FormState(Form("Private dialog probe", (
    Field("password", "Password", kind="password", required=True),
    Field("notes", "Notes", kind="multiline"),
)), deadline=time.monotonic() + (3 if mode == "expire" else 15))
status = run_dialog(state)
restored = before == termios.tcgetattr(0)
if status == "submitted":
    response = state.take_response()
    okay = mode == "submit" and response.values["password"] == b"DialogCanary9" and response.values["notes"] == b"line one\nline two"
    response.dispose()
else:
    okay = status == ("cancelled" if mode == "cancel" else "expired") and all(not value for value in state._buffers)
state.close()
os.write(1, b"\nPROBE_PASS\n" if okay and restored else b"\nPROBE_FAIL\n")
sys.exit(0 if okay and restored else 1)
'''
    process = subprocess.Popen([sys.executable, "-c", program, mode], stdin=slave, stdout=slave, stderr=slave,
                               env={**os.environ, "TERM": "xterm-256color"}, start_new_session=True, close_fds=True)
    output = bytearray()

    def read_until(marker: bytes, timeout: float = 4) -> None:
        deadline = time.monotonic() + timeout
        while marker not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
        assert marker in output, (marker, bytes(output)[-3000:])

    try:
        read_until(b"[hidden]")
        active_attributes = termios.tcgetattr(slave)
        assert not active_attributes[3] & termios.ECHO
        assert not active_attributes[0] & (termios.IXON | termios.IXOFF)
        # A pasted Ctrl-S must be rejected as data, never submit the form.
        os.write(master, b"\x1b[200~bad\x13\x1b[201~")
        read_until(b"unsupported characters")
        if mode == "expire":
            # An interrupted paste must expire without interpreting its contents.
            os.write(master, b"DialogCanary9\t\x1b[200~unfinished paste\x13")
            read_until(b"PROBE_PASS")
            assert process.wait(timeout=3) == 0
            assert termios.tcgetattr(slave) == original
            assert b"DialogCanary9" not in output
            return
        os.write(master, b"DialogCanary9\t\x1b[200~line one\nline two\x1b[201~")
        read_until(b"line two")
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 30, 0, 0))
        process.send_signal(signal.SIGWINCH)
        os.write(master, b"\x13")
        read_until(b"Enlarge")
        time.sleep(0.1)
        assert process.poll() is None
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 26, 90, 0, 0))
        process.send_signal(signal.SIGWINCH)
        assert b"DialogCanary9" not in output
        output.clear()
        read_until(b"line two")
        os.write(master, b"\x13" if mode == "submit" else b"\x03")
        read_until(b"PROBE_PASS")
        assert process.wait(timeout=3) == 0
        assert termios.tcgetattr(slave) == original
        assert b"DialogCanary9" not in output
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)
