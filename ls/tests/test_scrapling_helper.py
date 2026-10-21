"""Tests for the local Scrapling helper contracts without live engine execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ls.tools.scrapling_helper import main as scrapling_main


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "ls" / "skills" / "ls-scrapling-url-extraction"
VERIFIER = SKILL / "scripts" / "verify_scrapling_capabilities.py"
PACKAGED_CAPABILITIES = ROOT / "ls" / "tools" / "scrapling_helper" / "scrapling_capabilities.json"


def _offline_status() -> scrapling_main.ScraplingStatus:
    return scrapling_main.ScraplingStatus(
        env_type="missing",
        scrapling_available=False,
        version=None,
        healthy=False,
        docker_available=False,
        details="offline test",
    )


def _run_verifier(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), *args],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_scrapling_skill_smoke_command_validates_capability_index() -> None:
    smoke = yaml.safe_load((ROOT / "ls" / "tests" / "skill_smoke_commands.yaml").read_text(encoding="utf-8"))
    assert smoke["ls-scrapling-url-extraction"] == {
        "cwd": "repo-root",
        "command": "python3 ls/skills/ls-scrapling-url-extraction/scripts/verify_scrapling_capabilities.py --json",
    }

    result = _run_verifier("--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert "scrapling_status" in payload["checked"]
    assert "extract_url_structured" not in payload["checked"]
    assert "run_spider" not in payload["checked"]


def test_scrapling_capability_verifier_checks_packaged_index_from_skill_directory() -> None:
    result = _run_verifier("--json", cwd=SKILL)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert "extract_url_simple" in payload["checked"]


@pytest.mark.parametrize(
    ("expected_error", "removed_name", "replacement_cli"),
    [
        ("extract_url_structured", "extract_url_structured", None),
        ("run_spider", "run_spider", None),
        ("scrapling spider", None, "scrapling spider example"),
    ],
)
def test_capability_verifier_rejects_retired_contracts(
    tmp_path: Path,
    expected_error: str,
    removed_name: str | None,
    replacement_cli: str | None,
) -> None:
    data = json.loads(PACKAGED_CAPABILITIES.read_text(encoding="utf-8"))
    if removed_name:
        data[removed_name] = data["extract_url_simple"].copy()
    else:
        data["extract_url_simple"]["cli"] = replacement_cli

    capabilities_path = tmp_path / "capabilities.json"
    capabilities_path.write_text(json.dumps(data), encoding="utf-8")
    result = _run_verifier("--capabilities", str(capabilities_path), "--json")

    assert result.returncode == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert any(expected_error in error for error in payload["errors"])


def test_capability_verifier_fails_closed_for_unreadable_override(tmp_path: Path) -> None:
    result = _run_verifier("--capabilities", str(tmp_path), "--json")

    assert result.returncode == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert len(payload["errors"]) == 1
    assert "could not read capability index" in payload["errors"][0]
    assert str(tmp_path) in payload["errors"][0]


def test_show_status_serializes_injected_offline_status(monkeypatch) -> None:
    monkeypatch.setattr(scrapling_main, "scrapling_status", _offline_status)

    payload = json.loads(scrapling_main.show_status())

    assert payload["env_type"] == "missing"
    assert payload["scrapling_available"] is False
    assert payload["docker_available"] is False


def test_get_scrapling_version_does_not_fall_back_to_docker(monkeypatch) -> None:
    cfg = scrapling_main.load_config()
    missing_host = scrapling_main.HostEnvStatus(
        env_type="missing",
        scrapling_available=False,
        version=None,
        details="offline test",
    )

    monkeypatch.setattr(scrapling_main, "load_config", lambda: cfg)
    monkeypatch.setattr(scrapling_main, "detect_host_env", lambda _cfg: missing_host)
    monkeypatch.setattr(
        scrapling_main,
        "detect_docker",
        lambda: pytest.fail("host-only health must not query Docker"),
    )
    monkeypatch.setattr(
        scrapling_main,
        "apply_command_plan",
        lambda _plan: pytest.fail("missing host CLI must not run a Docker image"),
    )

    assert scrapling_main.get_scrapling_version() is None


def test_ensure_available_dry_run_does_not_apply_or_probe(monkeypatch) -> None:
    monkeypatch.setattr(scrapling_main, "scrapling_status", _offline_status)
    monkeypatch.setattr(scrapling_main.shutil, "which", lambda _binary: None)

    result = scrapling_main.ensure_available(dry_run=True, auto_confirm=False)

    assert result.applied is False
    assert result.command_result is None
    assert result.pipx_bootstrap_plans


def test_extract_url_simple_command_construction_and_mode_validation(tmp_path: Path, monkeypatch) -> None:
    called: dict[str, list[list[str]]] = {"plans": []}

    def fake_apply(plan: list[str]) -> dict:
        called["plans"].append(plan)
        return {"command": " ".join(plan), "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(scrapling_main, "apply_command_plan", fake_apply)
    out = tmp_path / "out.md"
    result = scrapling_main.extract_url_simple(
        "https://example.com",
        out,
        selector=None,
        mode_hint="get",
        use_docker=False,
    )

    assert "scrapling extract get https://example.com" in result["command"]
    assert result["output_path"] == str(out)
    assert [attempt["mode"] for attempt in result["attempts"]] == ["get"]

    invalid_out = tmp_path / "not-created" / "out.md"
    with pytest.raises(ValueError, match="unsupported Scrapling extraction mode"):
        scrapling_main.extract_url_simple(
            "https://example.com",
            invalid_out,
            mode_hint="dynamic",
        )
    assert called["plans"] == [["scrapling", "extract", "get", "https://example.com", str(out)]]
    assert not invalid_out.parent.exists()


def test_extract_url_simple_adaptive_escalates_on_failure(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_apply(plan: list[str]) -> dict:
        calls.append(plan)
        if len(calls) == 1:
            return {"command": " ".join(plan), "returncode": 1, "stdout": "", "stderr": "network error"}
        return {"command": " ".join(plan), "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(scrapling_main, "apply_command_plan", fake_apply)
    out = tmp_path / "out.md"
    result = scrapling_main.extract_url_simple(
        "https://example.com",
        out,
        selector=None,
        mode_hint=None,
        use_docker=False,
    )

    assert result["returncode"] == 0
    assert result["mode"] == "fetch"
    assert [attempt["mode"] for attempt in result["attempts"]] == ["get", "fetch"]


def test_scrapling_self_test_offline_uses_injected_status_and_fixture(tmp_path: Path, monkeypatch) -> None:
    cfg = scrapling_main.load_config()
    monkeypatch.setattr(cfg, "outputs_root", tmp_path)

    def fake_extract(url, output_path, selector=None, mode_hint=None, use_docker=False):
        return {
            "command": f"scrapling extract get {url} {output_path}",
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "mode": mode_hint or "get",
            "output_path": str(output_path),
            "attempts": [],
        }

    ensure_result = scrapling_main.EnsureResult(
        status=_offline_status(),
        plan=["pipx", "install", "scrapling[all]"],
        applied=False,
        command_result=None,
        pipx_bootstrap_plans=None,
    )
    monkeypatch.setattr(scrapling_main, "load_config", lambda: cfg)
    monkeypatch.setattr(scrapling_main, "scrapling_status", _offline_status)
    monkeypatch.setattr(scrapling_main, "ensure_available", lambda **_kwargs: ensure_result)
    monkeypatch.setattr(scrapling_main, "extract_url_simple", fake_extract)

    summary = scrapling_main.scrapling_self_test(mode="offline")

    assert summary["self_test_mode"] == "offline"
    status_path = Path(summary["status_path"])
    assert status_path.exists()


def test_docker_extraction_uses_test_owned_fake_from_root_safe_handoff(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_log = tmp_path / "fake-docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        r"""#!/usr/bin/env bash
set -euo pipefail
host_dir="${6%:/workspace}"
container_output="${11#/workspace/}"
case "${11}" in
    /workspace/*) ;;
    *) exit 64 ;;
esac
: > "$host_dir/$container_output"
printf '%s\n' "$@" >> "$FAKE_DOCKER_LOG"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    output_path = tmp_path / "handoff-output.md"
    inner_python = "\n".join(
        [
            "import json",
            "import os",
            "from pathlib import Path",
            "from ls.tools.scrapling_helper import main",
            "result = main.extract_url_simple(",
            "    'https://example.test',",
            "    Path(os.environ['SCRAPLING_TEST_OUTPUT']),",
            "    mode_hint='get',",
            "    use_docker=True,",
            ")",
            "print(json.dumps({'returncode': result['returncode'], 'cwd': os.getcwd()}))",
        ],
    )
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(fake_log)
    env["HOME"] = str(tmp_path / "home")
    env["SCRAPLING_TEST_OUTPUT"] = str(output_path)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'cd -- "$1" && exec "$2" -c "$3"',
            "bash",
            str(ROOT),
            sys.executable,
            inner_python,
        ],
        cwd=outside_cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {"returncode": 0, "cwd": str(ROOT)}
    assert output_path.is_file()
    argv = fake_log.read_text(encoding="utf-8").splitlines()
    assert argv == [
        "run",
        "--rm",
        "-w",
        "/workspace",
        "-v",
        f"{tmp_path.resolve()}:/workspace",
        "pyd4vinci/scrapling:latest",
        "extract",
        "get",
        "https://example.test",
        "/workspace/handoff-output.md",
    ]
def test_actual_tmux_ops_run_help_is_available_from_unrelated_directory(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("REMOTE_TMUX_HOST", None)
    env.pop("REMOTE_TMUX_CWD", None)
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()

    result = subprocess.run(
        [str(ROOT / "ls" / "tools" / "tmux_ops"), "run", "--help"],
        cwd=outside_cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "usage:" in result.stdout
    assert "--target" in result.stdout
    assert "--timeout" in result.stdout
    assert "--tail" in result.stdout
