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
    # Existing live sessions retain their profile. New additive control roles
    # use the canonical defaults until that session applies a newer profile.
    return _defaults() | dict(session.get("runtime_settings", {}).get("theme", {}))


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
        output.extend((ansi(colors["selection_text"] if selected else colors["text"],
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
