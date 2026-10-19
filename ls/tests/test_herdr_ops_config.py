from __future__ import annotations

import tomllib

import pytest

from ls.core.herdr_ops.config import merge_config_text, translate_config


def test_translate_config_copies_portable_values_and_reports_skips_and_conflicts() -> None:
    master = {
        "theme": {"name": "midnight", "auto_switch": True, "custom": {"private": True}},
        "ui": {"confirm_close": True, "mouse_scroll_lines": 14, "remote_only": "master-private"},
        "session": {"resume_agents_on_restore": False, "restore_tabs": True},
        "terminal": {"new_cwd": "/Users/example/project", "shell_mode": "login"},
        "keybindings": {"open": "ctrl+p"},
        "integrations": {"editor": "private"},
        "secret_store": {"token": "private"},
    }
    remote = {
        "theme": {"name": "remote-theme", "auto_switch": False, "dark_name": "remote-dark"},
        "ui": {"confirm_close": False, "remote_only": "retained"},
    }

    result = translate_config(master, remote)

    assert result["settings"] == {
        "theme": {"name": "midnight", "auto_switch": True},
        "ui": {"confirm_close": True, "mouse_scroll_lines": 14},
        "session": {"resume_agents_on_restore": False},
        "terminal": {"shell_mode": "login"},
    }
    assert {item["key"] for item in result["conflicts"]} == {
        "theme.name",
        "theme.auto_switch",
        "ui.confirm_close",
    }
    assert all(item["reason"] == "master-value-applied-over-remote" for item in result["conflicts"])
    assert {item["key"] for item in result["skipped"]} >= {
        "theme.custom",
        "ui.remote_only",
        "terminal.new_cwd",
        "keybindings",
        "integrations",
        "secret_store",
        "session.restore_tabs",
    }


def test_translate_config_rejects_arbitrary_shell_mode_and_nonportable_ui_values() -> None:
    result = translate_config(
        {
            "ui": {"confirm_close": True, "callback": ["execute", "something"]},
            "terminal": {"shell_mode": "sh -c arbitrary-command"},
        }
    )

    assert result["settings"] == {"ui": {"confirm_close": True}}
    assert {item["key"] for item in result["skipped"]} == {
        "ui.callback",
        "terminal.shell_mode",
    }


def test_merge_config_text_preserves_remote_unknown_values_and_applies_overlay() -> None:
    existing = """theme = \"remote\"\ncustom = [1, 2]\n\n[ui]\nremote_only = true\n"""
    merged = merge_config_text(
        existing,
        {"session": {"resume_agents_on_restore": True}, "ui": {"compact": False}},
    )
    parsed = tomllib.loads(merged)

    assert parsed == {
        "theme": "remote",
        "custom": [1, 2],
        "ui": {"remote_only": True, "compact": False},
        "session": {"resume_agents_on_restore": True},
    }


def test_merge_config_text_rejects_invalid_or_shape_conflicting_input() -> None:
    with pytest.raises(ValueError, match="invalid TOML"):
        merge_config_text("invalid = [", {})
    merged = merge_config_text("theme = \"remote\"\n\n[ui]\nremote_only = true\nconfirm_close = false\n", {
        "theme": {"name": "midnight"},
        "ui": {"confirm_close": True},
    })
    assert tomllib.loads(merged) == {
        "theme": {"name": "midnight"},
        "ui": {"remote_only": True, "confirm_close": True},
    }
    assert tomllib.loads(merge_config_text("ui = false\n", {"ui": {"confirm_close": True}})) == {
        "ui": {"confirm_close": True}
    }


def test_merge_config_text_round_trips_remote_array_tables() -> None:
    merged = merge_config_text(
        "[[profiles]]\nname = \"one\"\n\n[[profiles]]\nname = \"two\"\n\n[custom]\n\"key.with.dots\" = 3\n",
        {"session": {"resume_agents_on_restore": True}},
    )

    assert tomllib.loads(merged) == {
        "profiles": [{"name": "one"}, {"name": "two"}],
        "custom": {"key.with.dots": 3},
        "session": {"resume_agents_on_restore": True},
    }
