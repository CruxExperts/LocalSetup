"""Readable controls keep the same terminal cells as their mouse targets."""

import re

import pytest

from ls.core.terminal_ops.command_service import _native_radio_row
from ls.core.terminal_ops.command_theme import ansi, footer, palette


def _luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
              for value in channels]
    return sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))


def _contrast(foreground, background):
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + .05) / (dark + .05)


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


@pytest.mark.parametrize("columns", [80, 120, 500])
def test_radio_controls_have_separated_labels_and_aligned_hitboxes(columns):
    session = {"native_layout": {"mode": "follow", "columns": 120, "rows": 24,
               "desired_generation": 2, "applied_generation": 1,
               "page": 0, "pending": False, "weights": {}}}
    text, targets = _native_radio_row(session, columns=columns)
    expected = ["(*)follow", "( )stable", "( )fixed", "<", ">",
                "[panes]", "[layout]", "[logs]", "[?]"]

    assert len(targets) == len(expected)
    previous_end = None
    for (left, right, _action, _value), label in zip(targets, expected):
        assert text[left:right] == label
        if previous_end is not None:
            assert left == previous_end + 1
            assert text[previous_end:left] == " "
        previous_end = right
    assert len(text) < columns


def test_existing_session_gets_readable_additive_palette_roles():
    colors = palette({"runtime_settings": {"theme": {"text": "#F4F7FF"}}})
    for foreground, background in (("text", "toolbar"), ("control_text", "control"),
                                  ("selection_text", "selected"), ("selection_text", "attention")):
        assert _contrast(colors[foreground], colors[background]) >= 7


def test_default_toolbar_palette_uses_distinct_blue_surfaces_and_saved_colors_win():
    colors = palette({})
    assert _contrast(colors["text"], colors["toolbar"]) >= 7
    assert _contrast(colors["control_text"], colors["control"]) >= 7
    assert _contrast(colors["selection_text"], colors["selected"]) >= 7
    assert _contrast(colors["selection_text"], colors["attention"]) >= 7
    assert _luminance(colors["control"]) > _luminance(colors["toolbar"]) + .45
    red, green, blue = [int(colors["control"][i:i + 2], 16) for i in (1, 3, 5)]
    assert blue > green > red and green > 150
    red, green, blue = [int(colors["selected"][i:i + 2], 16) for i in (1, 3, 5)]
    assert green > red and blue > red
    red, green, blue = [int(colors["attention"][i:i + 2], 16) for i in (1, 3, 5)]
    assert red > green > blue

    custom = {"text": "#F4F7FF", "toolbar": "#182A3A", "control": "#26394C",
              "control_text": "#C8D5E3", "selected": "#A1E8F5",
              "selection_text": "#102030", "attention": "#FFD27A"}
    saved = palette({"runtime_settings": {"theme": custom}})
    assert {key: saved[key] for key in custom} == custom


def test_legacy_control_palette_derives_ink_from_saved_colors():
    dark_control = palette({"runtime_settings": {"theme": {
        "text": "#F4F7FF", "selection_text": "#082438", "control": "#2A5B82",
    }}})
    pale_control = palette({"runtime_settings": {"theme": {
        "text": "#FFFFFF", "selection_text": "#06233A", "control": "#B9E2FF",
    }}})

    assert dark_control["control_text"] == dark_control["text"]
    assert pale_control["control_text"] == pale_control["selection_text"]
    assert _contrast(dark_control["control_text"], dark_control["control"]) >= 4.5
    assert _contrast(pale_control["control_text"], pale_control["control"]) >= 7


def test_explicit_redraw_repaints_unchanged_control_row(tmp_path, monkeypatch):
    import io
    from ls.core.terminal_ops.command_service import ShellCommandService, _fresh_native_layout
    from ls.core.terminal_ops.command_state import atomic_json, read_json

    path = tmp_path / "session.json"
    atomic_json(path, {"state": "ready", "layout_mode": "canvas-v3", "handle": "redraw-test",
                       "session_record_path": str(path), "native_layout": _fresh_native_layout({"layout": {}})})
    service = ShellCommandService(tmp_path / "state", backend=object())
    monkeypatch.setattr(service, "_native_canvas_operations", lambda *_: [])
    monkeypatch.setattr(service, "_apply_native_layout_locked", lambda *_args, **_kwargs: {"state": "applied"})
    output = io.StringIO()
    monkeypatch.setattr("ls.core.terminal_ops.command_service.sys.stdout", output)
    ticks = []

    def advance(_seconds):
        ticks.append(True)
        current = read_json(path)
        if len(ticks) == 1:
            current["ui_redraw_generation"] = 1
        elif len(ticks) == 3:
            current["state"] = "closed"
        atomic_json(path, current)

    monkeypatch.setattr("ls.core.terminal_ops.command_service.time.sleep", advance)
    assert service.run_native_control_loop(path) == 0
    assert output.getvalue().count("\x1b[999;1H") == 2


@pytest.mark.parametrize("failure", ["native_ack", "record_write"])
def test_redraw_failure_preserves_applied_action_without_replay(tmp_path, monkeypatch, failure):
    from ls.core.terminal_ops.command_service import ShellCommandService
    from ls.core.terminal_ops.command_state import ShellCommandError

    service = ShellCommandService(tmp_path / "state", backend=object())
    actions = []

    def apply(*_args, **_kwargs):
        actions.append(True)
        return {"ok": True, "state": "applied", "effect_certainty": "known"}

    def redraw(*_args):
        if failure == "native_ack":
            raise ShellCommandError("lost_ack", "Lost repaint acknowledgement", effect="unknown")
        raise OSError("Redraw request write failed")

    monkeypatch.setattr(service, "_native_control_event", apply)
    monkeypatch.setattr(service, "_redraw_native_control", redraw)
    result = service.native_control_event(tmp_path / "session.json", 123, "mode", operation_id="follow")
    assert len(actions) == 1
    assert result["ok"] and result["state"] == "applied" and result["effect_certainty"] == "known"
    assert result["redraw"] == "unknown"
