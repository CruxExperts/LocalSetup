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


_HELP = "Tab focus | PageUp/PageDown details | Ctrl-S submit | Esc cancel"


def _wrap(value: str, width: int) -> list[str]:
    """Wrap static text by terminal cells without dropping any characters."""
    width = max(1, width)
    result: list[str] = []
    for paragraph in value.replace("\t", "    ").split("\n"):
        line: list[str] = []
        used = 0
        for character in paragraph:
            cells = _cells(character)
            if line and used + cells > width:
                text = "".join(line)
                boundary = text.rfind(" ")
                if boundary > 0:
                    result.append(text[:boundary + 1])
                    line = list(text[boundary + 1:])
                    used = _cells("".join(line))
                    if used + cells > width:
                        result.append("".join(line))
                        line, used = [], 0
                else:
                    result.append(text)
                    line, used = [], 0
            line.append(character)
            used += cells
        result.append("".join(line))
    return result


def _body_layout(width: int, height: int, title: str = "") -> tuple[int, int]:
    title_lines = _wrap(title, width - 3) if title else [""]
    fixed_title_lines = min(2, len(title_lines))
    help_lines = _wrap(_HELP, width - 3)
    body_start = fixed_title_lines + len(help_lines)
    return body_start, max(1, height - 3 - body_start)


def _draw(screen: object, state: FormState, top: int, *, follow_cursor: bool = True) -> int:
    screen.erase()
    height, width = screen.getmaxyx()
    state.resize(width, height)
    if not state.fits:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if height and width > 1:
            screen.addstr(0, 0, _clip("Enlarge to 40 columns and 10 rows. Esc cancels.", width - 1))
        screen.refresh()
        return top
    title_lines = _wrap(state.form.title, width - 3)
    fixed_title_lines = min(2, len(title_lines))
    for row, line in enumerate(title_lines[:fixed_title_lines]):
        screen.addstr(row, 1, line, curses.A_BOLD)
    help_lines = _wrap(_HELP, width - 3)
    for offset, line in enumerate(help_lines, fixed_title_lines):
        screen.addstr(offset, 1, line)
    body_start, body_height = _body_layout(width, height, state.form.title)
    lines: list[tuple[str, int]] = []
    cursor_row, cursor_column = 0, 0
    lines.extend((line, curses.A_BOLD) for line in title_lines[fixed_title_lines:])
    if state.form.description:
        for paragraph in state.form.description.split("\n"):
            for line in _wrap(paragraph, width - 3):
                lines.append((line, curses.A_NORMAL))
    for index, item in enumerate(state.form.fields):
        active = state.focus == index
        for line in _wrap(item.label + (" *" if item.required else ""), width - 3):
            lines.append((line, curses.A_BOLD if active else 0))
        if item.kind in {"checkbox", "radio"}:
            for choice_index, choice in enumerate(item.choices):
                marker = ("[x]" if state.selected(index, choice.value) else "[ ]") if item.kind == "checkbox" else ("(o)" if state.selected(index, choice.value) else "( )")
                focused = active and choice_index == state.cursors[index]
                if focused:
                    cursor_row, cursor_column = len(lines), 1
                for line in _wrap(marker + " " + choice.label, width - 3):
                    lines.append((line, curses.A_REVERSE if focused else 0))
        else:
            if item.kind == "confirm":
                for line in _wrap("Type: " + item.confirmation, width - 3):
                    lines.append((line, curses.A_NORMAL))
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
    if follow_cursor and state.focus < len(state.form.fields):
        if cursor_row < top:
            top = cursor_row
        elif cursor_row >= top + body_height:
            top = cursor_row - body_height + 1
    top = max(0, min(top, max(0, len(lines) - body_height)))
    for row, (line, style) in enumerate(lines[top:top + body_height], 2):
        screen.addstr(body_start + row - 2, 1, _clip(line, width - 3), style)
    screen.addstr(height - 3, 1, _clip(state.error, width - 3), curses.A_BOLD)
    submit = state.focus == len(state.form.fields)
    cancel = state.focus == len(state.form.fields) + 1
    screen.addstr(height - 2, 1, "[ Submit ]", curses.A_REVERSE if submit else curses.A_NORMAL)
    screen.addstr(height - 2, 14, "[ Cancel ]", curses.A_REVERSE if cancel else curses.A_NORMAL)
    cursor_visible = follow_cursor and state.focus < len(state.form.fields) and top <= cursor_row < top + body_height
    if cursor_visible:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        screen.move(body_start + cursor_row - top, min(width - 2, 1 + cursor_column))
    elif follow_cursor:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        screen.move(height - 2, 2 if submit else 15)
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
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


def _run(screen: object, state: FormState, alive: Callable[[], bool], on_visible: Callable[[], None]) -> None:
    curses.raw()
    curses.noecho()
    if curses.has_colors():
        curses.use_default_colors()
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
    follow_cursor = True
    announced = False
    while state.tick() == "editing":
        if not alive():
            state.close("unavailable")
            break
        # Resize can race a draw. Preserve state and retry on the next frame.
        try:
            _sync_size(screen, state)
            top = _draw(screen, state, top, follow_cursor=follow_cursor)
            if not announced:
                on_visible()
                announced = True
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
        if key in {curses.KEY_PPAGE, curses.KEY_NPAGE}:
            width, height = screen.getmaxyx()[::-1]
            _, body_height = _body_layout(width, height, state.form.title)
            step = max(1, body_height - 1)
            top = max(0, top - step) if key == curses.KEY_PPAGE else top + step
            follow_cursor = False
            continue
        follow_cursor = True
        if key == "\x1b":
            _escape(screen, state, alive)
        elif key in mapping:
            state.key(mapping[key])
        elif isinstance(key, str):
            state.insert(key)


def run_dialog(state: FormState, *, alive: Callable[[], bool] = lambda: True,
               on_visible: Callable[[], None] = lambda: None) -> str:
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
        curses.wrapper(_run, state, alive, on_visible)
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
