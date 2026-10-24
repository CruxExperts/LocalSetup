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


def test_static_text_wraps_by_terminal_cells_and_pages_without_losing_content() -> None:
    from ls.core.terminal_ops import dialog_terminal

    fingerprint = "A1B2C3D4" * 5
    description = (
        f"Purpose: approve operation {fingerprint}\n"
        f"Recipients: {fingerprint}, {fingerprint}\n"
        "Wide and combining text: 界e\u0301 remains readable after wrapping."
    )
    form = Form(
        "Certificate enrollment request",
        (Field("fingerprint", "Expected full fingerprint", kind="confirm", confirmation=fingerprint),),
        description=description,
    )

    class Screen:
        def __init__(self, width: int, height: int):
            self.width, self.height = width, height
            self.writes: list[tuple[int, int, str]] = []
            self.moves: list[tuple[int, int]] = []
            self.background = 0

        def erase(self) -> None:
            self.writes.clear()
            self.moves.clear()

        def bkgdset(self, _character: str, style: int) -> None:
            self.background = style

        def getmaxyx(self) -> tuple[int, int]:
            return self.height, self.width

        def addstr(self, row: int, column: int, value: str, *_style: int) -> None:
            assert 0 <= row < self.height and 0 <= column < self.width
            assert dialog_terminal._cells(value) <= self.width - column
            self.writes.append((row, column, value))

        def move(self, row: int, column: int) -> None:
            assert 0 <= row < self.height and 0 <= column < self.width
            self.moves.append((row, column))

        def refresh(self) -> None:
            pass

    wrapped = dialog_terminal._wrap("界e\u0301" + "X" * 42, 9)
    assert "".join(wrapped) == "界e\u0301" + "X" * 42
    assert all(dialog_terminal._cells(line) <= 9 for line in wrapped)

    state = FormState(form, deadline=30)
    screen = Screen(40, 10)
    body_start, body_height = dialog_terminal._body_layout(40, 10, form.title)
    top = 0
    rendered_rows: dict[int, str] = {}
    for _ in range(20):
        top = dialog_terminal._draw(screen, state, top, follow_cursor=False)
        assert screen.background == dialog_terminal.curses.A_NORMAL
        for row, _column, value in screen.writes:
            if body_start <= row < screen.height - 3:
                rendered_rows[top + row - body_start] = value
        next_top = dialog_terminal._draw(screen, state, top + body_height - 1, follow_cursor=False)
        if next_top == top:
            break
        top = next_top

    rendered = "".join(rendered_rows[index] for index in range(max(rendered_rows) + 1))
    assert description.replace("\n", "") in rendered
    assert "Type: " + fingerprint in rendered
    assert state.display(0) == ""

    # A valid maximum-size title retains its fixed action prefix while the full
    # title remains reachable in bounded scrolling body rows.
    long_title = ("A" * 20 + " ") * 12
    long_form = Form(long_title, (Field("password", "Passphrase", kind="password"),))
    long_state = FormState(long_form, deadline=30)
    long_screen = Screen(40, 10)
    long_start, long_height = dialog_terminal._body_layout(40, 10, long_title)
    title_lines = dialog_terminal._wrap(long_title, 37)
    fixed_title: dict[int, str] = {}
    continued_title: dict[int, str] = {}
    top = 0
    for _ in range(20):
        top = dialog_terminal._draw(long_screen, long_state, top, follow_cursor=False)
        for row, _column, value in long_screen.writes:
            if row < 2:
                fixed_title[row] = value
            elif long_start <= row < long_screen.height - 3:
                continued_title[top + row - long_start] = value
        next_top = dialog_terminal._draw(long_screen, long_state, top + long_height - 1, follow_cursor=False)
        if next_top == top:
            break
        top = next_top
    assert len(fixed_title) == 2
    assert "".join(fixed_title[index] for index in range(2)) == "".join(title_lines[:2])
    continuation_count = len(title_lines) - 2
    continuation = "".join(continued_title[index] for index in range(continuation_count))
    assert "".join(title_lines[:2]) + continuation == long_title


