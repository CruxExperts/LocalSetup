"""Explicit, readable surfaces for owned native controls and popups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..application_profiles.service import load_tmux_runtime_defaults


@lru_cache(maxsize=1)
def _defaults() -> dict[str, str]:
    return load_tmux_runtime_defaults(source_root=Path(__file__).resolve().parents[3],
                                     name="agent-shell")["settings"]["theme"]


def palette(session: Mapping[str, Any]) -> dict[str, str]:
    """Fill canonical roles, preserving saved colors and adapting legacy control ink."""
    defaults = _defaults()
    saved = dict(session.get("runtime_settings", {}).get("theme", {}))
    colors = defaults | saved
    if "control_text" not in saved:
        colors["control_text"] = _best_control_ink(colors)
    return colors


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
           targets: Sequence[tuple[int, int, str, str | None]]) -> str:
    """Style existing cell positions; mouse targets remain identical."""
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
    colors = palette(session)
    return ["-s", f"fg={colors['text']},bg={colors['toolbar']}",
            "-S", f"fg={colors['selected']},bg={colors['toolbar']}"]


def menu_options(session: Mapping[str, Any]) -> list[str]:
    colors = palette(session)
    return [*popup_options(session), "-H",
            f"fg={colors['selection_text']},bg={colors['selected']},bold"]
