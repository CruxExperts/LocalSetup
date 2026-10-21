from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ls.core.tmux_terminal_mode.constants import AGENT_RULE_BLOCK, SENTINEL_BEGIN
from ls.core.tmux_terminal_mode.layers import load_json_settings

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ls/tools/tmux_terminal_mode.py"


def _fake_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tmux.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return tmux


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_shell_enable_is_idempotent_and_disable_strips_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tmux(tmp_path, monkeypatch)
    rc_path = tmp_path / ".bashrc"
    rules_path = tmp_path / ".cursor/rules/operator-rules.mdc"

    first = _run(
        "enable",
        "--mode",
        "shell",
        "--shell-rc",
        str(rc_path),
        "--rules-file",
        str(rules_path),
    )
    assert first.returncode == 0, first.stderr

    disabled = _run(
        "disable",
        "--mode",
        "shell",
        "--shell-rc",
        str(rc_path),
        "--rules-file",
        str(rules_path),
    )
    assert disabled.returncode == 0, disabled.stderr
    assert SENTINEL_BEGIN not in rc_path.read_text(encoding="utf-8")
    assert SENTINEL_BEGIN not in rules_path.read_text(encoding="utf-8")

    rc_path.unlink()
    rules_path.unlink()

    first_again = _run(
        "enable",
        "--mode",
        "shell",
        "--shell-rc",
        str(rc_path),
        "--rules-file",
        str(rules_path),
    )
    assert first_again.returncode == 0, first_again.stderr

    second = _run(
        "enable",
        "--mode",
        "shell",
        "--shell-rc",
        str(rc_path),
        "--rules-file",
        str(rules_path),
    )
    assert second.returncode == 0, second.stderr
    assert rc_path.read_text(encoding="utf-8").count(SENTINEL_BEGIN) == 1
    assert rules_path.read_text(encoding="utf-8").count(SENTINEL_BEGIN) == 1


def test_ide_enable_writes_tmux_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmux = _fake_tmux(tmp_path, monkeypatch)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}\n", encoding="utf-8")
    rules_path = tmp_path / ".cursor/rules/operator-rules.mdc"

    result = _run(
        "enable",
        "--mode",
        "ide",
        "--settings-file",
        str(settings_path),
        "--rules-file",
        str(rules_path),
        "--session",
        "ops-test",
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    profile = data["terminal.integrated.profiles.linux"]["tmux-session"]
    assert profile["path"] == str(tmux)
    assert profile["args"] == ["new-session", "-A", "-s", "ops-test"]
    assert data["terminal.integrated.defaultProfile.linux"] == "tmux-session"


def test_invalid_settings_json_uses_error_boundary(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_json_settings(settings_path)

    assert exc_info.value.code == 1


def test_direct_wrapper_help_and_version() -> None:
    help_result = _run("--help")
    assert help_result.returncode == 0
    assert "tmux-default terminal mode" in help_result.stdout

    version_result = _run("--version")
    assert version_result.returncode == 0
    assert "tmux_terminal_mode 1.0.0" in version_result.stdout


def test_status_json_schema_is_read_only(tmp_path: Path) -> None:
    settings_path = tmp_path / "missing-settings.json"
    rc_path = tmp_path / "missing-bashrc"
    rules_path = tmp_path / ".cursor/rules/operator-rules.mdc"

    result = _run(
        "status",
        "--json",
        "--settings-file",
        str(settings_path),
        "--shell-rc",
        str(rc_path),
        "--rules-file",
        str(rules_path),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "none"
    assert payload["session"] is None
    assert payload["layers"]["ide"] == {
        "active": False,
        "session": None,
        "settings_path": str(settings_path),
    }
    assert payload["layers"]["shell"] == {
        "active": False,
        "session": None,
        "rc_path": str(rc_path),
    }
    assert payload["layers"]["rules"] == {
        "active": False,
        "current": False,
        "rules_path": str(rules_path),
    }
    assert "present" in payload["layers"]["tmux_ops"]
    assert not settings_path.exists()
    assert not rc_path.exists()
    assert not rules_path.exists()


def test_injected_rule_names_both_workflows_and_stop_contract() -> None:
    normalized_rules = " ".join(AGENT_RULE_BLOCK.split())
    assert "ls-wf-sudo-tmux-session" in AGENT_RULE_BLOCK
    assert "ls-wf-tmux-terminal-mode" in AGENT_RULE_BLOCK
    assert '"action_required": true' in AGENT_RULE_BLOCK
    assert "`sudo -v` in that exact tmux pane" in AGENT_RULE_BLOCK
    assert "Wait for that reply before probing again" in normalized_rules
    assert "<hostname.upper()>_SUDO_KEY" in AGENT_RULE_BLOCK
    assert "GENERIC_SUDO_KEY" in AGENT_RULE_BLOCK
    assert "credential_variable" in AGENT_RULE_BLOCK
    assert 'transcribed request for "pseudo" as a sudo' in AGENT_RULE_BLOCK
    assert "managed `run` and `status` results carry the same secret-free provenance" in normalized_rules.lower()
    assert "must not advance or replay credentials" in AGENT_RULE_BLOCK
