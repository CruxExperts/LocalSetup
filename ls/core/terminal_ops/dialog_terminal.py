"""Private terminal rendering for declarative forms; no shell or storage I/O."""

from __future__ import annotations

import curses
import os
import sys
import termios
import unicodedata
from collections.abc import Callable

from .dialog_model import FormState


def _cells(value: str) -> int:
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in value)


def _clip(value: str, width: int) -> str:
    result, used = [], 0
    for ch in value.replace("\t", "    "):
        cells = _cells(ch)
        if used + cells > width:
            break
        result.append(ch)
        used += cells
    return "".join(result)


def _slice(value: str, cursor: int, width: int) -> tuple[str, int]:
    """Keep the caret visible without cutting a wide terminal cell in half."""
    start, used = cursor, 0
    for position in range(cursor - 1, -1, -1):
        cells = 4 if value[position] == "\t" else _cells(value[position])
        if used + cells >= width:
            break
        start, used = position, used + cells
    return _clip(value[start:], width), used


def _draw(screen: object, state: FormState, top: int) -> int:
    screen.erase()
    height, width = screen.getmaxyx()
    state.resize(width, height)
    if not state.fits:
        if height and width > 1:
            screen.addstr(0, 0, _clip("Enlarge to 40 columns and 10 rows. Esc cancels.", width - 1))
        screen.refresh()
        return top
    screen.addstr(0, 1, _clip(state.form.title, width - 3), curses.A_BOLD)
    screen.addstr(1, 1, _clip("Tab focus  Arrows move  Space choose  Ctrl-S submit  Esc cancel", width - 3))
    lines: list[tuple[str, int]] = []
    cursor_row, cursor_column = 0, 0
    for line in state.form.description.splitlines():
        lines.append((line, curses.A_DIM))
    for index, item in enumerate(state.form.fields):
        active = state.focus == index
        lines.append((item.label + (" *" if item.required else ""), curses.A_BOLD if active else 0))
        if item.kind in {"checkbox", "radio"}:
            for choice_index, choice in enumerate(item.choices):
                marker = ("[x]" if state.selected(index, choice.value) else "[ ]") if item.kind == "checkbox" else ("(o)" if state.selected(index, choice.value) else "( )")
                focused = active and choice_index == state.cursors[index]
                if focused:
                    cursor_row, cursor_column = len(lines), 1
                lines.append((marker + " " + choice.label, curses.A_REVERSE if focused else 0))
        else:
            value = state.display(index)
            # Password position and length are never part of terminal output.
            position = 0 if item.kind == "password" else state.cursors[index]
            prefix = value[:position]
            caret_line, caret_column = prefix.count("\n"), len(prefix.rsplit("\n", 1)[-1])
            for line_index, line in enumerate(value.split("\n")):
                rendered, cell = _slice(line, caret_column if line_index == caret_line else 0, width - 5)
                if active and line_index == caret_line:
                    cursor_row, cursor_column = len(lines), 2 + cell
                lines.append(("| " + rendered, curses.A_REVERSE if active else 0))
            if item.kind == "confirm":
                lines.append(("Type: " + item.confirmation, curses.A_DIM))
    body_height = height - 5
    if state.focus < len(state.form.fields):
        if cursor_row < top:
            top = cursor_row
        elif cursor_row >= top + body_height:
            top = cursor_row - body_height + 1
    top = max(0, min(top, max(0, len(lines) - body_height)))
    for row, (line, style) in enumerate(lines[top:top + body_height], 2):
        screen.addstr(row, 1, _clip(line, width - 3), style)
    screen.addstr(height - 3, 1, _clip(state.error, width - 3), curses.A_BOLD)
    submit = state.focus == len(state.form.fields)
    cancel = state.focus == len(state.form.fields) + 1
    screen.addstr(height - 2, 1, "[ Submit ]", curses.A_REVERSE if submit else curses.A_NORMAL)
    screen.addstr(height - 2, 14, "[ Cancel ]", curses.A_REVERSE if cancel else curses.A_NORMAL)
    if state.focus < len(state.form.fields):
        screen.move(2 + cursor_row - top, min(width - 2, 1 + cursor_column))
    else:
        screen.move(height - 2, 2 if submit else 15)
    screen.refresh()
    return top


