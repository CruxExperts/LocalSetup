from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
import json
import hashlib
import os
import gzip
import shlex
import subprocess
import tomllib

import pytest

from ls.core.herdr_ops import remote
from ls.core.herdr_ops.cli import build_parser
from ls.core.herdr_ops.errors import HerdrOpsError


def _local_ssh_transport(monkeypatch, *, environment=None):
    """Exercise ssh_run's real argv checks and quoting, then run its command locally."""
    run = subprocess.run
    commands = []
    monkeypatch.setattr(remote, "_ssh_binary", lambda: "/usr/bin/ssh")

    def local_transport(argv, **kwargs):
        if argv == ["/usr/bin/ssh", "-G", "fixture"]:
            return subprocess.CompletedProcess(argv, 0, b"hostname fixture\nstricthostkeychecking yes\n", b"")
        assert argv[0] == "/usr/bin/ssh" and argv[-2] == "fixture"
        assert "StrictHostKeyChecking=yes" in argv
        assert "BatchMode=yes" in argv
        commands.append((argv[-1], kwargs.get("input")))
        options = {"capture_output": True, "check": False, "timeout": kwargs["timeout"], "env": environment}
        if "input" in kwargs:
            options["input"] = kwargs["input"]
        else:
            assert kwargs["stdin"] == subprocess.DEVNULL
            options["stdin"] = subprocess.DEVNULL
        return run(shlex.split(argv[-1]), **options)

    monkeypatch.setattr(remote.subprocess, "run", local_transport)
    return commands


def test_ssh_run_preserves_alias_and_quotes_remote_argv_without_shell_mode(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(remote, "_qualify_ssh_target", lambda *_args, **_kwargs: {
        "hostname": "resolved.example", "stricthostkeychecking": "ask",
        "proxyjump": "bastion", "controlmaster": "auto",
    })
    monkeypatch.setattr(remote, "_ssh_binary", lambda: "/usr/bin/ssh")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"receipt\n", b"")

    monkeypatch.setattr(remote.subprocess, "run", fake_run)
    result = remote.ssh_run("work", ["cat", "--", "/tmp/a; touch /tmp/no"], stdin=b"input")

    assert result.stdout == b"receipt\n"
    argv, kwargs = calls[0]
    assert argv[0:2] == ["/usr/bin/ssh", "-o"]
    assert "StrictHostKeyChecking=yes" in argv
    assert "BatchMode=yes" in argv
    assert argv[-2] == "work"
    assert argv[-1] == shlex.join(["cat", "--", "/tmp/a; touch /tmp/no"])
    assert kwargs["input"] == b"input"
    assert "shell" not in kwargs


def test_ssh_run_closes_local_stdin_when_no_payload(monkeypatch) -> None:
    monkeypatch.setattr(remote, "_qualify_ssh_target", lambda *_args, **_kwargs: {
        "hostname": "target", "stricthostkeychecking": "yes",
    })
    monkeypatch.setattr(remote, "_ssh_binary", lambda: "/usr/bin/ssh")
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(remote.subprocess, "run", fake_run)

    remote.ssh_run("target", ["true"])

    assert seen["stdin"] == subprocess.DEVNULL
    assert "input" not in seen


@pytest.mark.parametrize("target", ["", "-oProxyCommand=bad", "host name", "host\nname"])
def test_ssh_target_rejects_options_whitespace_and_controls(target: str) -> None:
    with pytest.raises(HerdrOpsError):
        remote.validate_ssh_target(target)


@pytest.mark.parametrize("argv", [["sh", "-c", "echo one\necho two"], ["sh", "-c", ""], ["sh", "-c", "echo\x00bad"]])
def test_remote_argv_rejects_empty_or_control_bearing_arguments(argv):
    with pytest.raises(HerdrOpsError) as caught:
        remote.validate_remote_argv(argv)
    assert caught.value.code == "invalid_remote_command"


def test_ssh_run_fails_closed_when_config_disables_strict_host_keys(monkeypatch) -> None:
    monkeypatch.setattr(remote, "_qualify_ssh_target", lambda *_args, **_kwargs: {
        "hostname": "target", "stricthostkeychecking": "no",
    })
    with pytest.raises(HerdrOpsError, match="disables strict host-key"):
        remote.ssh_run("target", ["true"])


