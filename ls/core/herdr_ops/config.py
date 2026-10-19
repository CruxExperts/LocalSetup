"""Portable, conservative configuration translation for Herdr clients."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import tomllib
from collections.abc import Mapping
from typing import Any


_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_SHELL_MODES = frozenset({"auto", "login", "non_login"})
_SIMPLE_TYPES = (str, bool, int, float)
_PORTABLE_THEME_KEYS = frozenset({"name", "auto_switch"})
_PORTABLE_UI_KEYS = frozenset(
    {
        "sidebar_width",
        "sidebar_min_width",
        "sidebar_max_width",
        "sidebar_start_collapsed",
        "sidebar_collapsed_mode",
        "mobile_width_threshold",
        "mouse_capture",
        "copy_on_select",
        "host_cursor",
        "right_click_passthrough_modifier",
        "redraw_on_focus_gained",
        "mouse_scroll_lines",
        "confirm_close",
        "pane_borders",
        "pane_outer_borders",
        "pane_scrollbars",
        "pane_gaps",
        "show_agent_labels_on_pane_borders",
        "hide_tab_bar_when_single_tab",
        "tab_bar_position",
        "tab_bar_right_separator",
        "agent_panel_sort",
        "accent",
    }
)


def translate_config(
    master: Mapping[str, Any], remote: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Select portable master settings, preserving target-specific choices.

    The result contains only an allowlisted overlay. Master values intentionally
    replace differing values for those settings; remote-only and unknown values
    remain untouched when the overlay is merged. Local paths, keybindings,
    integrations, credentials, and unknown master settings are reported as skips.
    """
    if not isinstance(master, Mapping):
        raise ValueError("master configuration must be a mapping")
    remote_config = remote if isinstance(remote, Mapping) else {}
    proposed: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []

    for key in master:
        if key not in {"theme", "ui", "session", "terminal", "keybindings", "integrations"}:
            skipped.append({"key": str(key), "reason": "not-allowlisted"})

    if "keybindings" in master:
        skipped.append({"key": "keybindings", "reason": "local-keybindings-retained"})
    if "integrations" in master:
        skipped.append({"key": "integrations", "reason": "target-specific-integration"})

    theme = master.get("theme")
    if theme is not None:
        if isinstance(theme, Mapping):
            portable_theme: dict[str, Any] = {}
            for key, value in theme.items():
                dotted = f"theme.{key}"
                if key not in _PORTABLE_THEME_KEYS:
                    skipped.append({"key": dotted, "reason": "not-allowlisted"})
                elif key == "name" and isinstance(value, str) and _safe_text(value):
                    _offer(proposed, remote_config, dotted, value, conflicts)
                elif key == "auto_switch" and isinstance(value, bool):
                    _offer(proposed, remote_config, dotted, value, conflicts)
                else:
                    skipped.append({"key": dotted, "reason": "unsupported-value"})
        else:
            skipped.append({"key": "theme", "reason": "expected-table"})

    ui = master.get("ui")
    if ui is not None:
        if isinstance(ui, Mapping):
            for key, value in ui.items():
                dotted = f"ui.{key}"
                if key not in _PORTABLE_UI_KEYS:
                    skipped.append({"key": dotted, "reason": "not-allowlisted"})
                elif not _portable_value(value):
                    skipped.append({"key": dotted, "reason": "non-portable-value"})
                else:
                    _offer(proposed, remote_config, dotted, value, conflicts)
        else:
            skipped.append({"key": "ui", "reason": "expected-table"})

    session = master.get("session")
    if session is not None:
        if not isinstance(session, Mapping):
            skipped.append({"key": "session", "reason": "expected-table"})
        elif "resume_agents_on_restore" in session:
            value = session["resume_agents_on_restore"]
            if isinstance(value, bool):
                _offer(
                    proposed,
                    remote_config,
                    "session.resume_agents_on_restore",
                    value,
                    conflicts,
                )
            else:
                skipped.append(
                    {
                        "key": "session.resume_agents_on_restore",
                        "reason": "expected-boolean",
                    }
                )
        for key in session:
            if key != "resume_agents_on_restore":
                skipped.append(
                    {"key": f"session.{key}", "reason": "not-allowlisted"}
                )

    terminal = master.get("terminal")
    if terminal is not None:
        if not isinstance(terminal, Mapping):
            skipped.append({"key": "terminal", "reason": "expected-table"})
        else:
            if "new_cwd" in terminal:
                skipped.append(
                    {"key": "terminal.new_cwd", "reason": "target-specific-path"}
                )
            if "shell_mode" in terminal:
                mode = terminal["shell_mode"]
                if isinstance(mode, str) and mode in _SAFE_SHELL_MODES:
                    _offer(
                        proposed,
                        remote_config,
                        "terminal.shell_mode",
                        mode,
                        conflicts,
                    )
                else:
                    skipped.append(
                        {
                            "key": "terminal.shell_mode",
                            "reason": "unsafe-or-unsupported-shell-mode",
                        }
                    )
            for key in terminal:
                if key not in {"new_cwd", "shell_mode"}:
                    skipped.append(
                        {"key": f"terminal.{key}", "reason": "not-allowlisted"}
                    )

    return {
        "settings": proposed,
        "skipped": _dedupe_entries(skipped),
        "conflicts": _dedupe_entries(conflicts),
    }


