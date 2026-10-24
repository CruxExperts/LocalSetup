"""Explicit SSH profiles, lifecycle planning, task-owned socket relays, and safe remote config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .config import merge_config_text, translate_config
from .errors import HerdrOpsError
from .provenance import default_manifest_path
from .transport import (
    MAX_UNIX_SOCKET_PATH_BYTES,
    atomic_write_json,
    machine_snapshot,
    owner_id,
    read_json_state,
    request,
    state_directory,
    writer_lock,
)


_MAX_OUTPUT = 8 * 1024 * 1024
_SSH_OPTIONS = (
    "BatchMode=yes",
    "StrictHostKeyChecking=yes",
    "PasswordAuthentication=no",
    "KbdInteractiveAuthentication=no",
    "NumberOfPasswordPrompts=0",
    "ExitOnForwardFailure=yes",
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("remote", help="Inspect or explicitly manage a saved Herdr SSH machine")
    parser.add_argument("--state-dir", default=argparse.SUPPRESS, help="private local Herdr operation-state directory")
    commands = parser.add_subparsers(dest="remote_command", required=True)
    commands.add_parser("profiles", help="List sanitized saved machine profiles")

    plan = commands.add_parser("plan", help="Plan a remote lifecycle operation without connecting")
    plan.add_argument("operation", choices=("deploy", "upgrade", "restart", "connect", "config"))
    plan.add_argument("target", help="SSH alias/target or saved Herdr profile ID/label")
    plan.add_argument("--session", default=argparse.SUPPRESS)
    plan.add_argument("--label")
    plan.add_argument("--remote-socket")

    inspect = commands.add_parser("inspect", help="Read a saved profile's compatible API snapshot")
    inspect.add_argument("profile")

    connect = commands.add_parser("connect", help="Inspect a saved profile and return native attach argv")
    connect.add_argument("profile")

    for name, help_text in (("deploy", "Install, configure, start, verify, and register an explicit remote session"),
                            ("upgrade", "Preflight, upgrade, configure, idle-first restart, and verify an explicit remote session")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("target", help="explicit SSH alias/target")
        command.add_argument("--session", default=argparse.SUPPRESS)
        command.add_argument("--label")
        command.add_argument("--remote-socket")
        command.add_argument("--manifest", type=Path)
        command.add_argument("--apply", action="store_true", help="dispatch the explicitly requested lifecycle mutation")
        command.add_argument("--allow-interruption", action="store_true", help="one decision for active/unknown occupants during upgrade")
        command.add_argument("--master-config", type=Path, help="local master config to translate; defaults to the active Herdr config")
        command.add_argument("--timeout", type=float, default=30)

    restart = commands.add_parser("restart", help="Idle-first restart of an explicit saved machine/session")
    restart.add_argument("profile")
    restart.add_argument("--remote-socket", help="explicit custom socket; otherwise use the documented session socket")
    restart.add_argument("--apply", action="store_true", help="dispatch the restart")
    restart.add_argument("--allow-interruption", action="store_true", help="one decision for the listed active/unknown occupants")
    restart.add_argument("--timeout", type=float, default=30)

    config = commands.add_parser("config", help="Apply the portable allowlisted master config to an explicit SSH target")
    config.add_argument("target", help="explicit SSH alias/target")
    config.add_argument("--master-config", type=Path)
    config.add_argument("--remote-config", help="explicit remote TOML path")
    config.add_argument("--apply", action="store_true", help="stage, natively validate, back up, and atomically activate config")
    config.add_argument("--timeout", type=float, default=30)

    tunnel = commands.add_parser("tunnel", help="Manage a task-owned local Unix-socket relay")
    tunnel_sub = tunnel.add_subparsers(dest="tunnel_command", required=True)
    open_parser = tunnel_sub.add_parser("open", help="Open an explicit SSH Unix-socket relay")
    open_parser.add_argument("target")
    open_parser.add_argument("remote_socket", help="explicit absolute remote Herdr socket path")
    open_parser.add_argument("--state-dir", default=argparse.SUPPRESS)
    tcp_parser = tunnel_sub.add_parser("tcp", help="Forward an explicit remote TCP port to loopback only")
    tcp_parser.add_argument("target")
    tcp_parser.add_argument("remote_host", help="remote host reachable from the SSH target (usually 127.0.0.1)")
    tcp_parser.add_argument("remote_port", type=int)
    tcp_parser.add_argument("local_port", type=int)
    tcp_parser.add_argument("--state-dir", default=argparse.SUPPRESS)
    close_parser = tunnel_sub.add_parser("close", help="Close only a relay with a matching owner identity")
    close_parser.add_argument("owner_id")
    close_parser.add_argument("--state-dir", default=argparse.SUPPRESS)


def dispatch(args: argparse.Namespace, root: Path | None = None) -> dict[str, Any]:
    command = getattr(args, "remote_command", None)
    state_dir = getattr(args, "state_dir", None)
    if getattr(args, "dry_run", False) and command in {"deploy", "upgrade", "restart", "config", "tunnel"}:
        if command in {"deploy", "upgrade"}:
            planned = argparse.Namespace(**vars(args))
            planned.apply = False
            return _install_command(
                planned,
                getattr(args, "manifest", None) or default_manifest_path(root or Path(__file__).resolve().parents[3]),
                operation=command,
            ) | {"dry_run": True, "apply_suppressed": bool(getattr(args, "apply", False))}
        if command == "restart":
            return lifecycle_plan("restart", args.profile) | {"dry_run": True, "apply_suppressed": bool(args.apply)}
        if command == "config":
            return apply_remote_config(args.target, master_config=args.master_config,
                                       remote_config=args.remote_config, apply=False,
                                       timeout=args.timeout) | {
                                           "dry_run": True,
                                           "apply_suppressed": bool(args.apply),
                                       }
        if args.tunnel_command == "open":
            _validate_remote_socket(args.remote_socket)
            return {"operation": "tunnel-open", "target": validate_ssh_target(args.target),
                    "remote_endpoint": args.remote_socket, "state": "planned", "dry_run": True}
        if args.tunnel_command == "tcp":
            if args.remote_host not in {"127.0.0.1", "localhost", "[::1]"} or not 1 <= args.remote_port <= 65535 or not 1 <= args.local_port <= 65535:
                raise HerdrOpsError("invalid_tunnel", "TCP tunnel must target a loopback service and valid ports.")
            return {"operation": "tunnel-tcp", "target": validate_ssh_target(args.target),
                    "remote_endpoint": f"{args.remote_host}:{args.remote_port}",
                    "local_endpoint": f"127.0.0.1:{args.local_port}", "state": "planned", "dry_run": True}
        owner_id(args.owner_id)
        return {"operation": "tunnel-close", "owner_id": args.owner_id, "state": "planned", "dry_run": True}
    if command == "profiles":
        return {"profiles": list_profiles()}
    if command == "plan":
        return lifecycle_plan(args.operation, args.target, session=getattr(args, "session", None) or "default", label=args.label, remote_socket=args.remote_socket)
    if command in {"inspect", "connect"}:
        return inspect_profile(args.profile, attach=command == "connect")
    if command in {"deploy", "upgrade"}:
        path = getattr(args, "manifest", None) or default_manifest_path(root or Path(__file__).resolve().parents[3])
        return _install_command(args, path, operation=command)
    if command == "restart":
        return restart_profile(args.profile, remote_socket=args.remote_socket, apply=args.apply,
                               allow_interruption=args.allow_interruption, timeout=args.timeout,
                               state_dir=state_dir)
    if command == "config":
        return apply_remote_config(args.target, master_config=args.master_config,
                                   remote_config=args.remote_config, apply=args.apply,
                                   timeout=args.timeout)
    if command == "tunnel":
        if args.tunnel_command == "open":
            return open_tunnel(args.target, args.remote_socket, state_dir=state_dir)
        if args.tunnel_command == "tcp":
            return open_tcp_tunnel(args.target, args.remote_host, args.remote_port, args.local_port, state_dir=state_dir)
        return close_tunnel(args.owner_id, state_dir=state_dir)
    raise HerdrOpsError("unknown_command", "Unknown Herdr remote operation.")


def validate_ssh_target(target: str) -> str:
    """Accept only one explicit SSH host token, never an option or command."""
    if (not isinstance(target, str) or not target or len(target) > 255 or target.startswith("-")
            or any(ord(ch) < 33 or ord(ch) == 127 for ch in target)):
        raise HerdrOpsError("invalid_ssh_target", "Provide one explicit SSH alias or host without whitespace or control characters.")
    return target


def validate_remote_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(argv, (tuple, list)) or not argv:
        raise HerdrOpsError("remote_command_required", "An explicit remote argv command is required.")
    if any(not isinstance(item, str) or not item or any(ord(ch) < 32 or ord(ch) == 127 for ch in item) for item in argv):
        raise HerdrOpsError("invalid_remote_command", "Remote argv must contain non-empty single-line arguments.")
    if len(shlex.join(argv).encode("utf-8")) > 64 * 1024:
        raise HerdrOpsError("remote_command_too_large", "The encoded remote command exceeds 64 KiB.")
    return tuple(argv)


def _qualify_ssh_target(target: str, *, timeout: float = 10) -> dict[str, str]:
    alias = validate_ssh_target(target)
    ssh = _ssh_binary()
    try:
        completed = subprocess.run(
            [ssh, "-G", alias],
            capture_output=True,
            timeout=min(max(timeout, 0.1), 30),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise HerdrOpsError("ssh_unavailable", "Could not evaluate the explicit SSH target configuration.") from None
    if completed.returncode != 0:
        raise HerdrOpsError("ssh_target_unavailable", "SSH rejected the explicit target or its effective configuration.")
    if len(completed.stdout) > _MAX_OUTPUT:
        raise HerdrOpsError("ssh_config_too_large", "Effective SSH target configuration exceeded the safe limit.")
    settings: dict[str, str] = {}
    try:
        for line in completed.stdout.decode("utf-8").splitlines():
            key, _, value = line.partition(" ")
            if key in {"hostname", "user", "port", "proxyjump", "controlmaster", "stricthostkeychecking"}:
                settings[key] = value.strip()
    except UnicodeDecodeError:
        raise HerdrOpsError("ssh_config_invalid", "Effective SSH target configuration was not readable.") from None
    if not settings.get("hostname"):
        raise HerdrOpsError("ssh_config_invalid", "Effective SSH configuration did not resolve a host.")
    return settings


def ssh_command(target: str, remote_argv: Sequence[str], *, timeout: float = 30) -> list[str]:
    """Qualify and construct strict SSH argv without dispatching a command."""
    alias = validate_ssh_target(target)
    argv = validate_remote_argv(remote_argv)
    import math
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise HerdrOpsError("invalid_timeout", "SSH command timeout must be between 0 and 600 seconds.")
    effective = _qualify_ssh_target(alias, timeout=min(timeout, 30))
    if effective.get("stricthostkeychecking", "ask").lower() not in {"yes", "ask"}:
        raise HerdrOpsError("ssh_host_key_policy", "SSH target disables strict host-key checking; no remote command was sent.")
    return [_ssh_binary(), *[item for option in _SSH_OPTIONS for item in ("-o", option)],
            alias, shlex.join(argv)]


def ssh_run(
    target: str,
    remote_argv: Sequence[str],
    *,
    stdin: bytes | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[bytes]:
    """Run a safely quoted command via the target's existing strict SSH config.

    OpenSSH accepts a remote command string; argv is therefore encoded once with
    POSIX shell quoting. Existing aliases, ProxyJump, ControlMaster, and identity
    configuration remain in effect. No interactive authentication is permitted.
    """
    command = ssh_command(target, remote_argv, timeout=timeout)
    input_options: dict[str, Any] = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
            **input_options,
        )
    except subprocess.TimeoutExpired as exc:
        raise HerdrOpsError("ssh_timeout", "SSH command timed out; reconcile remote state before retrying.", "unknown") from None
    except OSError:
        raise HerdrOpsError("ssh_unavailable", "Could not start the SSH command.") from None
    if len(result.stdout) > _MAX_OUTPUT or len(result.stderr) > _MAX_OUTPUT:
        raise HerdrOpsError("ssh_output_too_large", "SSH command output exceeded the safe limit.", "unknown")
    return result


def list_profiles() -> list[dict[str, Any]]:
    completed = _local_herdr(["machine", "list", "--json"])
    if completed.returncode:
        raise HerdrOpsError("machine_list_failed", "Could not read saved Herdr machine profiles.")
    try:
        raw = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HerdrOpsError("machine_list_invalid", "Herdr machine list was not valid JSON.") from None
    if isinstance(raw, dict) and isinstance(raw.get("result"), (dict, list)):
        raw = raw["result"]
    if isinstance(raw, dict):
        for key in ("machines", "profiles", "items"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        raise HerdrOpsError("machine_list_invalid", "Herdr machine list did not contain a profile list.")
    profiles: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        profile = {key: row[key] for key in ("id", "label", "ssh_target", "remote_session", "enabled") if key in row}
        if not all(isinstance(profile.get(key), str) and profile[key] for key in ("id", "label", "ssh_target", "remote_session")):
            continue
        profile["enabled"] = bool(profile.get("enabled", True))
        profiles.append(profile)
    return profiles


def find_profile(selector: str, profiles: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    if not isinstance(selector, str) or not selector:
        raise HerdrOpsError("profile_required", "Provide an exact saved Herdr machine ID or label.")
    matches = [dict(p) for p in profiles if p.get("id") == selector or p.get("label") == selector] if profiles is not None else [p for p in list_profiles() if p.get("id") == selector or p.get("label") == selector]
    if len(matches) != 1:
        raise HerdrOpsError("profile_not_found" if not matches else "profile_ambiguous", "Select exactly one saved Herdr profile by its case-sensitive ID or label.")
    if not matches[0].get("enabled", True):
        raise HerdrOpsError("profile_disabled", "The selected Herdr profile is disabled.")
    return matches[0]


def inspect_profile(selector: str, *, attach: bool = False) -> dict[str, Any]:
    profile = find_profile(selector)
    try:
        snapshot = machine_snapshot(str(profile["id"]))
    except HerdrOpsError as exc:
        return {"profile": _public_profile(profile), "connectivity": "unavailable", "reason": exc.code}
    body = _snapshot_body(snapshot)
    panes = body.get("panes") if isinstance(body, dict) else None
    agents = body.get("agents") if isinstance(body, dict) else None
    result: dict[str, Any] = {
        "profile": _public_profile(profile),
        "connectivity": "connected",
        "version": body.get("version") if isinstance(body, dict) else None,
        "protocol": body.get("protocol") if isinstance(body, dict) else None,
        "pane_count": len(panes) if isinstance(panes, list) else None,
        "agent_count": len(agents) if isinstance(agents, list) else None,
    }
    if attach:
        result["attach_argv"] = ["herdr", "--remote", str(profile["ssh_target"]), "--session", str(profile["remote_session"])]
    return result


def lifecycle_plan(operation: str, target: str, *, session: str = "default", label: str | None = None,
                   remote_socket: str | None = None) -> dict[str, Any]:
    if operation not in {"deploy", "upgrade", "restart", "connect", "config"}:
        raise HerdrOpsError("unknown_operation", "Unknown Herdr lifecycle operation.")
    _validate_session(session)
    if remote_socket is not None:
        _validate_remote_socket(remote_socket)
    return {
        "operation": operation,
        "target": target,
        "session": session,
        "label": label,
        "remote_socket": remote_socket,
        "steps": _PLAN_STEPS[operation],
        "needs_authorization": operation in {"deploy", "upgrade", "restart", "config"},
    }


def _snapshot_body(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("type") == "session_snapshot" and isinstance(value.get("snapshot"), dict):
        return value["snapshot"]
    if isinstance(value.get("snapshot"), dict):
        return value["snapshot"]
    if isinstance(value.get("result"), dict):
        result = value["result"]
        return result.get("snapshot", result) if isinstance(result.get("snapshot", result), dict) else value
    return value


def _public_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {key: profile[key] for key in ("id", "label", "ssh_target", "remote_session", "enabled") if key in profile}


def _local_herdr(argv: Sequence[str], *, timeout: float = 30, stdin: bytes | None = None,
                 mutation: bool = False) -> subprocess.CompletedProcess[bytes]:
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    environment = os.environ.copy()
    environment["SSH_ASKPASS_REQUIRE"] = "never"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        options: dict[str, Any] = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
        result = subprocess.run([binary, *argv], capture_output=True, env=environment,
                                timeout=timeout, check=False, **options)
    except OSError:
        raise HerdrOpsError("herdr_cli_unavailable", "Could not run the local Herdr CLI command.") from None
    except subprocess.TimeoutExpired:
        raise HerdrOpsError("herdr_cli_timeout", "Local Herdr CLI timed out; reconcile before retrying.",
                            "unknown" if mutation else "none") from None
    if len(result.stdout) > _MAX_OUTPUT or len(result.stderr) > _MAX_OUTPUT:
        raise HerdrOpsError("herdr_cli_output_too_large", "Local Herdr CLI output exceeded the safe limit.",
                            "unknown" if mutation else "none")
    return result


def _ssh_binary() -> str:
    import shutil

    binary = shutil.which("ssh")
    if not binary:
        raise HerdrOpsError("ssh_unavailable", "OpenSSH client is not available.")
    return binary


def open_tunnel(
    target: str,
    remote_socket: str,
    *,
    state_dir: str | os.PathLike[str] | None = None,
    timeout: float = 5,
) -> dict[str, Any]:
    """Open a private task-owned Unix-socket relay using an explicit remote path."""
    alias = validate_ssh_target(target)
    remote = _validate_remote_socket(remote_socket)
    ssh_settings = _qualify_ssh_target(alias)
    root = state_directory(state_dir)
    owner = uuid.uuid4().hex
    tunnel_root = Path(tempfile.mkdtemp(prefix="lsho-", dir="/tmp"))
    os.chmod(tunnel_root, 0o700)
    local_socket = tunnel_root / "s"
    if len(os.fsencode(local_socket)) > MAX_UNIX_SOCKET_PATH_BYTES:
        tunnel_root.rmdir()
        raise HerdrOpsError("local_socket_path_too_long", "Could not allocate a short private relay socket path.")
    ssh = _ssh_binary()
    forward = f"{local_socket}:{remote}"
    argv = [ssh, *[item for option in _SSH_OPTIONS for item in ("-o", option)], "-N", "-L", forward, alias]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        tunnel_root.rmdir()
        raise HerdrOpsError("tunnel_start_failed", "Could not start the SSH socket relay.") from None
    identity = _process_identity(process.pid)
    if identity is None:
        process.terminate()
        process.wait(timeout=2)
        tunnel_root.rmdir()
        raise HerdrOpsError("tunnel_identity_unavailable", "Could not establish an exact owner identity for the SSH relay.")
    record = {
        "schema_version": 1,
        "owner_id": owner,
        "kind": "unix_socket",
        "target": alias,
        "ssh_settings": ssh_settings,
        "remote_endpoint": remote,
        "local_endpoint": str(local_socket),
        "private_dir": str(tunnel_root),
        "process": identity,
        "created_at": time.time(),
    }
    record_path = _tunnel_path(root, owner)
    with writer_lock(root):
        atomic_write_json(record_path, record)
    deadline = time.monotonic() + min(max(timeout, 0.1), 20)
    while time.monotonic() < deadline:
        if local_socket.is_socket():
            return {key: record[key] for key in ("owner_id", "kind", "target", "remote_endpoint", "local_endpoint")}
        if process.poll() is not None:
            _unlink_owned_socket(local_socket)
            _forget_tunnel(root, record_path, tunnel_root)
            raise HerdrOpsError("tunnel_start_failed", "SSH exited before opening the private relay socket.")
        time.sleep(0.05)
    close_tunnel(owner, state_dir=root)
    raise HerdrOpsError("tunnel_timeout", "SSH did not open the private relay socket before timeout.")


def open_tcp_tunnel(
    target: str,
    remote_host: str,
    remote_port: int,
    local_port: int,
    *,
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Forward one remote TCP port to loopback without exposing a listener externally."""
    alias = validate_ssh_target(target)
    if remote_host not in {"127.0.0.1", "localhost", "[::1]"}:
        raise HerdrOpsError("invalid_remote_host", "TCP forwards are limited to a loopback service on the SSH target.")
    if not 1 <= remote_port <= 65535 or not 1 <= local_port <= 65535:
        raise HerdrOpsError("invalid_port", "Remote and local forwarding ports must be between 1 and 65535.")
    _qualify_ssh_target(alias)
    root = state_directory(state_dir)
    owner = uuid.uuid4().hex
    ssh = _ssh_binary()
    forward = f"127.0.0.1:{local_port}:{remote_host}:{remote_port}"
    argv = [ssh, *[item for option in _SSH_OPTIONS for item in ("-o", option)], "-N", "-L", forward, alias]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        raise HerdrOpsError("tunnel_start_failed", "Could not start the loopback-only TCP relay.") from None
    identity = _process_identity(process.pid)
    if identity is None:
        process.terminate()
        process.wait(timeout=2)
        raise HerdrOpsError("tunnel_identity_unavailable", "Could not establish an exact owner identity for the TCP relay.")
    record = {
        "schema_version": 1,
        "owner_id": owner,
        "kind": "tcp_loopback",
        "target": alias,
        "remote_endpoint": f"{remote_host}:{remote_port}",
        "local_endpoint": f"127.0.0.1:{local_port}",
        "process": identity,
        "created_at": time.time(),
    }
    with writer_lock(root):
        atomic_write_json(_tunnel_path(root, owner), record)
    # A real local bind is a read-only verification of the requested port.
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _forget_tunnel(root, _tunnel_path(root, owner), None)
            raise HerdrOpsError("tunnel_start_failed", "SSH exited while opening the loopback-only TCP relay.")
        time.sleep(0.02)
    return {key: record[key] for key in ("owner_id", "kind", "target", "remote_endpoint", "local_endpoint")}