def test_profiles_accept_supported_envelopes_and_emit_only_public_fields(monkeypatch) -> None:
    output = {
        "profiles": [
            {
                "id": "machine-1",
                "label": "box",
                "ssh_target": "box-alias",
                "remote_session": "work",
                "enabled": True,
                "identity_file": "/private/id",
                "password": "must-not-escape",
            },
            {"id": "broken", "label": "missing fields"},
        ]
    }
    monkeypatch.setattr(
        remote,
        "_local_herdr",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, json.dumps(output).encode(), b""),
    )

    profiles = remote.list_profiles()

    assert profiles == [{
        "id": "machine-1",
        "label": "box",
        "ssh_target": "box-alias",
        "remote_session": "work",
        "enabled": True,
    }]
    assert "password" not in json.dumps(profiles)


def test_find_profile_requires_one_enabled_exact_id_or_label() -> None:
    profiles = [
        {"id": "id-1", "label": "box", "enabled": True},
        {"id": "id-2", "label": "box", "enabled": True},
    ]
    with pytest.raises(HerdrOpsError, match="exactly one"):
        remote.find_profile("box", profiles)
    with pytest.raises(HerdrOpsError, match="disabled"):
        remote.find_profile("id-3", [{"id": "id-3", "label": "offline", "enabled": False}])


def test_connect_returns_native_attach_argv_without_launching_a_tui(monkeypatch) -> None:
    profile = {"id": "machine-1", "label": "box", "ssh_target": "ssh-alias", "remote_session": "work", "enabled": True}
    monkeypatch.setattr(remote, "find_profile", lambda _selector: profile)
    monkeypatch.setattr(remote, "machine_snapshot", lambda _selector: {
        "type": "session_snapshot",
        "snapshot": {"version": "0.9.5", "protocol": 22, "panes": [{"pane_id": "p"}], "agents": []},
    })

    result = remote.inspect_profile("box", attach=True)

    assert result["connectivity"] == "connected"
    assert result["attach_argv"] == ["herdr", "--remote", "ssh-alias", "--session", "work"]


def test_occupancy_uses_agent_status_and_process_info_and_fails_closed(monkeypatch) -> None:
    def fake_request(_endpoint, method, params=None, **_kwargs):
        if method == "session.snapshot":
            return {"snapshot": {
                "panes": [{"pane_id": "w:p1"}, {"pane_id": "w:p2"}],
                "agents": [{"pane_id": "w:p1", "agent_status": "working"}],
            }}
        if params["pane_id"] == "w:p1":
            return {"process_info": {"shell_pid": 11, "foreground_processes": [{"pid": 11, "name": "bash"}]}}
        return {"process_info": {"foreground_processes": []}}

    monkeypatch.setattr(remote, "request", fake_request)
    result = remote._inspect_occupants("/tmp/herdr.sock")
    assert result[0] == {"pane_id": "w:p1", "state": "active", "reason": "agent-working"}
    assert result[1] == {"pane_id": "w:p2", "state": "unknown", "reason": "process-info-incomplete"}


def test_restart_requires_interruption_authority_before_any_stop(monkeypatch) -> None:
    profile = {"id": "machine-1", "label": "box", "ssh_target": "alias", "remote_session": "work", "enabled": True}
    monkeypatch.setattr(remote, "find_profile", lambda _selector: profile)
    monkeypatch.setattr(remote, "_session_socket", lambda *_args, **_kwargs: "/home/user/.config/herdr/sessions/work/herdr.sock")
    monkeypatch.setattr(remote, "_open_profile_endpoint", lambda *_args, **_kwargs: ("/tmp/local.sock", "a" * 32))
    monkeypatch.setattr(remote, "_ping", lambda *_args, **_kwargs: {"version": "0.9.5", "protocol": 22, "endpoint_generation": 1})
    monkeypatch.setattr(remote, "_inspect_occupants", lambda *_args: [
        {"pane_id": "w:p1", "state": "active", "reason": "foreground-process"}
    ])
    monkeypatch.setattr(remote, "close_tunnel", lambda *_args, **_kwargs: {"state": "closed"})
    stopped: list[bool] = []
    monkeypatch.setattr(remote, "_stop_named_server", lambda *_args, **_kwargs: stopped.append(True))

    result = remote.restart_profile("box", apply=True)

    assert result["state"] == "needs_interruption_authority"
    assert result["affected"][0]["pane_id"] == "w:p1"
    assert stopped == []