def merge_config_text(existing_text: str, overlay: Mapping[str, Any]) -> str:
    """Merge a safe overlay into TOML while retaining unknown remote settings.

    This helper is intended for staging only. Callers must validate the staged
    file with Herdr's native config check before activation.
    """
    try:
        original = tomllib.loads(existing_text)
    except (tomllib.TOMLDecodeError, TypeError) as exc:
        raise ValueError("existing Herdr config is invalid TOML") from exc
    if not isinstance(original, dict):
        raise ValueError("existing Herdr config must be a TOML table")
    merged = _deep_copy(original)
    _merge_table(merged, overlay)
    return _dump_toml(merged)


def _offer(
    destination: dict[str, Any],
    remote: Mapping[str, Any],
    dotted_key: str,
    value: Any,
    conflicts: list[dict[str, str]],
) -> None:
    existing = _get_dotted(remote, dotted_key)
    if existing is not _MISSING and existing != value:
        conflicts.append({"key": dotted_key, "reason": "master-value-applied-over-remote"})
    _set_dotted(destination, dotted_key, value)


def _portable_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _safe_text(value: str) -> bool:
    return bool(value) and all(ch >= " " and ch != "\x7f" for ch in value)


def _get_dotted(table: Mapping[str, Any], dotted_key: str) -> Any:
    current: Any = table
    for part in dotted_key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _set_dotted(table: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    current = table
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _merge_table(destination: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError("overlay contains an invalid TOML key")
        if isinstance(value, Mapping):
            current = destination.get(key)
            if not isinstance(current, dict):
                current = {}
                destination[key] = current
            _merge_table(current, value)
        elif _portable_value(value):
            # The master overlay deliberately owns these allowlisted values.
            # Replacing a scalar preserves the remote-only siblings in tables.
            destination[key] = value
        else:
            raise ValueError(f"overlay contains a non-portable value: {key}")


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_deep_copy(child) for child in value]
    return value


def _dump_toml(table: Mapping[str, Any]) -> str:
    lines: list[str] = []

    def emit(current: Mapping[str, Any], prefix: tuple[str, ...], *, array_item: bool = False) -> None:
        if prefix:
            if lines and lines[-1] != "":
                lines.append("")
            opener, closer = ("[[", "]]" ) if array_item else ("[", "]")
            lines.append(opener + ".".join(_toml_key(part) for part in prefix) + closer)
        scalars = [
            (key, value)
            for key, value in current.items()
            if not isinstance(value, Mapping) and not _array_of_tables(value)
        ]
        children = [(key, value) for key, value in current.items() if isinstance(value, Mapping)]
        array_tables = [(key, value) for key, value in current.items() if _array_of_tables(value)]
        for key, value in scalars:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in children:
            emit(value, (*prefix, key))
        for key, entries in array_tables:
            for entry in entries:
                emit(entry, (*prefix, key), array_item=True)

    emit(table, ())
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{ " + ", ".join(f"{_toml_key(key)} = {_toml_value(child)}" for key, child in value.items()) + " }"
    raise ValueError(f"unsupported TOML value: {type(value).__name__}")


def _array_of_tables(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, Mapping) for item in value)


def _toml_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("TOML keys must be strings")
    if _KEY_RE.fullmatch(value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _dedupe_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, str]] = []
    for entry in entries:
        identity = (entry["key"], entry["reason"])
        if identity not in seen:
            seen.add(identity)
            output.append(entry)
    return output


_MISSING = object()
