"""Explicit, readable surfaces for owned native controls and popups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from functools import lru_cache
import json
import re
from pathlib import Path
from typing import Any

from ..application_profiles.service import load_tmux_runtime_defaults


PRESENTATION_KEYS = frozenset({'theme', 'startup_mode', 'inspection_enabled',
    'inspection_duration_seconds', 'reflow_debounce_ms', 'double_escape_ms',
    'auto_close_completed', 'auto_close_delay_seconds', 'status_detail',
    'toolbar_visible', 'shortcut_hints_visible', 'colors_enabled'})


@lru_cache(maxsize=1)
def _defaults() -> dict[str, str]:
    return load_tmux_runtime_defaults(source_root=Path(__file__).resolve().parents[3],
                                     name="agent-shell")["settings"]["theme"]


# Themes affect framework chrome only; application output is never recolored.
LEGACY_STOCK_PALETTE = {'background': '#061020', 'surface': '#0B1830', 'text': '#FFFFFF', 'muted': '#BFD2E8', 'border': '#35699C', 'accent': '#41B8FF', 'toolbar': '#00549E', 'control': '#B9E2FF', 'control_text': '#06233A', 'selected': '#42DDF7', 'selection_text': '#06233A', 'attention': '#FFB52E'}


def themes() -> list[dict[str, Any]]:
    """Return independent, versioned JSON definitions for the shared renderer."""
    root = Path(__file__).resolve().parents[2] / "config/application-profiles/tmux/themes"
    definitions = []
    for name in ("midnight", "graphite", "high-contrast"):
        definition = json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
        validate_theme(definition)
        definitions.append(definition)
    return definitions


def validate_theme(value: Mapping[str, Any]) -> None:
    """Validate semantic text pairs without interpreting application bytes."""
    roles = value.get("roles", value)
    if not isinstance(roles, Mapping) or any(
            not isinstance(color, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", color) is None
            for color in roles.values()):
        raise ValueError("Theme roles require six-digit RGB colors")
    roles = _defaults() | dict(roles)
    for foreground, background in (("text", "background"), ("text", "surface"),
            ("text", "toolbar"), ("muted", "background"), ("control_text", "control"),
            ("selection_text", "selected"), ("selection_text", "attention"),
            ("success", "background"), ("error", "background"), ("running", "background")):
        if foreground in roles and background in roles and _contrast(roles[foreground], roles[background]) < 4.5:
            raise ValueError(f"Theme {foreground}/{background} contrast must be at least 4.5")
    if 'roles' in value:
        # Versioned semantic roles are also used on pane borders and collapsed
        # rows. Preserve the older flat-palette compatibility contract.
        for foreground in ('muted', 'attention', 'success', 'error', 'running'):
            for background in ('background', 'surface', 'toolbar'):
                if _contrast(roles[foreground], roles[background]) < 4.5:
                    raise ValueError(f"Theme {foreground}/{background} contrast must be at least 4.5")


def theme_definition(name: str) -> dict[str, Any]:
    for theme in themes():
        if theme["name"].casefold().replace(" ", "-") == name.casefold().replace(" ", "-"):
            return deepcopy(theme)
    raise ValueError("Unknown terminal theme")


def palette(session: Mapping[str, Any]) -> dict[str, str]:
    """Resolve versioned themes and preserve custom legacy flat palettes."""
    defaults = _defaults()
    value = session.get("runtime_settings", {}).get("theme", {})
    saved = dict(value.get("roles", value))
    # Recognize old shipped colors separately; custom flat colors remain intact.
    stock_keys = LEGACY_STOCK_PALETTE.keys() - {"control_text"}
    if (stock_keys <= saved.keys() and all(
            color == (LEGACY_STOCK_PALETTE | defaults).get(key)
            for key, color in saved.items() if key not in LEGACY_STOCK_PALETTE)
            and all(saved[key] == LEGACY_STOCK_PALETTE[key] for key in stock_keys)
            and saved.get("control_text", LEGACY_STOCK_PALETTE["control_text"])
                == LEGACY_STOCK_PALETTE["control_text"]):
        saved = {}
    colors = defaults | saved
    if "control_text" not in saved:
        colors["control_text"] = _best_control_ink(colors)
    return colors


def preview(theme: Mapping[str, Any], *, color: bool = True) -> str:
    """Produce chooser-local preview text, without changing session preferences."""
    validate_theme(theme)
    colors = palette({"runtime_settings": {"theme": theme}})
    text = f"{theme['name']}: Ready  Running  Completed  Attention"
    return ansi(colors["text"], colors["toolbar"]) + text + "\x1b[0m" if color else text


def _luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
              for value in channels]
    return sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))


def _contrast(foreground: str, background: str) -> float:
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + .05) / (dark + .05)


def _best_control_ink(colors: Mapping[str, str]) -> str:
    """Select readable ink from session colors, with black/white safety fallbacks."""
    saved_colors = (colors["text"], colors["selection_text"])
    saved_ink = max(saved_colors, key=lambda color: _contrast(color, colors["control"]))
    if _contrast(saved_ink, colors["control"]) >= 4.5:
        return saved_ink
    safety_colors = ("#FFFFFF", "#000000")
    return max(safety_colors, key=lambda color: _contrast(color, colors["control"]))


def ansi(foreground: str, background: str, *, bold: bool = False) -> str:
    def channels(color: str) -> str:
        return ";".join(str(int(color[index:index + 2], 16)) for index in (1, 3, 5))

    return f"\x1b[0;{'1;' if bold else ''}38;2;{channels(foreground)};48;2;{channels(background)}m"


def footer(session: Mapping[str, Any], text: str,
           targets: Sequence[tuple[int, int, str, str | None]], *, color: bool = True) -> str:
    """Style existing cell positions; mouse targets remain identical."""
    if not color or not session.get("runtime_settings", {}).get("colors_enabled", True):
        return text
    colors = palette(session)
    base = ansi(colors["text"], colors["toolbar"])
    output, position = [base], 0
    mode = session.get("native_layout", {}).get("mode")
    for left, right, action, value in targets:
        output.extend((base, text[position:left]))
        label = text[left:right]
        selected = action == "mode" and value == mode
        if action == "mode":
            label = ("●" if selected else "○") + " " + label[3:] + " "
        elif label.startswith("[") and label.endswith("]"):
            label = " " + label[1:-1] + " "
        output.extend((ansi(colors["selection_text"] if selected else colors["control_text"],
                            colors["selected"] if selected else colors["control"], bold=True), label))
        position = right
    trailing = text[position:]
    if "!pending" in trailing:
        output.extend((ansi(colors["selection_text"], colors["attention"], bold=True), trailing))
    else:
        output.extend((base, trailing))
    output.extend((base, "\x1b[K\x1b[0m"))
    return "".join(output)


def popup_options(session: Mapping[str, Any]) -> list[str]:
    if native_border_styles(session)["pane-border-style"] == "default":
        return ["-s", "default", "-S", "default"]
    colors = palette(session)
    return ["-s", f"fg={colors['text']},bg={colors['toolbar']}",
            "-S", f"fg={colors['selected']},bg={colors['toolbar']}"]


def menu_options(session: Mapping[str, Any]) -> list[str]:
    if native_border_styles(session)["pane-border-style"] == "default":
        return [*popup_options(session), "-H", "default"]
    colors = palette(session)
    return [*popup_options(session), "-H",
            f"fg={colors['selection_text']},bg={colors['selected']},bold"]



def native_border_styles(session: Mapping[str, Any]) -> dict[str, str]:
    """Canonical native chrome styles, shared by writes and reconciliation."""
    grouped = (session.get("layout_mode") == "canvas-v3"
               and session.get("native_layout", {}).get("grouped_sections"))
    if grouped and not session.get("runtime_settings", {}).get("colors_enabled", True):
        return {"pane-border-style": "default", "pane-active-border-style": "default"}
    colors = palette(session)
    return {"pane-border-style": f"fg={colors['text']},bg={colors['toolbar']}",
            "pane-active-border-style": f"fg={colors['selected']},bg={colors['toolbar']}"}
