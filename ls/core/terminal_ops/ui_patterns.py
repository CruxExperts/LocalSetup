"""Shared overlay geometry and keyboard/mouse close affordance."""
import unicodedata
import os

ESC_LABEL = "[ESC]"


def prepare_curses():
    """Use the actual popup TTY, not inherited COLUMNS/LINES from its parent."""
    import curses
    curses.use_env(False)


def popup_size(screen):
    import curses
    try:
        size = os.get_terminal_size(0)
        if size.lines > 0 and size.columns > 0 and screen.getmaxyx() != (size.lines, size.columns):
            curses.resizeterm(size.lines, size.columns)
    except OSError:
        pass
    return screen.getmaxyx()


def _clip(text, width):
    result, used = [], 0
    for char in text:
        if unicodedata.category(char).startswith("C"):
            char = "?"
        cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + cells > width:
            break
        result.append(char)
        used += cells
    return "".join(result), used


def close_bounds(width):
    right = max(0, width - 1)
    return max(0, right - len(ESC_LABEL)), right


def close_clicked(x, y, width):
    left, right = close_bounds(width)
    return y == 0 and width >= len(ESC_LABEL) + 1 and left <= x < right


def popup_header(screen, text, *, style=0, button_style=0, centered=False):
    _height, width = screen.getmaxyx()
    left, right = close_bounds(width)
    available = max(0, left - 1)
    text, cells = _clip(text, available)
    column = max(0, (available - cells) // 2) if centered else 0
    if text:
        screen.addstr(0, column, text, style)
    if right - left == len(ESC_LABEL):
        screen.addstr(0, left, ESC_LABEL, button_style)
