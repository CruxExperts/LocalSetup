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
_COLOR_PAIRS = {
    "body": ("text", "background"),
    "header": ("text", "toolbar"),
    "description": ("muted", "background"),
    "field": ("text", "surface"),
    "field_focus": ("control_text", "control"),
    "focus": ("selection_text", "selected"),
    "button": ("control_text", "control"),
    "error": ("attention", "background"),
}


def _fallback_styles() -> dict[str, int]:
    """Keep meaning visible when the terminal has no usable color support."""
    return {
        "body": curses.A_NORMAL,
        "title": curses.A_BOLD,
        "help": curses.A_NORMAL,
        "description": curses.A_NORMAL,
        "field_label": curses.A_BOLD,
        "field_label_focus": curses.A_BOLD | curses.A_REVERSE,
        "field": curses.A_NORMAL,
        "field_focus": curses.A_REVERSE,
        "confirm": curses.A_NORMAL,
        "choice": curses.A_NORMAL,
        "choice_focus": curses.A_REVERSE,
        "button": curses.A_NORMAL,
        "button_focus": curses.A_REVERSE | curses.A_BOLD,
        "error": curses.A_BOLD,
    }


def _rgb(color: str) -> tuple[int, int, int]:
    if len(color) != 7 or not color.startswith("#"):
        raise ValueError("Theme color is invalid.")
    try:
        return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    except ValueError:
        raise ValueError("Theme color is invalid.") from None