def test_config_apply_preserves_remote_only_secret_without_reporting_it(monkeypatch, tmp_path: Path) -> None:
    master = tmp_path / "master.toml"
    master.write_text('[theme]\nname = "midnight"\nauto_switch = true\n\n[ui]\nconfirm_close = true\n', encoding="utf-8")
    existing = (
        'machine_token = "remote-secret"\n'
        '[[machines]]\nlabel = "remote-profile"\nidentity_file = "/private/key"\n\n'
        '[theme]\nname = "remote-theme"\nauto_switch = false\ndark_name = "remote-dark"\n\n'
        '[ui]\nconfirm_close = false\nremote_only = "keep"\n'
    ).encode()
    calls: list[tuple[tuple[str, ...], bytes | None]] = []

    def fake_ssh(_target: str, argv, *, stdin=None, timeout=30):
        command = tuple(argv)
        calls.append((command, stdin))
        if command[0] == "sh":
            return subprocess.CompletedProcess([], 0, existing, b"")
        if command[0] == "bash" and "cat >" in command[2]:
            return subprocess.CompletedProcess([], 0, b"", b"")
        if command[0] == "bash":
            assert '"$binary" config check' in command[2]
            assert command[-1] == "/home/user/.local/bin/herdr"
            assert "gzip" in command[2] and "mv -f" in command[2]
            return subprocess.CompletedProcess([], 0, b"LSHERDR_CONFIG_APPLIED\n", b"")
        raise AssertionError(command)

    monkeypatch.setattr(remote, "ssh_run", fake_ssh)

    result = remote.apply_remote_config(
        "alias",
        master_config=master,
        remote_config="/home/user/.config/herdr/config.toml",
        binary_path="/home/user/.local/bin/herdr",
        apply=True,
    )

    staged = next(payload for command, payload in calls if command[0] == "bash" and payload is not None)
    assert staged is not None
    merged = tomllib.loads(staged.decode())
    assert merged["machine_token"] == "remote-secret"
    assert merged["machines"][0]["identity_file"] == "/private/key"
    assert merged["theme"] == {"name": "midnight", "auto_switch": True, "dark_name": "remote-dark"}
    assert merged["ui"] == {"confirm_close": True, "remote_only": "keep"}
    assert "remote-secret" not in json.dumps(result)
    assert "/private/key" not in json.dumps(result)
    assert result["native_validation"] == "passed"
    assert result["backup"].endswith(".gz")


def test_config_preview_does_not_start_ssh(monkeypatch, tmp_path: Path) -> None:
    master = tmp_path / "master.toml"
    master.write_text("[session]\nresume_agents_on_restore = true\n", encoding="utf-8")
    monkeypatch.setattr(remote, "ssh_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SSH should not run")))

    result = remote.apply_remote_config("alias", master_config=master, apply=False)

    assert result["needs_authorization"] is True
    assert result["settings"] == {"session": {"resume_agents_on_restore": True}}


def test_pinned_release_uses_supported_platform_asset_and_verified_source_pins(monkeypatch) -> None:
    manifest = remote.default_manifest_path(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(remote, "_remote_platform", lambda _target: "linux-x86_64")

    release = remote._pinned_release(manifest, "alias")

    assert release["version"] == "0.9.5"
    assert release["url"].startswith("https://github.com/herdrdev/herdr/releases/download/")
    assert len(release["sha256"]) == 64


@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", "linux-x86_64"), ("Linux", "arm64", "linux-aarch64"),
    ("Darwin", "x86_64", "macos-x86_64"), ("Darwin", "arm64", "macos-aarch64"),
])
def test_remote_platform_keys_match_every_pinned_asset(monkeypatch, system: str, machine: str, expected: str) -> None:
    monkeypatch.setattr(remote, "ssh_run", lambda *_args, **_kw: subprocess.CompletedProcess([], 0, f"{system}\n{machine}\n".encode(), b""))
    assert remote._remote_platform("fixture") == expected


def test_process_identity_requires_pid_start_boot_and_exact_argv() -> None:
    expected: Mapping[str, object] = {
        "pid": 55, "boot_id": "boot", "start_ticks": "123", "argv_sha256": "hash", "argv": ["ssh", "-N"],
    }
    assert remote._same_process_identity(expected, dict(expected))
    for field, value in (("pid", 56), ("boot_id", "other"), ("start_ticks", "999"), ("argv_sha256", "other"), ("argv", ["ssh", "different"])):
        changed = dict(expected)
        changed[field] = value
        assert not remote._same_process_identity(expected, changed)


