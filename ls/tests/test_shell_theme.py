"""Readable controls keep the same terminal cells as their mouse targets."""

import re

import pytest

from ls.core.terminal_ops.command_service import _native_radio_row
from ls.core.terminal_ops.command_theme import ansi, footer, palette


@pytest.mark.parametrize("mode", ["follow", "stable", "fixed"])
@pytest.mark.parametrize("columns", [40, 80, 95, 120, 500])
def test_colored_footer_preserves_click_targets_and_width(mode, columns):
    session = {"native_layout": {"mode": mode, "columns": 120, "rows": 24,
               "desired_generation": 2, "applied_generation": 1,
               "page": 0, "pending": True, "weights": {}}}
    text, targets = _native_radio_row(session, columns=columns)
    rendered = footer(session, text, targets)
    visible = re.sub(r"\x1b\[[0-9;]*[mK]", "", rendered)
    assert len(visible) == len(text) < columns
    assert "\x1b[2m" not in rendered
    colors = palette(session)
    for left, right, action, value in targets:
        if action == "mode":
            assert visible[left:right] == ("●" if value == mode else "○") + " " + value + " "
    assert ansi(colors["selection_text"], colors["selected"], bold=True) in rendered
    assert rendered.endswith(ansi(colors["text"], colors["toolbar"]) + "\x1b[K\x1b[0m")


def test_existing_session_gets_readable_additive_palette_roles():
    colors = palette({"runtime_settings": {"theme": {"text": "#F4F7FF"}}})
    for foreground, background in (("text", "toolbar"), ("text", "control"),
                                  ("selection_text", "selected"), ("selection_text", "attention")):
        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
            return sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))
        light, dark = sorted((luminance(colors[foreground]), luminance(colors[background])), reverse=True)
        assert (light + .05) / (dark + .05) >= 4.5