def _paste(screen: object, state: FormState, alive: Callable[[], bool]) -> None:
    """Drain bracketed paste as data, even when it contains control keystrokes."""
    ending = b"\x1b[201~"
    pending = bytearray()
    rejected = False
    screen.keypad(False)
    try:
        while state.tick() == "editing":
            if not alive():
                state.close("unavailable")
                return
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if isinstance(key, int):
                continue
            encoded = key.encode("utf-8")
            if len(pending) + len(encoded) > 65542:
                rejected = True
                pending[:] = b"\0" * len(pending)
                pending.clear()
            pending.extend(encoded)
            if pending.endswith(ending):
                if rejected:
                    state.error = "Pasted input exceeds the supported limit."
                else:
                    state.insert(pending[:-len(ending)].decode("utf-8"))
                return
    finally:
        pending[:] = b"\0" * len(pending)
        pending.clear()
        screen.keypad(True)


def _escape(screen: object, state: FormState, alive: Callable[[], bool]) -> None:
    # ncurses leaves unknown bracketed-paste delimiters as literal characters.
    screen.timeout(30)
    sequence = ""
    try:
        while len(sequence) < 5:
            try:
                key = screen.get_wch()
            except curses.error:
                break
            if not isinstance(key, str):
                break
            sequence += key
            if not "[200~".startswith(sequence):
                break
    finally:
        screen.timeout(100)
    if sequence == "[200~":
        _paste(screen, state, alive)
    elif not sequence:
        state.key("escape")


def _sync_size(screen: object, state: FormState) -> None:
    size = os.get_terminal_size(sys.stdin.fileno())
    state.resize(size.columns, size.lines)
    if size.lines > 0 and size.columns > 0 and screen.getmaxyx() != (size.lines, size.columns):
        curses.resizeterm(size.lines, size.columns)


def _run(screen: object, state: FormState, alive: Callable[[], bool]) -> None:
    curses.raw()
    curses.noecho()
    screen.keypad(True)
    screen.timeout(100)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    mapping = {
        curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
        curses.KEY_RIGHT: "right", curses.KEY_HOME: "home", curses.KEY_END: "end",
        curses.KEY_BACKSPACE: "backspace", curses.KEY_DC: "delete", curses.KEY_BTAB: "backtab",
        curses.KEY_ENTER: "enter", "\t": "tab", "\r": "enter", "\n": "enter",
        "\x7f": "backspace", "\b": "backspace", "\x13": "submit", "\x03": "cancel", " ": "space",
    }
    top = 0
    while state.tick() == "editing":
        if not alive():
            state.close("unavailable")
            break
        # Resize can race a draw. Preserve state and retry on the next frame.
        try:
            _sync_size(screen, state)
            top = _draw(screen, state, top)
        except curses.error:
            curses.napms(20)
            continue
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        # A resize or consumer exit may occur while waiting for this key.
        _sync_size(screen, state)
        if not alive():
            state.close("unavailable")
            break
        if key == "\x1b":
            _escape(screen, state, alive)
        elif key in mapping:
            state.key(mapping[key])
        elif isinstance(key, str):
            state.insert(key)


def run_dialog(state: FormState, *, alive: Callable[[], bool] = lambda: True) -> str:
    """Run in the admitted helper's private TTY, returning only a safe status.

    The caller owns response delivery and must dispose state on all failure
    paths. Python and ncurses may retain temporary copies; this is not a secure
    memory zeroization guarantee.
    """
    fd = sys.stdin.fileno()
    if not os.isatty(fd) or not os.isatty(sys.stdout.fileno()):
        state.close("unavailable")
        return state.status
    original = termios.tcgetattr(fd)
    attributes = termios.tcgetattr(fd)
    attributes[0] &= ~(termios.IXON | termios.IXOFF)
    attributes[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
        curses.wrapper(_run, state, alive)
    except (KeyboardInterrupt, EOFError):
        state.close()
    except BaseException:
        state.close("failed")
        raise
    finally:
        try:
            try:
                sys.stdout.write("\x1b[?2004l")
                sys.stdout.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSANOW, original)
        except BaseException:
            state.close("failed")
            raise
    return state.status
