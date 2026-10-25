"""Shared terminal capability and compact semantic output styling."""
import os
from pathlib import Path
import stat


def _read_presentation(path):
    from ..application_profiles.service import _bounded_json
    limit = 2_000_000
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError("invalid presentation record")
        value = _bounded_json(stream.read(limit + 1), label="terminal presentation", limit=limit)
    if not isinstance(value, dict):
        raise ValueError("invalid presentation object")
    return value


def selected_presentation():
    """Read the current managed theme, or user defaults, without writing state."""
    from ..application_profiles.service import resolve_tmux_runtime_profile
    from ..terminal_ops.command_theme import palette, validate_theme
    try:
        path = os.environ.get("LOCALSETUP_TERMINAL_SESSION_RECORD")
        if path:
            record_path = Path(path)
            if not record_path.is_absolute():
                raise ValueError("relative presentation context")
            record = _read_presentation(record_path)
            if record.get("session_record_path") != str(record_path):
                raise ValueError("presentation context identity differs")
            settings = record["runtime_settings"]
        else:
            settings = resolve_tmux_runtime_profile(
                source_root=Path(__file__).resolve().parents[3], home=Path.home(),
                initialize=False)["settings"]
        validate_theme(settings["theme"])
        return palette({"runtime_settings": settings}), settings.get("colors_enabled", True) is True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Bad display preferences cannot interrupt a protected workload or
        # introduce terminal controls. They convey no execution authority.
        return palette({}), False


def color_capable(stream, environ=None):
    environ = os.environ if environ is None else environ
    if "NO_COLOR" in environ:
        return False
    if environ.get("FORCE_COLOR") not in {None, "", "0"}:
        return True
    try:
        return bool(stream.isatty()) and environ.get("TERM") not in {None, "", "dumb"}
    except (AttributeError, OSError):
        return False


def render_parts(parts, *, color=False, width=None, theme=None, pad=False):
    """Clip plain ASCII segments before styling, never through an ANSI escape."""
    if color:
        from ..terminal_ops.command_theme import ansi
        if theme is None:
            theme, enabled = selected_presentation()
            color = color and enabled
        styles = {"brand": "accent", "label": "muted", "value": "text",
                  "success": "success", "warning": "attention", "error": "error"}
    output = []
    for text, role in parts:
        if width is not None:
            text = text[:max(0, width)]
            width -= len(text)
        if text:
            output.append(ansi(theme[styles[role]], theme["background"], bold=role != "label")
                          + text + "\x1b[0m" if color else text)
    if pad and width is not None:
        output.append(" " * max(0, width))
    return "".join(output)