def test_remote_parser_preserves_global_target_state_for_leaf_defaults(monkeypatch) -> None:
    args = build_parser().parse_args([
        "--dry-run", "--state-dir", "/tmp/assigned-state", "--session", "chosen",
        "remote", "deploy", "alias", "--apply",
    ])
    assert args.state_dir == "/tmp/assigned-state"
    assert args.session == "chosen"
    assert args.dry_run is True

    monkeypatch.setattr(remote, "list_profiles", lambda: (_ for _ in ()).throw(AssertionError("must not inspect profiles")))
    monkeypatch.setattr(remote, "ssh_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not connect")))
    result = remote.dispatch(args, Path(__file__).resolve().parents[2])
    assert result["session"] == "chosen"
    assert result["dry_run"] is True
    assert result["apply_suppressed"] is True


def test_explicit_remote_leaf_options_override_global_defaults() -> None:
    args = build_parser().parse_args([
        "--state-dir", "/tmp/global-state", "--session", "global-session",
        "remote", "--state-dir", "/tmp/remote-state", "deploy", "alias", "--session", "leaf-session",
    ])
    assert args.state_dir == "/tmp/remote-state"
    assert args.session == "leaf-session"


def test_global_dry_run_suppresses_tunnel_open_even_when_target_is_explicit(monkeypatch) -> None:
    args = build_parser().parse_args([
        "--dry-run", "remote", "tunnel", "open", "alias", "/tmp/herdr.sock",
    ])
    monkeypatch.setattr(remote, "open_tunnel", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tunnel must not open")))

    result = remote.dispatch(args)

    assert result == {
        "operation": "tunnel-open", "target": "alias", "remote_endpoint": "/tmp/herdr.sock",
        "state": "planned", "dry_run": True,
    }


def test_managed_binary_install_and_guarded_rollback_execute_shell(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    downloaded = tmp_path / "downloaded-herdr"
    downloaded.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo "herdr 0.9.5"; fi\n', encoding="utf-8")
    downloaded.chmod(0o700)
    curl = tools / "curl"
    curl.write_text(f'#!/bin/sh\nwhile [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then cp {shlex.quote(str(downloaded))} "$2"; exit $?; fi; shift; done\nexit 2\n', encoding="utf-8")
    curl.chmod(0o700)
    user_bin = home / ".local" / "bin" / "herdr"
    user_bin.parent.mkdir(parents=True)
    user_bin.write_text('#!/bin/sh\necho "herdr 0.9.4"\n', encoding="utf-8")
    user_bin.chmod(0o700)
    state = home / ".local" / "state" / "localsetup" / "herdr-ops"
    state.mkdir(parents=True)
    old_hash = hashlib.sha256(user_bin.read_bytes()).hexdigest()
    (state / "binary.sha256").write_text(f"localsetup-managed:{old_hash}\n", encoding="utf-8")
    environment = os.environ | {"HOME": str(home), "PATH": f"{tools}:/usr/bin:/bin"}

    commands = _local_ssh_transport(monkeypatch, environment=environment)
    digest = hashlib.sha256(downloaded.read_bytes()).hexdigest()
    installed = remote._install_binary("fixture", {"platform": "linux-x86_64", "url": "https://github.com/herdrdev/herdr/releases/download/v0.9.5/herdr-linux-x86_64", "sha256": digest, "version": "0.9.5"}, operation="upgrade", timeout=30)
    assert hashlib.sha256(user_bin.read_bytes()).hexdigest() == digest
    assert installed["backup_created"] is True
    assert Path(installed["backup_path"]).read_bytes() != downloaded.read_bytes()
    remote._rollback_binary("fixture", installed, timeout=30)
    assert hashlib.sha256(user_bin.read_bytes()).hexdigest() == old_hash
    assert (state / "binary.sha256").read_text().strip() == f"localsetup-managed:{old_hash}"
    assert len(commands) == 2
    assert all(command.startswith("sh -s -- ") and b"set -eu\n" in payload for command, payload in commands)


def test_fresh_binary_install_and_rollback_cross_real_ssh_validator(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    downloaded = tmp_path / "downloaded-herdr"
    downloaded.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo "herdr 0.9.5"; fi\n', encoding="utf-8")
    downloaded.chmod(0o700)
    curl = tools / "curl"
    curl.write_text(f'#!/bin/sh\nwhile [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then cp {shlex.quote(str(downloaded))} "$2"; exit $?; fi; shift; done\nexit 2\n', encoding="utf-8")
    curl.chmod(0o700)
    environment = os.environ | {"HOME": str(home), "PATH": f"{tools}:/usr/bin:/bin"}
    commands = _local_ssh_transport(monkeypatch, environment=environment)
    digest = hashlib.sha256(downloaded.read_bytes()).hexdigest()

    installed = remote._install_binary("fixture", {"platform": "linux-x86_64", "url": "https://github.com/herdrdev/herdr/releases/download/v0.9.5/herdr-linux-x86_64", "sha256": digest, "version": "0.9.5"}, operation="deploy", timeout=30)
    binary = home / ".local" / "bin" / "herdr"
    receipt = home / ".local" / "state" / "localsetup" / "herdr-ops" / "binary.sha256"
    assert binary.read_bytes() == downloaded.read_bytes()
    assert installed["backup_path"] is None and installed["old_sha256"] is None
    remote._rollback_binary("fixture", installed, timeout=30)
    assert not binary.exists() and not receipt.exists()
    assert len(commands) == 2
    assert shlex.split(commands[1][0]) == ["sh", "-s", "--", digest]
    assert b"backup=${2-}; old=${3-}" in commands[1][1]


def test_uncertain_install_exit_does_not_retry_or_claim_no_effect(monkeypatch) -> None:
    calls = []
    def lost_ssh(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess([], 255, b"", b"connection lost")
    monkeypatch.setattr(remote, "ssh_run", lost_ssh)
    with pytest.raises(HerdrOpsError) as caught:
        remote._install_binary("fixture", {"platform": "linux-x86_64", "url": "https://github.com/herdrdev/herdr/releases/download/v0.9.5/herdr-linux-x86_64", "sha256": "a" * 64, "version": "0.9.5"}, operation="upgrade", timeout=30)
    assert caught.value.effect == "unknown"
    assert len(calls) == 1


def test_remote_config_stage_check_backup_activation_execute_shell(tmp_path: Path, monkeypatch) -> None:
    master = tmp_path / "master.toml"
    master.write_text('[theme]\nname = "new"\n', encoding="utf-8")
    destination = tmp_path / "remote" / "config.toml"
    destination.parent.mkdir()
    original = 'remote_token = "keep-secret"\n[theme]\nname = "old"\n'
    destination.write_text(original, encoding="utf-8")
    binary = tmp_path / "herdr"
    binary.write_text('#!/bin/sh\n[ "$1" = config ] && [ "$2" = check ]\n', encoding="utf-8")
    binary.chmod(0o700)

    commands = _local_ssh_transport(monkeypatch)
    result = remote.apply_remote_config("fixture", master_config=master,
                                        remote_config=str(destination), binary_path=str(binary), apply=True)
    assert result["applied"] is True
    merged = tomllib.loads(destination.read_text())
    assert merged["remote_token"] == "keep-secret"
    assert merged["theme"]["name"] == "new"
    with gzip.open(result["backup"], "rt", encoding="utf-8") as stream:
        assert stream.read() == original
    assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
    assert all("\n" not in command for command, _ in commands)
    assert len(commands) == 3


def test_foreign_binary_owner_prevents_competing_user_install(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    foreign = tools / "herdr"
    foreign.write_text('#!/bin/sh\necho "herdr 0.9.4"\n', encoding="utf-8")
    foreign.chmod(0o700)
    environment = os.environ | {"HOME": str(home), "PATH": f"{tools}:/usr/bin:/bin"}
    _local_ssh_transport(monkeypatch, environment=environment)
    with pytest.raises(HerdrOpsError) as caught:
        remote._install_binary("fixture", {"platform": "linux-x86_64", "url": "https://github.com/herdrdev/herdr/releases/download/v0.9.5/herdr-linux-x86_64", "sha256": "a" * 64, "version": "0.9.5"}, operation="upgrade", timeout=30)
    assert caught.value.code == "unsupported_existing_install"
    assert caught.value.effect == "none"
    assert not (home / ".local").exists()


@pytest.mark.parametrize("other_state,expected", [("idle", "verified"), ("active", "needs_interruption_authority")])
def test_upgrade_assesses_every_saved_session_before_shared_mutation(tmp_path: Path, monkeypatch, other_state: str, expected: str) -> None:
    profiles = [
        {"id": "one", "label": "one", "ssh_target": "alias", "remote_session": "one", "enabled": True},
        {"id": "two", "label": "two", "ssh_target": "alias", "remote_session": "two", "enabled": True},
    ]
    events: list[str] = []
    monkeypatch.setattr(remote, "list_profiles", lambda: profiles)
    monkeypatch.setattr(remote, "_account_profiles", lambda _profiles, _target: profiles)
    monkeypatch.setattr(remote, "_remote_session_inventory", lambda *_args, **_kw: [
        {"session": name, "running": True, "remote_socket": f"/sockets/{name}.sock"} for name in ("one", "two")])
    monkeypatch.setattr(remote, "_session_socket", lambda _target, session, _explicit, **_kw: f"/sockets/{session}.sock")
    monkeypatch.setattr(remote, "_open_profile_endpoint", lambda _target, socket, _state: (socket, "a" * 32))
    monkeypatch.setattr(remote, "close_tunnel", lambda *_args, **_kw: {"state": "closed"})
    monkeypatch.setattr(remote, "_ping", lambda endpoint, **_kw: {"version": "0.8.0" if "one" in endpoint else "0.9.5", "protocol": 21 if "one" in endpoint else 22, "endpoint_generation": 1})
    def occupants(endpoint):
        session = "one" if "one" in endpoint else "two"
        events.append(f"inspect:{session}")
        return [{"pane_id": f"{session}:p1", "state": other_state if session == "two" else "idle", "reason": "fixture"}]
    monkeypatch.setattr(remote, "_inspect_occupants", occupants)
    monkeypatch.setattr(remote, "_pinned_release", lambda *_args: {"platform": "linux-x86_64", "version": "0.9.5"})
    monkeypatch.setattr(remote, "_install_binary", lambda *_args, **_kw: events.append("install") or {"version": "0.9.5", "backup_path": "/backup/herdr"})
    monkeypatch.setattr(remote, "apply_remote_config", lambda *_args, **_kw: events.append("config") or {"applied": True})
    monkeypatch.setattr(remote, "_stop_named_server", lambda _target, session, _socket, **_kw: events.append(f"stop:{session}"))
    monkeypatch.setattr(remote, "_wait_remote_socket_absent", lambda *_args, **_kw: None)
    monkeypatch.setattr(remote, "_start_headless_server", lambda _target, session, _socket, _binary, **_kw: events.append(f"start:{session}") or {"session": session})
    monkeypatch.setattr(remote, "_wait_ping", lambda *_args, **_kw: {"protocol": 22})
    monkeypatch.setattr(remote, "_user_binary_path", lambda _target: "/new/herdr")
    monkeypatch.setattr(remote, "inspect_profile", lambda selector: {"profile": next(p for p in profiles if p["id"] == selector), "connectivity": "connected"})
    args = argparse.Namespace(target="alias", session="one", label=None, remote_socket=None, apply=True,
                              timeout=30, allow_interruption=False, master_config=None, state_dir=str(tmp_path))
    result = remote._install_command(args, tmp_path / "manifest.json", operation="upgrade")
    assert result["state"] == expected
    assert events[:2] == ["inspect:one", "inspect:two"]
    if expected == "verified":
        assert events[2:] == ["install", "config", "stop:one", "start:one", "stop:two", "start:two"]
        assert len(result["restarted"]) == 2
        record = json.loads((tmp_path / "remote-operations" / f"{result['operation_id']}.json").read_text())
        assert record["state"] == "verified" and record["phase"] == "verified"
        assert record["restarted"] == ["one", "two"]
    else:
        assert events == ["inspect:one", "inspect:two"]
        assert result["affected"][0]["session"] == "two"


def test_deploy_adopts_compatible_running_server_without_replacement(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(remote, "list_profiles", lambda: [])
    monkeypatch.setattr(remote, "_account_profiles", lambda _profiles, _target: [])
    monkeypatch.setattr(remote, "_session_socket", lambda *_args, **_kw: "/sockets/default.sock")
    monkeypatch.setattr(remote, "_open_profile_endpoint", lambda *_args: ("/relay/herdr.sock", "a" * 32))
    monkeypatch.setattr(remote, "_ping", lambda *_args, **_kw: {"version": "0.9.5", "protocol": 22, "endpoint_generation": 1})
    monkeypatch.setattr(remote, "close_tunnel", lambda *_args, **_kw: calls.append("close") or {"state": "closed"})
    profile = {"id": "adopted", "label": "default", "ssh_target": "alias", "remote_session": "default", "enabled": True}
    monkeypatch.setattr(remote, "_register_profile", lambda *_args: calls.append("register") or profile)
    monkeypatch.setattr(remote, "inspect_profile", lambda *_args: {"connectivity": "connected", "profile": profile})
    monkeypatch.setattr(remote, "_install_binary", lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("must not replace binary")))
    args = argparse.Namespace(target="alias", session="default", label=None, remote_socket=None,
                              apply=True, timeout=30, allow_interruption=False, master_config=None, state_dir=str(tmp_path))
    result = remote._install_command(args, tmp_path / "manifest.json", operation="deploy")
    assert result["state"] == "adopted"
    assert calls == ["close", "register"]


@pytest.mark.parametrize("effect,rollback_expected", [("none", True), ("unknown", False)])
def test_proven_config_failure_rolls_back_binary_but_uncertain_activation_does_not(tmp_path: Path, monkeypatch, effect: str, rollback_expected: bool) -> None:
    events: list[str] = []
    monkeypatch.setattr(remote, "list_profiles", lambda: [])
    monkeypatch.setattr(remote, "_account_profiles", lambda _profiles, _target: [])
    monkeypatch.setattr(remote, "_remote_session_inventory", lambda *_args, **_kw: [])
    monkeypatch.setattr(remote, "_session_socket", lambda *_args, **_kw: "/sockets/default.sock")
    monkeypatch.setattr(remote, "_open_profile_endpoint", lambda *_args: (_ for _ in ()).throw(HerdrOpsError("server_unavailable", "no server")))
    monkeypatch.setattr(remote, "_remote_path_exists", lambda *_args, **_kw: False)
    monkeypatch.setattr(remote, "_pinned_release", lambda *_args: {"version": "0.9.5"})
    monkeypatch.setattr(remote, "_install_binary", lambda *_args, **_kw: events.append("install") or {"sha256": "a" * 64})
    monkeypatch.setattr(remote, "_user_binary_path", lambda _target: "/home/fixture/.local/bin/herdr")
    def failed_config(*_args, **_kw):
        events.append("config")
        raise HerdrOpsError("native_check_failed" if effect == "none" else "config_result_unknown", "failed", effect)
    monkeypatch.setattr(remote, "apply_remote_config", failed_config)
    monkeypatch.setattr(remote, "_rollback_binary", lambda *_args, **_kw: events.append("rollback"))
    monkeypatch.setattr(remote, "_start_headless_server", lambda *_args, **_kw: events.append("start"))
    args = argparse.Namespace(target="alias", session="default", label=None, remote_socket=None,
                              apply=True, timeout=30, allow_interruption=False, master_config=None, state_dir=str(tmp_path))
    with pytest.raises(HerdrOpsError) as caught:
        remote._install_command(args, tmp_path / "manifest.json", operation="deploy")
    assert caught.value.effect == effect
    assert caught.value.operation_id
    record = json.loads((tmp_path / "remote-operations" / f"{caught.value.operation_id}.json").read_text())
    assert record["phase"] == ("binary_rolled_back" if rollback_expected else "binary_installed")
    assert record["state"] == ("failed" if rollback_expected else "unknown")
    assert events == (["install", "config", "rollback"] if rollback_expected else ["install", "config"])


def test_fresh_deploy_verifies_profile_and_shared_session_before_mutation(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    other = {"id": "other", "label": "other", "ssh_target": "alias", "remote_session": "other", "enabled": True}
    new = {"id": "new", "label": "new", "ssh_target": "alias", "remote_session": "new", "enabled": True}
    monkeypatch.setattr(remote, "list_profiles", lambda: [other])
    monkeypatch.setattr(remote, "_account_profiles", lambda _profiles, _target: [other])
    monkeypatch.setattr(remote, "_remote_session_inventory", lambda *_args, **_kw: [
        {"session": "other", "running": True, "remote_socket": "/sockets/other.sock"}])
    monkeypatch.setattr(remote, "_session_socket", lambda _target, session, _explicit, **_kw: f"/sockets/{session}.sock")
    monkeypatch.setattr(remote, "_open_profile_endpoint", lambda _target, socket, _state: (socket, "a" * 32))
    def ping(endpoint, **_kw):
        if "new" in endpoint and "install" not in events:
            raise HerdrOpsError("server_unavailable", "not started")
        return {"version": "0.9.5", "protocol": 22, "endpoint_generation": 1}
    monkeypatch.setattr(remote, "_ping", ping)
    monkeypatch.setattr(remote, "_inspect_occupants", lambda endpoint: events.append("inspect:other") or [{"pane_id": "other:p", "state": "idle", "reason": "shell-only"}])
    monkeypatch.setattr(remote, "close_tunnel", lambda *_args, **_kw: {"state": "closed"})
    monkeypatch.setattr(remote, "_remote_path_exists", lambda *_args, **_kw: False)
    monkeypatch.setattr(remote, "_pinned_release", lambda *_args: {"version": "0.9.5"})
    monkeypatch.setattr(remote, "_install_binary", lambda *_args, **_kw: events.append("install") or {"version": "0.9.5"})
    monkeypatch.setattr(remote, "apply_remote_config", lambda *_args, **_kw: events.append("config") or {"applied": True})
    monkeypatch.setattr(remote, "_user_binary_path", lambda _target: "/home/fixture/.local/bin/herdr")
    monkeypatch.setattr(remote, "_start_headless_server", lambda _target, session, _socket, _binary, **_kw: events.append(f"start:{session}") or {"session": session})
    monkeypatch.setattr(remote, "_wait_ping", lambda *_args, **_kw: {"protocol": 22})
    monkeypatch.setattr(remote, "_register_profile", lambda *_args: events.append("register") or new)
    monkeypatch.setattr(remote, "find_profile", lambda *_args: new)
    monkeypatch.setattr(remote, "inspect_profile", lambda selector: {"connectivity": "connected", "profile": new if selector == "new" else other})
    monkeypatch.setattr(remote, "_stop_named_server", lambda _target, session, _socket, **_kw: events.append(f"stop:{session}"))
    monkeypatch.setattr(remote, "_wait_remote_socket_absent", lambda *_args, **_kw: None)
    args = argparse.Namespace(target="alias", session="new", label="new", remote_socket=None,
                              apply=True, timeout=30, allow_interruption=False, master_config=None, state_dir=str(tmp_path))
    result = remote._install_command(args, tmp_path / "manifest.json", operation="deploy")
    assert result["state"] == "verified"
    assert result["restarted_shared_sessions"] == ["other"]
    assert events == ["inspect:other", "install", "config", "start:new", "register", "stop:other", "start:other"]


def test_native_remote_inventory_includes_unregistered_running_session(monkeypatch) -> None:
    payload = {"sessions": [{"default": False, "name": "unregistered", "running": True,
                              "session_dir": "/home/u/.config/herdr/sessions/unregistered",
                              "socket_path": "/home/u/.config/herdr/sessions/unregistered/herdr.sock"}]}
    monkeypatch.setattr(remote, "ssh_run", lambda *_args, **_kw: subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b""))
    inventory = remote._remote_session_inventory("fixture", timeout=5)
    assert inventory == [{"session": "unregistered", "running": True,
                          "remote_socket": payload["sessions"][0]["socket_path"]}]
    monkeypatch.setattr(remote, "_remote_session_inventory", lambda *_args, **_kw: inventory)
    monkeypatch.setattr(remote, "_open_profile_endpoint", lambda *_args: ("/relay/socket", "a" * 32))
    monkeypatch.setattr(remote, "_ping", lambda *_args: {"version": "0.8.0", "protocol": 21})
    monkeypatch.setattr(remote, "_inspect_occupants", lambda *_args: [{"pane_id": "w:p", "state": "active", "reason": "foreground-process"}])
    monkeypatch.setattr(remote, "close_tunnel", lambda *_args, **_kw: {"state": "closed"})
    observed = remote._observe_shared_sessions("fixture", [], selected_session="new", selected_socket=None,
                                               include_selected=False, state_dir=None, timeout=5)
    assert observed[0]["profile"] is None
    assert observed[0]["occupants"][0]["state"] == "active"


def test_shared_account_profiles_include_other_ssh_alias(monkeypatch) -> None:
    monkeypatch.setattr(remote, "_qualify_ssh_target", lambda alias: {
        "hostname": "same.example", "user": "operator", "port": "22",
    } if alias in {"first", "second"} else {"hostname": "different.example", "user": "operator", "port": "22"})
    profiles = [
        {"id": "one", "ssh_target": "first", "remote_session": "one"},
        {"id": "two", "ssh_target": "second", "remote_session": "two"},
        {"id": "three", "ssh_target": "other", "remote_session": "three"},
    ]
    assert [item["id"] for item in remote._account_profiles(profiles, "first")] == ["one", "two"]