def close_tunnel(owner: str, *, state_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    identity = owner_id(owner)
    root = state_directory(state_dir, create=False)
    record_path = _tunnel_path(root, identity)
    with writer_lock(root):
        record = read_json_state(record_path)
        if record.get("owner_id") != identity or not isinstance(record.get("process"), dict):
            raise HerdrOpsError("ownership_mismatch", "Tunnel state does not match the requested owner ID.")
        pid = record["process"].get("pid")
        current = _process_identity(pid) if isinstance(pid, int) else None
        if current is None:
            private = Path(record["private_dir"]) if isinstance(record.get("private_dir"), str) else None
            local_endpoint = record.get("local_endpoint")
            if private and private.parent == Path("/tmp") and private.name.startswith("lsho-") and isinstance(local_endpoint, str) and Path(local_endpoint) == private / "s":
                _unlink_owned_socket(Path(local_endpoint))
            _forget_tunnel(root, record_path, private)
            return {"owner_id": identity, "state": "already-exited"}
        if not _same_process_identity(record["process"], current):
            raise HerdrOpsError("ownership_mismatch", "Current process identity differs from the recorded SSH relay; it was left untouched.")
        try:
            if os.getpgid(pid) != pid:
                raise HerdrOpsError("ownership_mismatch", "Recorded SSH relay no longer owns its process group.")
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            raise HerdrOpsError("tunnel_close_failed", "Could not stop the exact task-owned SSH relay.", "unknown") from None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = _process_identity(pid)
            if current is None:
                break
            if not _same_process_identity(record["process"], current):
                raise HerdrOpsError("ownership_mismatch", "Process identity changed while closing the relay; no further signal was sent.", "unknown")
            time.sleep(0.05)
        else:
            current = _process_identity(pid)
            if current is not None and _same_process_identity(record["process"], current):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    raise HerdrOpsError("tunnel_close_failed", "Could not finish closing the exact task-owned SSH relay.", "unknown") from None
        local_endpoint = record.get("local_endpoint")
        private_dir = Path(record["private_dir"]) if isinstance(record.get("private_dir"), str) else None
        if private_dir and (private_dir.parent != Path("/tmp") or not private_dir.name.startswith("lsho-")):
            raise HerdrOpsError("ownership_mismatch", "Relay temporary directory does not match the task-owned location.")
        if isinstance(local_endpoint, str) and private_dir and Path(local_endpoint) == private_dir / "s":
            _unlink_owned_socket(Path(local_endpoint))
        _forget_tunnel(root, record_path, private_dir)
    return {"owner_id": identity, "state": "closed"}


def _tunnel_path(root: Path, identity: str) -> Path:
    return root / "tunnels" / f"{owner_id(identity)}.json"


def verify_receipt_tunnel(endpoint: str, target: str, *, state_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Bind remote receipt SSH reads to the live relay carrying pane input."""
    root = state_directory(state_dir, create=False)
    path = str(Path(endpoint).expanduser().absolute())
    records: list[dict[str, Any]] = []
    for candidate in (root / "tunnels").glob("*.json"):
        record = read_json_state(candidate)
        if record.get("local_endpoint") == path:
            records.append(record)
    if len(records) != 1:
        raise HerdrOpsError("receipt_route_unbound", "The remote task endpoint has no unique task-owned SSH relay.")
    record = records[0]
    if record.get("kind") != "unix_socket" or record.get("target") != target or not isinstance(record.get("remote_endpoint"), str):
        raise HerdrOpsError("receipt_route_mismatch", "The receipt SSH target differs from the task's pane relay.")
    process = record.get("process")
    pid = process.get("pid") if isinstance(process, dict) else None
    current = _process_identity(pid) if isinstance(pid, int) else None
    if current is None or not _same_process_identity(process, current):
        raise HerdrOpsError("receipt_route_unbound", "The recorded pane relay is no longer the running SSH process.")
    settings = _qualify_ssh_target(target)
    if settings != record.get("ssh_settings"):
        raise HerdrOpsError("receipt_route_mismatch", "The effective SSH target changed since the pane relay opened.")
    return {"target": target, "remote_socket": str(record["remote_endpoint"]),
            "tunnel_owner": str(record["owner_id"]), "ssh_settings": settings}


def _process_identity(pid: int) -> dict[str, Any] | None:
    if pid <= 0:
        return None
    proc = Path(f"/proc/{pid}")
    if proc.is_dir():
        try:
            raw = (proc / "cmdline").read_bytes()
            stat = (proc / "stat").read_text(encoding="ascii")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            fields = stat[stat.rfind(")") + 2 :].split()
            start_ticks = fields[19]
        except (OSError, UnicodeError, IndexError):
            return None
        if not raw:
            return None
        argv = [item.decode("utf-8", errors="replace") for item in raw.split(b"\x00") if item]
        return {"pid": pid, "boot_id": boot, "start_ticks": start_ticks,
                "argv_sha256": hashlib.sha256(raw).hexdigest(), "argv": argv}
    try:
        completed = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                   capture_output=True, timeout=2, check=False)
        output = completed.stdout.decode("utf-8").strip()
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    if completed.returncode or not output:
        return None
    try:
        boot_result = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, timeout=2, check=False)
        boot = boot_result.stdout.decode("ascii").strip() or "darwin-boot-unknown"
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        boot = "darwin-boot-unknown"
    start, _, command = output.partition(" ")
    raw = output.encode("utf-8")
    return {"pid": pid, "boot_id": boot, "start_ticks": start,
            "argv_sha256": hashlib.sha256(raw).hexdigest(), "argv": [command or output]}


def _same_process_identity(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(expected.get(key) == actual.get(key) for key in ("pid", "boot_id", "start_ticks", "argv_sha256")) and expected.get("argv") == actual.get("argv")


def _unlink_owned_socket(path: Path) -> None:
    try:
        if path.is_socket() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def _forget_tunnel(root: Path, record_path: Path, private_dir: Path | None) -> None:
    try:
        record_path.unlink(missing_ok=True)
    except OSError:
        pass
    if private_dir:
        try:
            private_dir.rmdir()
        except OSError:
            pass


def _validate_session(session: str) -> str:
    if not isinstance(session, str) or not session or len(session) > 64 or not all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for ch in session):
        raise HerdrOpsError("invalid_session", "Herdr session names may use letters, numbers, underscores, and hyphens.")
    return session


def _validate_remote_socket(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or len(path.encode()) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise HerdrOpsError("invalid_remote_socket", "Provide an absolute remote Unix socket path no longer than 103 bytes.")
    return path


_PLAN_STEPS: dict[str, list[str]] = {
    "deploy": ["qualify effective SSH alias", "install pinned checksummed binary under ~/.local/bin", "start documented headless server on explicit socket", "verify ping/version/protocol", "register native machine profile with explicit session and label", "verify saved-machine API snapshot"],
        "upgrade": ["qualify effective SSH alias", "inspect every pane process and agent state before replacement", "return needs_interruption_authority if any occupant is active or unknown", "backup and atomically activate pinned binary", "apply and natively validate portable master config", "stop named server, start headless server, and verify ping", "verify saved-machine API snapshot"],
    "restart": ["resolve exact enabled saved profile", "relay explicit remote socket", "inspect every pane process and agent state", "return needs_interruption_authority if any occupant is active or unknown", "stop named server, start headless server, and verify ping"],
    "connect": ["resolve exact enabled saved profile", "read compatible API snapshot", "return native attach argv without launching UI"],
    "config": ["read allowlisted local master settings", "merge into remote TOML while preserving remote-only settings", "validate staged config with native herdr config check", "backup and atomically activate the validated config"],
}


def apply_remote_config(
    target: str,
    *,
    master_config: Path | None = None,
    remote_config: str | None = None,
    binary_path: str | None = None,
    apply: bool = False,
    timeout: float = 30,
) -> dict[str, Any]:
    """Merge only portable master settings on the remote host and validate natively."""
    local_path = master_config or Path(os.environ.get("HERDR_CONFIG_PATH", Path.home() / ".config" / "herdr" / "config.toml"))
    try:
        import tomllib
        master = tomllib.loads(local_path.read_text(encoding="utf-8")) if local_path.exists() else {}
    except (OSError, UnicodeDecodeError, ValueError):
        raise HerdrOpsError("master_config_invalid", "The selected local Herdr master config is not valid TOML.") from None
    if not isinstance(master, dict):
        raise HerdrOpsError("master_config_invalid", "The selected local Herdr master config must be a TOML table.")
    translation = translate_config(master)
    destination = remote_config or "~/.config/herdr/config.toml"
    if "\x00" in destination or any(ord(ch) < 32 for ch in destination):
        raise HerdrOpsError("invalid_remote_config", "Remote config path contains unsupported characters.")
    preview = {
        "target": target,
        "remote_config": destination,
        "settings": translation["settings"],
        "skipped": translation["skipped"],
        "conflicts": translation["conflicts"],
        "needs_authorization": not apply,
    }
    if not apply:
        return preview | {"planned": "validate-backup-and-atomically-activate-config"}
    resolved = _resolve_remote_config_path(target, destination, timeout=timeout)
    if binary_path is None:
        binary_path = _existing_binary_path(target, timeout=timeout)
    elif binary_path.startswith("~/"):
        binary_path = _resolve_remote_config_path(target, binary_path, timeout=timeout)
    current = _read_remote_config(target, resolved, timeout=timeout)
    try:
        remote_text = current.decode("utf-8")
        import tomllib

        remote_settings = tomllib.loads(remote_text) if remote_text else {}
    except (UnicodeDecodeError, ValueError):
        raise HerdrOpsError("remote_config_invalid", "Existing remote Herdr config is not valid UTF-8 TOML; it was left untouched.") from None
    translation = translate_config(master, remote_settings)
    try:
        staged_text = merge_config_text(remote_text, translation["settings"]) if remote_text else merge_config_text("", translation["settings"])
    except ValueError as exc:
        raise HerdrOpsError("remote_config_invalid", "Existing remote Herdr config could not be safely merged.") from exc
    owner = uuid.uuid4().hex
    stage = str(Path(resolved).with_name(f".{Path(resolved).name}.ls-herdr-{owner}.tmp"))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = f"{resolved}.backup-{stamp}-{owner[:8]}.gz"
    stage_script = 'set -eu; umask 077; mkdir -p -- "$(dirname -- "$1")"; cat > "$1"'
    staged = ssh_run(target, ["bash", "-c", stage_script, "ls-herdr-stage", stage], stdin=staged_text.encode("utf-8"), timeout=timeout)
    if staged.returncode != 0:
        raise HerdrOpsError("config_stage_failed", "Could not stage the merged remote config; the active file was not changed.")
    activate_script = (
        'set -eu; umask 077; stage=$1; dest=$2; backup=$3; binary=$4; '
        'test -f "$stage"; '
        'HERDR_CONFIG_PATH="$stage" "$binary" config check >/dev/null 2>&1 || { rm -f -- "$stage"; exit 45; }; '
        'if [ -e "$dest" ]; then gzip -c -- "$dest" > "$backup"; fi; '
        'mv -f -- "$stage" "$dest"; '
        'printf "LSHERDR_CONFIG_APPLIED\\n"'
    )
    activated = ssh_run(target, ["bash", "-c", activate_script, "ls-herdr-config", stage, resolved, backup, binary_path], timeout=timeout)
    if activated.returncode == 45:
        raise HerdrOpsError("native_check_failed", "Herdr rejected the staged config; the active remote config was left unchanged.")
    if activated.returncode != 0 or b"LSHERDR_CONFIG_APPLIED" not in activated.stdout:
        raise HerdrOpsError("config_result_unknown", "Remote config activation could not be confirmed; reconcile before retrying.", "unknown")
    return preview | {"remote_config": resolved, "conflicts": translation["conflicts"], "applied": True,
                      "backup": backup if current else None, "native_validation": "passed"}


def _resolve_remote_config_path(target: str, path: str, *, timeout: float = 30) -> str:
    if path.startswith("~/"):
        home_result = ssh_run(target, ["sh", "-c", 'printf "%s\\n" "$HOME"'], timeout=timeout)
        if home_result.returncode:
            raise HerdrOpsError("remote_home_unavailable", "Could not resolve the SSH user's home directory.")
        try:
            home = home_result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            home = ""
        if not home.startswith("/") or "\x00" in home or any(ord(ch) < 32 for ch in home):
            raise HerdrOpsError("remote_home_invalid", "SSH target returned an invalid home directory.")
        path = home + path[1:]
    if not path.startswith("/") or "\x00" in path or any(ord(ch) < 32 for ch in path):
        raise HerdrOpsError("invalid_remote_config", "Remote config and binary paths must be absolute paths.")
    return path


def _read_remote_config(target: str, path: str, *, timeout: float = 30) -> bytes:
    script = 'if [ -f "$1" ]; then cat -- "$1"; else exit 44; fi'
    result = ssh_run(target, ["sh", "-c", script, "ls-herdr-cat", path], timeout=timeout)
    if result.returncode == 44:
        return b""
    if result.returncode:
        raise HerdrOpsError("remote_config_unreadable", "Could not read the explicitly selected remote config; it was left untouched.")
    return result.stdout


def _remote_session_inventory(target: str, *, timeout: float) -> list[dict[str, Any]]:
    """Read native account sessions, including sessions without a local profile."""
    script = (
        'set -eu; '
        'if [ -x "$HOME/.local/bin/herdr" ]; then "$HOME/.local/bin/herdr" session list --json; '
        'elif command -v herdr >/dev/null 2>&1; then herdr session list --json; '
        'elif [ -e "$HOME/.config/herdr/herdr.sock" ] || [ -d "$HOME/.config/herdr/sessions" ]; then exit 42; '
        'else printf \'{"sessions":[]}\\n\'; fi'
    )
    result = ssh_run(target, ["sh", "-c", script], timeout=timeout)
    if result.returncode:
        raise HerdrOpsError("remote_session_inventory_unknown", "Could not read the native remote session inventory; shared files were not changed.")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise HerdrOpsError("remote_session_inventory_unknown", "Native remote session inventory was not valid JSON.") from None
    rows = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise HerdrOpsError("remote_session_inventory_unknown", "Native remote session inventory had no sessions list.")
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("running"), bool):
            raise HerdrOpsError("remote_session_inventory_unknown", "Native remote session inventory contains an invalid row.")
        name = _validate_session(row["name"])
        if name in seen:
            raise HerdrOpsError("remote_session_inventory_unknown", "Native remote session inventory repeats a session name.")
        seen.add(name)
        socket = row.get("socket_path")
        if not isinstance(socket, str):
            raise HerdrOpsError("remote_session_inventory_unknown", "Native remote session inventory omitted a socket path.")
        inventory.append({"session": name, "running": row["running"], "remote_socket": _validate_remote_socket(socket)})
    return inventory


def _account_profiles(profiles: Sequence[Mapping[str, Any]], target: str) -> list[dict[str, Any]]:
    requested = _qualify_ssh_target(target)
    identity = tuple(requested.get(key) for key in ("hostname", "user", "port"))
    same: list[dict[str, Any]] = []
    for profile in profiles:
        alias = profile.get("ssh_target")
        if not isinstance(alias, str):
            continue
        effective = requested if alias == target else _qualify_ssh_target(alias)
        if tuple(effective.get(key) for key in ("hostname", "user", "port")) == identity:
            same.append(dict(profile))
    return same


def _observe_shared_sessions(
    target: str, profiles: Sequence[Mapping[str, Any]], *, selected_session: str,
    selected_socket: str | None, include_selected: bool, state_dir: str | None,
    timeout: float,
) -> list[dict[str, Any]]:
    inventory = {row["session"]: row for row in _remote_session_inventory(target, timeout=timeout)}
    profile_by_session: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        name = _validate_session(str(profile["remote_session"]))
        if name in profile_by_session:
            raise HerdrOpsError("profile_ambiguous", "Several saved profiles name the same remote session.")
        profile_by_session[name] = profile
    names = set(inventory) | set(profile_by_session)
    if include_selected:
        names.add(selected_session)
    else:
        names.discard(selected_session)
    observations: list[dict[str, Any]] = []
    for name in sorted(names):
        native = inventory.get(name)
        profile = profile_by_session.get(name)
        socket = selected_socket if name == selected_session and selected_socket else (
            native["remote_socket"] if native else _session_socket(target, name, None, timeout=timeout))
        if native is not None and not native["running"] and name != selected_session:
            observations.append({"session": name, "remote_socket": socket,
                                 "profile": _public_profile(profile) if profile else None,
                                 "running": False, "occupants": []})
            continue
        endpoint, tunnel_owner = _open_profile_endpoint(target, socket, state_dir)
        try:
            try:
                compatibility = _ping(endpoint)
                running = True
            except HerdrOpsError as exc:
                compatibility = {"state": "unknown" if exc.code == "server_unavailable" else "incompatible", "reason": exc.code}
                running = exc.code != "server_unavailable" or bool(native and native["running"])
            occupants = _inspect_occupants(endpoint) if running else []
            if native and native["running"] and not occupants:
                occupants = [{"pane_id": None, "state": "unknown", "reason": "native-running-without-snapshot"}]
            observations.append({"session": name, "remote_socket": socket,
                                 "profile": _public_profile(profile) if profile else None,
                                 "running": running, "compatibility": compatibility, "occupants": occupants})
        finally:
            close_tunnel(tunnel_owner, state_dir=state_dir)
    return observations


def _begin_operation(kind: str, target: str, sessions: Sequence[str], state_dir: str | None) -> dict[str, Any]:
    record = {"schema_version": 1, "operation_id": uuid.uuid4().hex, "kind": kind,
              "target": target, "sessions": list(sessions), "state": "running",
              "phase": "preflight_complete", "binary": None, "config": None,
              "restarted": [], "updated_at": time.time()}
    _save_operation(record, state_dir)
    return record


def _save_operation(record: dict[str, Any], state_dir: str | None) -> None:
    root = state_directory(state_dir)
    record["updated_at"] = time.time()
    with writer_lock(root):
        try:
            atomic_write_json(root / "remote-operations" / f"{record['operation_id']}.json", record)
        except (OSError, HerdrOpsError):
            raise HerdrOpsError("operation_record_unknown", "Could not durably record the remote lifecycle phase; reconcile before retrying.", "unknown") from None


def _operation_phase(record: dict[str, Any], state_dir: str | None, phase: str, **values: Any) -> None:
    record.update(values)
    record["phase"] = phase
    _save_operation(record, state_dir)


def _operation_failure(record: dict[str, Any], state_dir: str | None, exc: HerdrOpsError) -> HerdrOpsError:
    record["state"] = "unknown" if exc.effect == "unknown" else "failed"
    record["error"] = exc.as_dict()
    _save_operation(record, state_dir)
    return HerdrOpsError(exc.code, exc.message, exc.effect, operation_id=str(record["operation_id"]))


def _remote_step(record: dict[str, Any], state_dir: str | None, phase: str,
                 action: Callable[[], Any], *, field: str | None = None) -> Any:
    try:
        result = action()
    except HerdrOpsError as exc:
        raise _operation_failure(record, state_dir, exc) from exc
    values = {field: result} if field else {}
    _operation_phase(record, state_dir, phase, **values)
    return result


def _close_operation_tunnel(record: dict[str, Any], state_dir: str | None, owner: str) -> None:
    try:
        close_tunnel(owner, state_dir=state_dir)
    except HerdrOpsError as exc:
        raise _operation_failure(record, state_dir, exc) from exc


def _install_command(args: argparse.Namespace, manifest_path: Path, *, operation: str) -> dict[str, Any]:
    target = validate_ssh_target(args.target)
    session = _validate_session(getattr(args, "session", None) or "default")
    label = args.label or session
    if not label or len(label) > 128 or label.startswith("-") or any(ord(ch) < 32 for ch in label):
        raise HerdrOpsError("invalid_profile_label", "Machine labels must be non-empty single-line text up to 128 characters.")
    if not args.apply:
        return lifecycle_plan(operation, target, session=session, label=label, remote_socket=args.remote_socket) | {
            "manifest": str(manifest_path),
            "config": "portable master settings included by default",
        }
    timeout = float(args.timeout)
    if not 0 < timeout <= 600:
        raise HerdrOpsError("invalid_timeout", "Lifecycle timeout must be between 0 and 600 seconds.")
    profiles = list_profiles()
    account_profiles = _account_profiles(profiles, target)
    matching = [p for p in account_profiles if p.get("remote_session") == session]
    if operation == "upgrade":
        if len(matching) != 1 or not matching[0].get("enabled"):
            raise HerdrOpsError("profile_required", "Upgrade requires one exact enabled saved profile for this SSH target and session.")
        profile = matching[0]
        # Every saved session for this SSH account shares the binary and the
        # default config. Inspect all of them before any account-wide write.
        selected_socket = _session_socket(target, session, args.remote_socket, timeout=timeout)
        observations = _observe_shared_sessions(
            target, account_profiles, selected_session=session, selected_socket=selected_socket,
            include_selected=True, state_dir=getattr(args, "state_dir", None), timeout=timeout)
        affected = [{"session": row["session"], **occupant} for row in observations
                    for occupant in row["occupants"] if occupant["state"] != "idle"]
        scope = {"ssh_target": target, "sessions": [row["session"] for row in observations]}
        if affected and not args.allow_interruption:
            return {"operation": "upgrade", "state": "needs_interruption_authority",
                    "scope": scope, "affected": affected, "binary_replaced": False,
                    "observations": observations}
        release = _pinned_release(manifest_path, target)
        op_state = getattr(args, "state_dir", None)
        op_record = _begin_operation("upgrade", target, scope["sessions"], op_state)
        install_receipt = _remote_step(op_record, op_state, "binary_installed",
                                       lambda: _install_binary(target, release, operation="upgrade", timeout=timeout), field="binary")
        try:
            config_receipt = apply_remote_config(target, master_config=args.master_config, remote_config=None,
                                                 binary_path=_user_binary_path(target), apply=True, timeout=timeout)
        except HerdrOpsError as exc:
            if exc.effect == "none":
                _remote_step(op_record, op_state, "binary_rolled_back",
                             lambda: _rollback_binary(target, install_receipt, timeout=timeout))
                op_record["binary"]["restored"] = True
            raise _operation_failure(op_record, op_state, exc) from exc
        _operation_phase(op_record, op_state, "config_applied",
                         config={key: config_receipt.get(key) for key in ("remote_config", "backup", "native_validation")})
        restarted: list[dict[str, Any]] = []
        for row in observations:
            if not row["running"] and row["session"] != session:
                continue
            name, socket = row["session"], row["remote_socket"]
            if row["running"]:
                _remote_step(op_record, op_state, f"server_stopped:{name}",
                             lambda: _stop_named_server(target, name, socket,
                                 binary=install_receipt.get("backup_path") or install_receipt.get("old_binary_path") or _user_binary_path(target), timeout=timeout))
                _remote_step(op_record, op_state, f"socket_absent:{name}",
                             lambda: _wait_remote_socket_absent(target, socket, timeout=min(timeout, 30)))
            started = _remote_step(op_record, op_state, f"server_started:{name}",
                                   lambda: _start_headless_server(target, name, socket, _user_binary_path(target), timeout=timeout))
            endpoint, tunnel_owner = _remote_step(op_record, op_state, f"relay_opened:{name}",
                                                  lambda: _open_profile_endpoint(target, socket, op_state))
            try:
                verified = _remote_step(op_record, op_state, f"server_verified:{name}",
                                        lambda: _wait_ping(endpoint, manifest_path=manifest_path, timeout=min(timeout, 30)))
            finally:
                _close_operation_tunnel(op_record, op_state, tunnel_owner)
            saved = _remote_step(op_record, op_state, f"saved_profile_checked:{name}",
                                 lambda: inspect_profile(str(row["profile"]["id"]))) if row["profile"] else None
            if saved is not None and saved.get("connectivity") != "connected":
                raise _operation_failure(op_record, op_state, HerdrOpsError("reconnect_unconfirmed", "A restarted saved machine did not reconnect; reconcile all affected sessions.", "unknown"))
            restarted.append({"session": name, "server": started, "ping": verified,
                              "profile": saved["profile"] if saved else None})
            _operation_phase(op_record, op_state, f"session_complete:{name}", restarted=[item["session"] for item in restarted])
        _operation_phase(op_record, op_state, "verified", state="verified")
        return {"operation": "upgrade", "state": "verified", "scope": scope,
                "operation_id": op_record["operation_id"],
                "binary": install_receipt, "config": {key: value for key, value in config_receipt.items() if key != "settings"},
                "restarted": restarted, "affected": affected}

    if matching:
        return {"operation": "deploy", "state": "profile_already_exists", "profile": _public_profile(matching[0]),
                "next": "use remote upgrade to preserve idle-first restart checks"}
    remote_socket = _session_socket(target, session, args.remote_socket, timeout=timeout)
    probe: dict[str, Any] | None = None
    tunnel_owner: str | None = None
    try:
        endpoint, tunnel_owner = _open_profile_endpoint(target, remote_socket, getattr(args, "state_dir", None))
        probe = _ping(endpoint, manifest_path=manifest_path)
    except HerdrOpsError as exc:
        if exc.effect == "unknown":
            raise
        # A newly provisioned session has no socket yet. Continue only after a
        # read-only probe failed; never stop, remove, or replace an existing path.
    if probe:
        if tunnel_owner:
            close_tunnel(tunnel_owner, state_dir=getattr(args, "state_dir", None))
        op_state = getattr(args, "state_dir", None)
        op_record = _begin_operation("adopt", target, [session], op_state)
        registered = _remote_step(op_record, op_state, "profile_registered",
                                  lambda: _register_profile(target, session, label))
        verification = _remote_step(op_record, op_state, "profile_checked",
                                    lambda: inspect_profile(str(registered["id"])))
        if verification.get("connectivity") != "connected":
            raise _operation_failure(op_record, op_state, HerdrOpsError("profile_verify_failed", "The running server was registered but its saved profile did not reconnect.", "unknown"))
        _operation_phase(op_record, op_state, "verified", state="verified")
        return {"operation": "deploy", "state": "adopted", "compatibility": probe,
                "profile": verification["profile"], "connectivity": verification,
                "operation_id": op_record["operation_id"]}
    if tunnel_owner:
        close_tunnel(tunnel_owner, state_dir=getattr(args, "state_dir", None))
    if _remote_path_exists(target, remote_socket, timeout=timeout):
        return {"operation": "deploy", "state": "server_state_unknown", "remote_socket": remote_socket,
                "needs_interruption_authority": True, "binary_replaced": False,
                "reason": "the documented socket path exists but ping did not verify a compatible server"}
    # A new named session still shares the account-wide binary and config
    # with every saved session already running on this SSH target.
    observations = _observe_shared_sessions(
        target, account_profiles, selected_session=session, selected_socket=None,
        include_selected=False, state_dir=getattr(args, "state_dir", None), timeout=timeout)
    affected = [{"session": row["session"], **item} for row in observations
                for item in row["occupants"] if item["state"] != "idle"]
    if affected and not args.allow_interruption:
        return {"operation": "deploy", "state": "needs_interruption_authority",
                "scope": {"ssh_target": target, "sessions": [session, *[row["session"] for row in observations]]},
                "affected": affected, "binary_replaced": False}
    release = _pinned_release(manifest_path, target)
    op_state = getattr(args, "state_dir", None)
    op_record = _begin_operation("deploy", target, [session, *[row["session"] for row in observations]], op_state)
    install_receipt = _remote_step(op_record, op_state, "binary_installed",
                                   lambda: _install_binary(target, release, operation="deploy", timeout=timeout), field="binary")
    try:
        config_receipt = apply_remote_config(target, master_config=args.master_config, remote_config=None,
                                             binary_path=_user_binary_path(target), apply=True, timeout=timeout)
    except HerdrOpsError as exc:
        if exc.effect == "none":
            _remote_step(op_record, op_state, "binary_rolled_back",
                         lambda: _rollback_binary(target, install_receipt, timeout=timeout))
            op_record["binary"]["restored"] = True
        raise _operation_failure(op_record, op_state, exc) from exc
    _operation_phase(op_record, op_state, "config_applied",
                     config={key: config_receipt.get(key) for key in ("remote_config", "backup", "native_validation")})
    start_receipt = _remote_step(op_record, op_state, f"server_started:{session}",
                                 lambda: _start_headless_server(target, session, remote_socket, _user_binary_path(target), timeout=timeout))
    endpoint, tunnel_owner = _remote_step(op_record, op_state, f"relay_opened:{session}",
                                          lambda: _open_profile_endpoint(target, remote_socket, op_state))
    try:
        _remote_step(op_record, op_state, f"server_verified:{session}",
                     lambda: _wait_ping(endpoint, manifest_path=manifest_path, timeout=min(timeout, 30)))
        _remote_step(op_record, op_state, "profile_registered",
                     lambda: _register_profile(target, session, label))
        profile = _remote_step(op_record, op_state, "profile_found", lambda: find_profile(label))
        verified = _remote_step(op_record, op_state, "profile_verified",
                                lambda: _verify_profile(profile, install_receipt, config_receipt, start_receipt))
    finally:
        _close_operation_tunnel(op_record, op_state, tunnel_owner)
    restarted: list[str] = []
    for row in observations:
        if not row["running"]:
            continue
        name, socket = row["session"], row["remote_socket"]
        _remote_step(op_record, op_state, f"server_stopped:{name}",
                     lambda: _stop_named_server(target, name, socket,
                         binary=install_receipt.get("backup_path") or install_receipt.get("old_binary_path") or _user_binary_path(target), timeout=timeout))
        _remote_step(op_record, op_state, f"socket_absent:{name}",
                     lambda: _wait_remote_socket_absent(target, socket, timeout=min(timeout, 30)))
        _remote_step(op_record, op_state, f"server_started:{name}",
                     lambda: _start_headless_server(target, name, socket, _user_binary_path(target), timeout=timeout))
        endpoint, tunnel_owner = _remote_step(op_record, op_state, f"relay_opened:{name}",
                                              lambda: _open_profile_endpoint(target, socket, op_state))
        try:
            _remote_step(op_record, op_state, f"server_verified:{name}",
                         lambda: _wait_ping(endpoint, manifest_path=manifest_path, timeout=min(timeout, 30)))
        finally:
            _close_operation_tunnel(op_record, op_state, tunnel_owner)
        saved = _remote_step(op_record, op_state, f"saved_profile_checked:{name}",
                             lambda: inspect_profile(str(row["profile"]["id"]))) if row["profile"] else None
        if saved is not None and saved.get("connectivity") != "connected":
            raise _operation_failure(op_record, op_state, HerdrOpsError("reconnect_unconfirmed", "A saved machine did not reconnect after shared binary replacement; reconcile before retrying.", "unknown"))
        restarted.append(name)
        _operation_phase(op_record, op_state, f"session_complete:{name}", restarted=list(restarted))
    _operation_phase(op_record, op_state, "verified", state="verified")
    return verified | {"restarted_shared_sessions": restarted, "affected": affected,
                       "operation_id": op_record["operation_id"]}


def _pinned_release(manifest_path: Path, target: str) -> dict[str, str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise HerdrOpsError("manifest_invalid", "The reviewed Herdr provenance manifest cannot be read.") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise HerdrOpsError("manifest_invalid", "The reviewed Herdr provenance manifest is unsupported.")
    release = manifest.get("release")
    if not isinstance(release, dict) or not isinstance(release.get("assets"), dict) or not isinstance(release.get("sha256"), dict):
        raise HerdrOpsError("manifest_invalid", "The reviewed Herdr release asset pins are missing.")
    platform_name = _remote_platform(target)
    url = release["assets"].get(platform_name)
    digest = release["sha256"].get(platform_name)
    version = release.get("version")
    if not isinstance(url, str) or not isinstance(digest, str) or not isinstance(version, str):
        raise HerdrOpsError("unsupported_remote_platform", "No pinned Herdr asset is available for the remote platform.")
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path.startswith("/herdrdev/herdr/releases/download/"):
        raise HerdrOpsError("manifest_invalid", "Pinned Herdr asset URL is not an official HTTPS release asset.")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise HerdrOpsError("manifest_invalid", "Pinned Herdr asset SHA-256 is malformed.")
    try:
        from .provenance import check_provenance
        checked = check_provenance(manifest_path.parent, manifest_path)
    except Exception:
        raise HerdrOpsError("manifest_invalid", "Could not verify source pins before remote installation.") from None
    if checked["source"]["status"] != "ok" or checked["schema"]["status"] != "ok":
        raise HerdrOpsError("manifest_drift", "Pinned upstream source/schema files drift from the reviewed manifest.")
    return {"platform": platform_name, "url": url, "sha256": digest, "version": version}


def _remote_platform(target: str) -> str:
    result = ssh_run(target, ["sh", "-c", "uname -s; uname -m"])
    if result.returncode:
        raise HerdrOpsError("platform_probe_failed", "Could not identify the explicit SSH target platform.")
    try:
        system, machine = result.stdout.decode("ascii").splitlines()[:2]
    except (UnicodeDecodeError, ValueError):
        raise HerdrOpsError("platform_probe_failed", "SSH target returned an unsupported platform response.") from None
    machines = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
    family = {"Linux": "linux", "Darwin": "macos"}.get(system)
    if not family or machine.lower() not in machines:
        raise HerdrOpsError("unsupported_remote_platform", "Pinned binaries are available only for supported Linux and macOS architectures.")
    return f"{family}-{machines[machine.lower()]}"


def _install_binary(target: str, release: Mapping[str, str], *, operation: str, timeout: float) -> dict[str, Any]:
    owner = uuid.uuid4().hex
    script = r'''set -eu
mode=$1; url=$2; expected=$3; version=$4; owner=$5
bin="$HOME/.local/bin/herdr"; state="$HOME/.local/state/localsetup/herdr-ops"; receipt="$state/binary.sha256"
hash_file() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else return 127; fi; }
had=0; old_hash=; old_path=$(command -v herdr 2>/dev/null || :)
if [ -e "$bin" ]; then
  [ -f "$bin" ] || { printf 'ERR:unowned_install\n'; exit 20; }
  [ -f "$receipt" ] || { printf 'ERR:unowned_install\n'; exit 20; }
  old_hash=$(hash_file "$bin") || { printf 'ERR:hash_tool_missing\n'; exit 21; }
  [ "$(cat "$receipt")" = "localsetup-managed:$old_hash" ] || { printf 'ERR:ownership_mismatch\n'; exit 22; }
  had=1
elif [ -n "$old_path" ]; then
  printf 'ERR:unsupported_existing_install\n'; exit 23
fi
mkdir -p "$HOME/.local/bin" "$state"; chmod 700 "$state"
tmp=$(mktemp -d "$state/install.XXXXXX") || { printf 'ERR:temporary_storage_failed\n'; exit 24; }
stage="$HOME/.local/bin/.herdr-$owner"
trap 'rm -rf "$tmp"; rm -f "$stage"' EXIT HUP INT TERM
if command -v curl >/dev/null 2>&1; then curl --fail --location --proto '=https' --tlsv1.2 --silent --show-error "$url" -o "$tmp/herdr" 2>/dev/null || { printf 'ERR:download_failed\n'; exit 25; }
elif command -v wget >/dev/null 2>&1; then wget -q "$url" -O "$tmp/herdr" 2>/dev/null || { printf 'ERR:download_failed\n'; exit 25; }
else printf 'ERR:downloader_missing\n'; exit 26; fi
actual=$(hash_file "$tmp/herdr") || { printf 'ERR:hash_tool_missing\n'; exit 21; }
[ "$actual" = "$expected" ] || { printf 'ERR:checksum_mismatch\n'; exit 27; }
chmod 700 "$tmp/herdr"
version_output=$("$tmp/herdr" --version 2>/dev/null) || { printf 'ERR:version_check_failed\n'; exit 28; }
case "$version_output" in *"$version"*) ;; *) printf 'ERR:version_mismatch\n'; exit 29;; esac
install -m 700 "$tmp/herdr" "$stage" || { printf 'ERR:stage_failed\n'; exit 30; }
backup=
if [ "$had" = 1 ]; then stamp=$(date -u +%Y%m%dT%H%M%SZ); short_hash=$(printf '%s' "$old_hash" | cut -c1-8); backup="$bin.backup-$stamp-$short_hash"; cp -p "$bin" "$backup" || { printf 'ERR:backup_failed\n'; exit 31; }; fi
mv -f "$stage" "$bin" || { printf 'ERR:activate_failed\n'; exit 32; }
printf 'localsetup-managed:%s\n' "$expected" > "$receipt.tmp-$owner"
chmod 600 "$receipt.tmp-$owner"; mv -f "$receipt.tmp-$owner" "$receipt"
printf 'OK\n%s\n%s\n%s\n%s\n' "$version" "$backup" "$old_hash" "$old_path"
'''
    result = ssh_run(target, ["sh", "-s", "--", operation,
                              release["url"], release["sha256"], release["version"], owner],
                     stdin=script.encode("utf-8"), timeout=max(timeout, 120))
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    if result.returncode != 0 or not lines or lines[0] != "OK":
        code = lines[0][4:] if lines and lines[0].startswith("ERR:") else "install_result_unknown"
        effect = "none" if code in {"unowned_install", "ownership_mismatch", "unsupported_existing_install", "temporary_storage_failed", "download_failed", "downloader_missing", "hash_tool_missing", "checksum_mismatch", "version_check_failed", "version_mismatch", "stage_failed", "backup_failed"} else "unknown"
        message = ("A Herdr binary is already provided by a different installer; upgrade it through that owning installer or migrate it explicitly before using managed deployment."
                   if code == "unsupported_existing_install" else
                   "Pinned remote binary installation did not complete; reconcile the target before retrying.")
        raise HerdrOpsError(code, message, effect)
    backup_path = lines[2] if len(lines) > 2 else ""
    return {"platform": release["platform"], "version": release["version"],
            "user_level_binary": "~/.local/bin/herdr", "backup_created": bool(backup_path),
            "backup_path": backup_path or None, "old_sha256": lines[3] if len(lines) > 3 and lines[3] else None,
            "sha256": release["sha256"], "old_binary_path": lines[4] if len(lines) > 4 and lines[4].startswith("/") else None}


def _rollback_binary(target: str, install: Mapping[str, Any], *, timeout: float) -> None:
    """Restore only the exact binary installed by this operation after a proven pre-restart failure."""
    script = r'''set -eu
expected=$1; backup=${2-}; old=${3-}
bin="$HOME/.local/bin/herdr"; receipt="$HOME/.local/state/localsetup/herdr-ops/binary.sha256"
hash_file() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
[ -f "$bin" ] && [ -f "$receipt" ] || exit 40
[ "$(hash_file "$bin")" = "$expected" ] && [ "$(cat "$receipt")" = "localsetup-managed:$expected" ] || exit 41
if [ -n "$backup" ]; then
  [ -f "$backup" ] && [ "$(hash_file "$backup")" = "$old" ] || exit 42
  stage="$bin.rollback-$$"; cp -p "$backup" "$stage"; mv -f "$stage" "$bin"
  printf 'localsetup-managed:%s\n' "$old" > "$receipt.tmp-$$"; mv -f "$receipt.tmp-$$" "$receipt"
else
  rm -f -- "$bin" "$receipt"
fi
printf 'LSHERDR_BINARY_ROLLED_BACK\n'
'''
    backup = install.get("backup_path")
    rollback_argv = ["sh", "-s", "--", str(install["sha256"])]
    if backup:
        old_sha256 = install.get("old_sha256")
        if not isinstance(old_sha256, str) or not old_sha256:
            raise HerdrOpsError("binary_rollback_unknown", "Previous binary checksum is missing; reconcile before retrying.", "unknown")
        rollback_argv.extend((str(backup), old_sha256))
    result = ssh_run(target, rollback_argv, stdin=script.encode("utf-8"), timeout=timeout)
    if result.returncode != 0 or b"LSHERDR_BINARY_ROLLED_BACK" not in result.stdout:
        raise HerdrOpsError("binary_rollback_unknown", "Could not confirm restoration of the previous managed binary; reconcile before retrying.", "unknown")


def _user_binary_path(target: str) -> str:
    return _resolve_remote_config_path(target, "~/.local/bin/herdr")


def _existing_binary_path(target: str, *, timeout: float) -> str:
    script = ('if [ -x "$HOME/.local/bin/herdr" ]; then '
              'printf "%s\\n" "$HOME/.local/bin/herdr"; else command -v herdr; fi')
    result = ssh_run(target, ["sh", "-c", script], timeout=timeout)
    try:
        path = result.stdout.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError:
        path = ""
    if result.returncode or not path.startswith("/"):
        raise HerdrOpsError("binary_unavailable", "Could not resolve an existing remote Herdr executable; no mutation was sent.")
    validate_remote_argv([path])
    return path


def _session_socket(target: str, session: str, explicit: str | None, *, timeout: float = 30) -> str:
    if explicit is not None:
        return _validate_remote_socket(_resolve_remote_config_path(target, explicit, timeout=timeout))
    suffix = ".config/herdr/herdr.sock" if session == "default" else f".config/herdr/sessions/{session}/herdr.sock"
    return _validate_remote_socket(_resolve_remote_config_path(target, "~/" + suffix, timeout=timeout))


def _remote_path_exists(target: str, path: str, *, timeout: float = 30) -> bool:
    result = ssh_run(target, ["test", "-e", path], timeout=timeout)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise HerdrOpsError("remote_path_check_failed", "Could not inspect the explicit remote socket path safely.")


def _wait_remote_socket_absent(target: str, path: str, *, timeout: float) -> None:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if not _remote_path_exists(target, path, timeout=min(5, timeout)):
            return
        time.sleep(0.2)
    raise HerdrOpsError("server_stop_unknown", "The named server socket remains after stop; reconcile before starting it again.", "unknown")


def _start_headless_server(target: str, session: str, remote_socket: str, binary_path: str, *, timeout: float = 30) -> dict[str, Any]:
    session = _validate_session(session)
    socket_path = _validate_remote_socket(remote_socket)
    binary = _resolve_remote_config_path(target, binary_path, timeout=timeout) if binary_path.startswith("~/") else binary_path
    owner = uuid.uuid4().hex
    script = (
        'set -eu; session=$1; socket=$2; binary=$3; owner=$4; '
        'if [ -e "$socket" ]; then printf "ERR:socket_exists\\n"; exit 40; fi; '
        'mkdir -p -- "$(dirname -- "$socket")"; '
        'nohup env HERDR_SESSION="$session" HERDR_SOCKET_PATH="$socket" LS_HERDR_OWNER_ID="$owner" "$binary" server </dev/null >/dev/null 2>&1 & '
        'printf "LSHERDR_SERVER_DISPATCHED\\n"'
    )
    result = ssh_run(target, ["sh", "-c", script, "ls-herdr-start", session, socket_path, binary, owner], timeout=timeout)
    if result.returncode == 40:
        raise HerdrOpsError("socket_exists", "The explicit server socket already exists; it was left untouched.")
    if result.returncode != 0 or b"LSHERDR_SERVER_DISPATCHED" not in result.stdout:
        raise HerdrOpsError("server_start_unknown", "Headless server start could not be confirmed; inspect the explicit socket before retrying.", "unknown")
    return {"session": session, "remote_socket": socket_path, "server": "headless-start-dispatched", "owner_id": owner}


def _stop_named_server(target: str, session: str, remote_socket: str, *, binary: str, timeout: float = 30) -> None:
    result = ssh_run(target, ["env", f"HERDR_SESSION={session}", f"HERDR_SOCKET_PATH={remote_socket}", binary, "server", "stop"], timeout=timeout)
    if result.returncode:
        raise HerdrOpsError("server_stop_failed", "Native Herdr server stop failed; reconcile the session before any retry.", "unknown")


def _open_profile_endpoint(target: str, remote_socket: str, state_dir: str | None) -> tuple[str, str]:
    opened = open_tunnel(target, remote_socket, state_dir=state_dir)
    return str(opened["local_endpoint"]), str(opened["owner_id"])


def _ping(endpoint: str, *, manifest_path: Path | None = None) -> dict[str, Any]:
    try:
        response = request(endpoint, "ping", {})
    except HerdrOpsError as exc:
        raise HerdrOpsError("server_unavailable", "Herdr ping failed; the target was not restarted.", "none") from exc
    capabilities = response.get("capabilities")
    if not isinstance(capabilities, dict):
        raise HerdrOpsError("server_incompatible", "Herdr ping returned no capability map.")
    version, protocol = response.get("version"), response.get("protocol")
    generation = capabilities.get("endpoint_protocol_generation")
    if not isinstance(version, str) or not isinstance(protocol, int) or not isinstance(generation, int):
        raise HerdrOpsError("server_incompatible", "Herdr ping did not report version, protocol, and endpoint generation.")
    if manifest_path:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        release = manifest["release"]
        if protocol != release["protocol"] or generation < release["endpoint_generation"]:
            raise HerdrOpsError("server_incompatible", "Remote server protocol or endpoint generation is incompatible with the pinned client.")
    return {"version": version, "protocol": protocol, "endpoint_generation": generation}


def _wait_ping(endpoint: str, *, manifest_path: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout)
    last: HerdrOpsError | None = None
    while time.monotonic() < deadline:
        try:
            return _ping(endpoint, manifest_path=manifest_path)
        except HerdrOpsError as exc:
            if exc.code == "server_incompatible":
                raise
            last = exc
            time.sleep(0.2)
    raise HerdrOpsError("server_verify_unknown", "Headless server did not return a compatible ping; do not replay the start command.", "unknown") from last


def _inspect_occupants(endpoint: str) -> list[dict[str, Any]]:
    try:
        snapshot = _snapshot_body(request(endpoint, "session.snapshot", {}))
    except HerdrOpsError:
        return [{"pane_id": None, "state": "unknown", "reason": "session-snapshot-unavailable"}]
    panes = snapshot.get("panes") if isinstance(snapshot, dict) else None
    agents = snapshot.get("agents") if isinstance(snapshot, dict) else None
    if not isinstance(panes, list):
        return [{"pane_id": None, "state": "unknown", "reason": "pane-inventory-unavailable"}]
    if not isinstance(agents, list):
        return [{"pane_id": None, "state": "unknown", "reason": "agent-inventory-unavailable"}]
    agent_states = {
        item.get("pane_id"): item.get("agent_status", item.get("state"))
        for item in agents
        if isinstance(item, dict) and isinstance(item.get("pane_id"), str)
    }
    occupancy: list[dict[str, Any]] = []
    for pane in panes:
        if not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str):
            occupancy.append({"pane_id": None, "state": "unknown", "reason": "invalid-pane-row"})
            continue
        pane_id = pane["pane_id"]
        try:
            raw = request(endpoint, "pane.process_info", {"pane_id": pane_id})
        except HerdrOpsError:
            occupancy.append({"pane_id": pane_id, "state": "unknown", "reason": "process-info-unavailable"})
            continue
        info = raw.get("process_info") if isinstance(raw.get("process_info"), dict) else raw
        processes = info.get("foreground_processes") if isinstance(info, dict) else None
        shell_pid = info.get("shell_pid") if isinstance(info, dict) else None
        if not isinstance(processes, list) or not isinstance(shell_pid, int):
            occupancy.append({"pane_id": pane_id, "state": "unknown", "reason": "process-info-incomplete"})
            continue
        non_shell = [
            process for process in processes
            if not isinstance(process, dict) or process.get("pid") != shell_pid
        ]
        agent_state = agent_states.get(pane_id)
        if non_shell or agent_state in {"working", "blocked", "unknown"}:
            reason = "foreground-process" if non_shell else f"agent-{agent_state}"
            occupancy.append({"pane_id": pane_id, "state": "active", "reason": reason})
        elif pane_id not in agent_states or agent_state in {"idle", "done"}:
            occupancy.append({"pane_id": pane_id, "state": "idle", "reason": "shell-only"})
        else:
            occupancy.append({"pane_id": pane_id, "state": "unknown", "reason": "agent-state-unrecognized"})
    return occupancy


def restart_profile(
    selector: str,
    *,
    remote_socket: str | None = None,
    apply: bool = False,
    allow_interruption: bool = False,
    timeout: float = 30,
    state_dir: str | None = None,
) -> dict[str, Any]:
    if not 0 < timeout <= 600:
        raise HerdrOpsError("invalid_timeout", "Restart timeout must be between 0 and 600 seconds.")
    profile = find_profile(selector)
    target = str(profile["ssh_target"])
    session = _validate_session(str(profile["remote_session"]))
    socket_path = _session_socket(target, session, remote_socket, timeout=timeout)
    endpoint, tunnel_owner = _open_profile_endpoint(target, socket_path, state_dir)
    try:
        compatibility = _ping(endpoint)
        occupants = _inspect_occupants(endpoint)
        affected = [item for item in occupants if item["state"] != "idle"]
        scope = {"ssh_target": target, "profile_id": profile["id"], "session": session, "remote_socket": socket_path}
        if affected and not allow_interruption:
            return {
                "operation": "restart",
                "state": "needs_interruption_authority",
                "scope": scope,
                "affected": affected,
                "compatibility": compatibility,
                "server_stopped": False,
            }
        if not apply:
            return {
                "operation": "restart",
                "state": "planned",
                "scope": scope,
                "occupants": occupants,
                "compatibility": compatibility,
                "needs_authorization": True,
                "interruption_authority_supplied": bool(allow_interruption),
            }
        binary = _existing_binary_path(target, timeout=timeout)
        _stop_named_server(target, session, socket_path, binary=binary, timeout=timeout)
        started = _start_headless_server(target, session, socket_path, binary, timeout=timeout)
        verified = _wait_ping(endpoint, manifest_path=default_manifest_path(Path(__file__).resolve().parents[3]), timeout=min(timeout, 30))
        saved_snapshot = inspect_profile(str(profile["id"]))
        if saved_snapshot.get("connectivity") != "connected":
            raise HerdrOpsError("reconnect_unconfirmed", "Server ping passed but the saved machine could not reconnect; inspect before retrying.", "unknown")
        return {
            "operation": "restart",
            "state": "restarted",
            "scope": scope,
            "compatibility": verified,
            "server": started,
            "reconnect": saved_snapshot,
            "affected": affected,
        }
    finally:
        close_tunnel(tunnel_owner, state_dir=state_dir)


def _register_profile(target: str, session: str, label: str) -> dict[str, Any]:
    settings = _qualify_ssh_target(target)
    if settings.get("stricthostkeychecking", "").lower() != "yes":
        raise HerdrOpsError("ssh_host_key_policy", "Profile registration requires StrictHostKeyChecking=yes in the effective SSH config.")
    existing = list_profiles()
    matches = [p for p in existing if p.get("ssh_target") == target and p.get("remote_session") == session]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 or any(p.get("label") == label for p in existing):
        raise HerdrOpsError("profile_conflict", "The requested machine label or target/session is already assigned.")
    try:
        _local_herdr(["machine", "add", target, "--remote-session", session, "--label", label],
                     timeout=90, mutation=True)
    except HerdrOpsError as exc:
        if exc.effect != "unknown":
            raise
    # After dispatch, reconcile even when the response was lost or nonzero; never replay add.
    try:
        after = list_profiles()
    except HerdrOpsError:
        raise HerdrOpsError("profile_registration_unknown", "Could not inspect saved profiles after registration; reconcile before retrying.", "unknown") from None
    matches = [p for p in after if p.get("ssh_target") == target and p.get("remote_session") == session and p.get("label") == label]
    if (len(matches) == 1
            and sum(p.get("ssh_target") == target and p.get("remote_session") == session for p in after) == 1
            and sum(p.get("label") == label for p in after) == 1):
        return matches[0]
    raise HerdrOpsError("profile_registration_unknown", "Native Herdr machine registration has no uniquely verifiable saved profile; reconcile before retrying.", "unknown")


def _verify_profile(
    profile: Mapping[str, Any],
    install: Mapping[str, Any],
    config: Mapping[str, Any],
    start: Mapping[str, Any],
) -> dict[str, Any]:
    verification = inspect_profile(str(profile["id"]))
    if verification.get("connectivity") != "connected":
        raise HerdrOpsError("profile_verify_failed", "The saved profile was registered but its API snapshot could not be verified.", "unknown")
    return {
        "operation": "deploy-or-upgrade",
        "state": "verified",
        "profile": verification["profile"],
        "connectivity": verification,
        "binary": dict(install),
        "config": {key: value for key, value in config.items() if key not in {"settings"}},
        "server": dict(start),
    }