def test_dialog_uses_shared_theme_roles_and_no_color_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core.terminal_ops import command_theme, dialog_terminal

    theme = {
        "background": "#061020", "surface": "#0B1830", "text": "#F4F7FF",
        "muted": "#A8B9D5", "toolbar": "#173F63", "control": "#2A5B82",
        "selected": "#7DDCF7", "selection_text": "#082438", "attention": "#FFD27A",
    }
    received_sessions = []
    pair_values: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(command_theme, "palette", lambda session: received_sessions.append(session) or theme)
    monkeypatch.setattr(dialog_terminal.curses, "has_colors", lambda: True)
    monkeypatch.setattr(dialog_terminal.curses, "start_color", lambda: None)
    monkeypatch.setattr(dialog_terminal.curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(dialog_terminal.curses, "COLOR_PAIRS", 64, raising=False)
    monkeypatch.setattr(dialog_terminal.curses, "init_pair", lambda pair, foreground, background: pair_values.__setitem__(pair, (foreground, background)))
    monkeypatch.setattr(dialog_terminal.curses, "color_pair", lambda pair: pair << 8)
    monkeypatch.setattr(dialog_terminal.curses, "init_color", lambda *_args: pytest.fail("terminal RGB slots must not be redefined"), raising=False)

    styles = dialog_terminal._dialog_styles()
    assert received_sessions == [{}]
    assert set(pair_values) == set(range(1, len(dialog_terminal._COLOR_PAIRS) + 1))
    for pair, (_role, (foreground, background)) in enumerate(dialog_terminal._COLOR_PAIRS.items(), 1):
        assert pair_values[pair] == (
            dialog_terminal._terminal_color(theme[foreground], 256),
            dialog_terminal._terminal_color(theme[background], 256),
        )
    assert styles["body"] == 1 << 8
    assert styles["title"] == (2 << 8) | dialog_terminal.curses.A_BOLD
    assert styles["field"] == 4 << 8
    assert styles["field_focus"] == 5 << 8
    assert styles["choice_focus"] == (6 << 8) | dialog_terminal.curses.A_BOLD
    assert styles["button"] == (7 << 8) | dialog_terminal.curses.A_BOLD
    assert styles["button_focus"] == (6 << 8) | dialog_terminal.curses.A_BOLD
    assert styles["error"] == (8 << 8) | dialog_terminal.curses.A_BOLD

    class Screen:
        def __init__(self) -> None:
            self.background = 0
            self.writes: list[tuple[int, int, str, int]] = []

        def bkgdset(self, _character: str, style: int) -> None:
            self.background = style

        def erase(self) -> None:
            self.writes.clear()

        def getmaxyx(self) -> tuple[int, int]:
            return 10, 40

        def addstr(self, row: int, column: int, value: str, style: int = 0) -> None:
            self.writes.append((row, column, value, style))

        def move(self, _row: int, _column: int) -> None:
            pass

        def refresh(self) -> None:
            pass

    screen = Screen()
    state = FormState(Form("Unlock signing key", (Field("passphrase", "Passphrase", kind="password"),)),
                      deadline=30, clock=lambda: 0)
    dialog_terminal._draw(screen, state, 0, styles=styles)
    assert screen.background == styles["body"]
    assert any(row == 0 and style == styles["title"] for row, _column, _value, style in screen.writes)
    assert any(row >= 3 and value == " " * 40 and style == styles["field_focus"]
               for row, _column, value, style in screen.writes)
    assert any(value == "| [hidden]" and style == styles["field_focus"]
               for _row, _column, value, style in screen.writes)
    state.key("tab")
    state.error = "Input was not accepted."
    dialog_terminal._draw(screen, state, 0, styles=styles)
    assert any(value == "[ Submit ]" and style == styles["button_focus"]
               for _row, _column, value, style in screen.writes)
    assert any(value == "[ Cancel ]" and style == styles["button"]
               for _row, _column, value, style in screen.writes)
    assert any(value == "Input was not accepted." and style == styles["error"]
               for _row, _column, value, style in screen.writes)

    monkeypatch.setattr(dialog_terminal.curses, "has_colors", lambda: False)
    monkeypatch.setattr(dialog_terminal.curses, "start_color", lambda: pytest.fail("color setup should not run"))
    monkeypatch.setattr(dialog_terminal.curses, "init_pair", lambda *_args: pytest.fail("color pairs should not be required"))
    assert dialog_terminal._dialog_styles() == dialog_terminal._fallback_styles()


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


def test_real_terminal_pages_long_details_and_preserves_masked_input_on_resize() -> None:
    """Page details at the minimum supported size and preserve input across resize."""
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 40, 0, 0))
    program = r'''
import fcntl, os, signal, sys, termios, time
from ls.core.terminal_ops.dialog_model import Field, Form, FormState
from ls.core.terminal_ops.dialog_terminal import run_dialog
fingerprint = "0123456789ABCDEF0123456789ABCDEFFEDCBA98"
details = "TOP_DETAIL " + ("The enrollment purpose and recipient set are shown in full. " * 3)
details += "\nRecipients: " + fingerprint
details += "\n" + "\n".join(f"DETAIL_LINE_{index:02d} operation context" for index in range(8))
state = FormState(Form("Enroll certificate", (
    Field("passphrase", "Private passphrase", kind="password", required=True),
    Field("fingerprint", "Expected fingerprint", kind="confirm", required=True, confirmation=fingerprint),
), description=details), deadline=time.monotonic() + 15)
status = run_dialog(state)
okay = status == "submitted"
if okay:
    response = state.take_response()
    okay = response.values["passphrase"] == b"DialogResizeCanary9" and response.values["fingerprint"] == fingerprint.encode()
    response.dispose()
state.close()
os.write(1, b"\nPROBE_PASS\n" if okay else b"\nPROBE_FAIL\n")
sys.exit(0 if okay else 1)
'''
    process = subprocess.Popen([sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=slave,
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

    fingerprint = "0123456789ABCDEF0123456789ABCDEFFEDCBA98"
    secret = b"DialogResizeCanary9"
    try:
        read_until(b"[hidden]")
        assert b"TOP_DETAIL" not in output
        assert secret not in output

        # Repeated PageUp reaches the beginning of the long, wrapped operation details.
        os.write(master, b"\x1b[5~" * 16)
        read_until(b"TOP_DETAIL")
        assert secret not in output

        # PageDown returns toward the active field; input then restores follow mode.
        os.write(master, b"\x1b[6~" * 16)
        read_until(b"Private passphrase")
        read_until(b"Type:")
        os.write(master, secret)
        time.sleep(0.1)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 30, 0, 0))
        process.send_signal(signal.SIGWINCH)
        read_until(b"Enlarge")
        assert secret not in output
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 40, 0, 0))
        process.send_signal(signal.SIGWINCH)
        time.sleep(0.15)
        assert secret not in output

        os.write(master, b"\t" + fingerprint.encode() + b"\x13")
        read_until(b"PROBE_PASS")
        assert process.wait(timeout=3) == 0
        assert termios.tcgetattr(slave) == original
        assert secret not in output
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)