def _terminal_color(color: str, count: int) -> int:
    """Choose the nearest existing xterm color; never redefine terminal colors."""
    target = _rgb(color)
    ansi = (
        (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
        (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
        (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
        (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    )
    candidates = list(enumerate(ansi[:min(16, count)]))
    if count >= 256:
        levels = (0, 95, 135, 175, 215, 255)
        candidates.extend(
            (16 + red * 36 + green * 6 + blue,
             (levels[red], levels[green], levels[blue]))
            for red in range(6) for green in range(6) for blue in range(6)
        )
        candidates.extend((232 + gray, (8 + gray * 10,) * 3) for gray in range(24))
    if not candidates:
        raise ValueError("Terminal has no usable colors.")
    return min(candidates, key=lambda item: sum((actual - wanted) ** 2
                                                 for actual, wanted in zip(item[1], target)))[0]


def _colored_styles(pairs: dict[str, int]) -> dict[str, int]:
    return {
        "body": pairs["body"],
        "title": pairs["header"] | curses.A_BOLD,
        "help": pairs["header"],
        "description": pairs["description"],
        "field_label": pairs["field"] | curses.A_BOLD,
        "field_label_focus": pairs["field_focus"] | curses.A_BOLD,
        "field": pairs["field"],
        "field_focus": pairs["field_focus"],
        "confirm": pairs["field"],
        "choice": pairs["field"],
        "choice_focus": pairs["focus"] | curses.A_BOLD,
        "button": pairs["button"] | curses.A_BOLD,
        "button_focus": pairs["focus"] | curses.A_BOLD,
        "error": pairs["error"] | curses.A_BOLD,
    }


def _dialog_styles() -> dict[str, int]:
    """Adapt the shared semantic palette to curses without changing RGB slots."""
    fallback = _fallback_styles()
    try:
        if not curses.has_colors():
            return fallback
        curses.start_color()
        color_count, pair_count = curses.COLORS, curses.COLOR_PAIRS
        if color_count < 8 or pair_count <= len(_COLOR_PAIRS):
            return fallback
        from .command_theme import palette

        colors = palette({})
        pairs: dict[str, int] = {}
        for pair, (role, (foreground, background)) in enumerate(_COLOR_PAIRS.items(), 1):
            curses.init_pair(pair, _terminal_color(colors[foreground], color_count),
                             _terminal_color(colors[background], color_count))
            pairs[role] = curses.color_pair(pair)
        return _colored_styles(pairs)
    except (curses.error, KeyError, OSError, TypeError, ValueError):
        return fallback


def _fill_row(screen: object, row: int, width: int, style: int) -> None:
    if width > 0:
        screen.addstr(row, 0, " " * width, style)


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


def _draw(screen: object, state: FormState, top: int, *, follow_cursor: bool = True,
          styles: dict[str, int] | None = None) -> int:
    styles = _fallback_styles() if styles is None else styles
    screen.bkgdset(" ", styles["body"])
    screen.erase()
    height, width = screen.getmaxyx()
    state.resize(width, height)
    if not state.fits:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if height and width > 1:
            screen.addstr(0, 0, _clip("Enlarge to 40 columns and 10 rows. Esc cancels.", width - 1), styles["error"])
        screen.refresh()
        return top
    title_lines = _wrap(state.form.title, width - 3)
    fixed_title_lines = min(2, len(title_lines))
    for row, line in enumerate(title_lines[:fixed_title_lines]):
        _fill_row(screen, row, width, styles["title"])
        screen.addstr(row, 1, line, styles["title"])
    help_lines = _wrap(_HELP, width - 3)
    for offset, line in enumerate(help_lines, fixed_title_lines):
        _fill_row(screen, offset, width, styles["title"])
        screen.addstr(offset, 1, line, styles["help"])
    body_start, body_height = _body_layout(width, height, state.form.title)
    lines: list[tuple[str, str]] = []
    cursor_row, cursor_column = 0, 0
    lines.extend((line, "title") for line in title_lines[fixed_title_lines:])
    if state.form.description:
        for paragraph in state.form.description.split("\n"):
            for line in _wrap(paragraph, width - 3):
                lines.append((line, "description"))
    for index, item in enumerate(state.form.fields):
        active = state.focus == index
        for line in _wrap(item.label + (" *" if item.required else ""), width - 3):
            lines.append((line, "field_label_focus" if active else "field_label"))
        if item.kind in {"checkbox", "radio"}:
            for choice_index, choice in enumerate(item.choices):
                marker = ("[x]" if state.selected(index, choice.value) else "[ ]") if item.kind == "checkbox" else ("(o)" if state.selected(index, choice.value) else "( )")
                focused = active and choice_index == state.cursors[index]
                if focused:
                    cursor_row, cursor_column = len(lines), 1
                for line in _wrap(marker + " " + choice.label, width - 3):
                    lines.append((line, "choice_focus" if focused else "choice"))
        else:
            if item.kind == "confirm":
                for line in _wrap("Type: " + item.confirmation, width - 3):
                    lines.append((line, "confirm"))
            value = state.display(index)
            # Password position and length are never part of terminal output.
            position = 0 if item.kind == "password" else state.cursors[index]
            prefix = value[:position]
            caret_line, caret_column = prefix.count("\n"), len(prefix.rsplit("\n", 1)[-1])
            for line_index, line in enumerate(value.split("\n")):
                rendered, cell = _slice(line, caret_column if line_index == caret_line else 0, width - 5)
                if active and line_index == caret_line:
                    cursor_row, cursor_column = len(lines), 2 + cell
                lines.append(("| " + rendered, "field_focus" if active else "field"))
    if follow_cursor and state.focus < len(state.form.fields):
        if cursor_row < top:
            top = cursor_row
        elif cursor_row >= top + body_height:
            top = cursor_row - body_height + 1
    top = max(0, min(top, max(0, len(lines) - body_height)))
    for row, (line, role) in enumerate(lines[top:top + body_height], body_start):
        style = styles[role]
        _fill_row(screen, row, width, style)
        screen.addstr(row, 1, _clip(line, width - 3), style)
    _fill_row(screen, height - 3, width, styles["body"])
    screen.addstr(height - 3, 1, _clip(state.error, width - 3), styles["error"])
    submit = state.focus == len(state.form.fields)
    cancel = state.focus == len(state.form.fields) + 1
    _fill_row(screen, height - 2, width, styles["body"])
    screen.addstr(height - 2, 1, "[ Submit ]", styles["button_focus"] if submit else styles["button"])
    screen.addstr(height - 2, 14, "[ Cancel ]", styles["button_focus"] if cancel else styles["button"])
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
    styles = _dialog_styles()
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
            top = _draw(screen, state, top, follow_cursor=follow_cursor, styles=styles)
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
