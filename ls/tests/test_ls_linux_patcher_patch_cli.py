from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "ls-linux-patch-planning" / "scripts" / "patch_cli.py"


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def guarded_path(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ssh-invoked"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        '#!/bin/sh\nprintf "%s\\n" invoked > "$SSH_GUARD_MARKER"\nexit 99\n',
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["SSH_GUARD_MARKER"] = str(marker)
    return env, marker


def test_host_only_outputs_plan_without_executing_remote_command(tmp_path: Path) -> None:
    env, marker = guarded_path(tmp_path)
    result = run_cli("host-only", "admin@example.com", env=env)
    assert result.returncode == 0
    assert "Mode: plan-only" in result.stdout
    assert "ssh admin@example.com" in result.stdout
    assert "**preflight**" in result.stdout
    assert "```bash" in result.stdout
    assert "sudo -n -l --" in result.stdout
    assert "sudo -n " + "true" not in result.stdout
    assert not marker.exists()


def test_rejects_shell_operator_in_host() -> None:
    result = run_cli("host-only", "admin@example.com;rm")
    assert result.returncode == 2
    assert "InputError" in result.stderr
    assert "host:" in result.stderr


def test_host_full_requires_absolute_docker_path() -> None:
    result = run_cli("host-full", "admin@example.com", "relative/path")
    assert result.returncode == 2
    assert "docker_path" in result.stderr
    assert "absolute path" in result.stderr


def test_multiple_config_accepts_simple_host_rows(tmp_path: Path) -> None:
    config = tmp_path / "hosts.conf"
    config.write_text("# comment\nadmin@example.com\nroot@example.net,/opt/docker\n", encoding="utf-8")
    env, marker = guarded_path(tmp_path)
    result = run_cli("--json", "multiple", str(config), env=env)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "plan-only"
    assert len(payload["steps"]) == 9
    assert [step["phase"] for step in payload["steps"]].count("preflight") == 2
    assert all(
        "sudo -n -l --" in step["command"]
        for step in payload["steps"]
        if step["phase"] == "preflight"
    )
    assert any("admin@example.com" in step["command"] for step in payload["steps"])
    assert any("/opt/docker" in step["command"] for step in payload["steps"])
    assert not marker.exists()


def test_auto_mode_is_guidance_only() -> None:
    result = run_cli("auto", "--skip-docker")
    assert result.returncode == 0
    assert "guidance-only" in result.stdout
    assert "host-only" in result.stdout
