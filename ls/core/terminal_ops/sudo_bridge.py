"""Run an operation-bound sudo command in the already qualified visible pane.

The controller retains credential resolution. This child obtains each candidate
only on demand over a private Unix socket; no secret enters argv, a file, or a
child environment. The process launched under sudo is the exact requested argv.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import socket
import stat
import struct
import sys
from typing import Iterator

from .sudo import OperationBinding, payload_digest, run_sudo
from .models import PaneError
from .shell import _atomic_json, _operation_guard, _read_json, _read_operation, _state_directory


def _receive_candidate(socket_path: str, operation_id: str) -> tuple[str, str] | None:
    path = Path(socket_path)
    info = path.lstat()
    parent = path.parent.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077):
        raise OSError("private credential channel has unsafe ownership")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(7)
        connection.connect(socket_path)
        encoded_id = operation_id.encode("ascii", "strict")
        connection.sendall(struct.pack("!I", len(encoded_id)) + encoded_id)
        header = _recv_exact(connection, 4)
        length = struct.unpack("!I", header)[0]
        if length > 8192:
            raise OSError("private credential response is oversized")
        payload = bytearray()
        while len(payload) < length:
            payload.extend(_recv_exact(connection, length - len(payload)))
        if not payload:
            return None
        name, separator, value = bytes(payload).partition(b"\0")
        if not separator:
            raise OSError("private credential response is malformed")
        return name.decode("ascii"), value.decode("utf-8", "strict")


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        block = connection.recv(length - len(data))
        if not block:
            raise OSError("private credential channel was interrupted")
        data.extend(block)
    return bytes(data)


def _running(state_dir: str, binding_key: str, operation_id: str, shell_pid: int) -> bool:
    try:
        _, record = _read_operation(state_dir, binding_key, operation_id)
    except Exception:
        return False
    return record.get("state") == "running" and record.get("shell_pid") == shell_pid and os.getppid() == shell_pid


def _manual_fallback(argv: list[str], runas_user: str | None, runas_group: str | None) -> str:
    command = ["sudo"]
    if runas_user:
        command.extend(("-u", runas_user))
    if runas_group:
        command.extend(("-g", runas_group))
    command.extend(("--", *argv))
    return shlex.join(command)


def main() -> int:
    parser = argparse.ArgumentParser(prog="terminal-sudo-bridge")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--binding-key", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--single-password-qualified", action="store_true")
    parser.add_argument("--runas-user")
    parser.add_argument("--runas-group")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    try:
        _, record = _read_operation(args.state_dir, args.binding_key, args.operation_id)
        if record.get("state") != "running" or not isinstance(record.get("shell_pid"), int):
            return 2
        if os.getppid() != record["shell_pid"]:
            return 2
        shell_pid = record["shell_pid"]
        owner = record.get("binding_key")
        if owner != args.binding_key:
            return 2
        target = record.get("target")
        # The staged transition carries only nonsecret operation metadata.
        if not isinstance(target, str) or not target:
            target = "GENERIC"
        working_directory = record.get("working_directory")
        if (not isinstance(working_directory, str) or not working_directory.startswith("/")
                or "\x00" in working_directory):
            return 2

        def candidates(_target: str, _environment: dict[str, str]) -> Iterator[tuple[str, str]]:
            for _ in range(2):
                candidate = _receive_candidate(args.socket, args.operation_id)
                if candidate is None:
                    return
                yield candidate

        binding = OperationBinding(
            operation_id=args.operation_id,
            owner_id=args.binding_key,
            target=target,
            endpoint_id=str(record.get("endpoint") or args.binding_key),
            live_owner_id=str(record.get("controller_id") or args.binding_key),
            payload_digest=payload_digest(argv, runas_user=args.runas_user, runas_group=args.runas_group),
            single_password_qualified=args.single_password_qualified,
            validate_current=lambda: _running(args.state_dir, args.binding_key, args.operation_id, shell_pid),
        )
        result = run_sudo(
            binding,
            argv,
            environment={key: value for key, value in os.environ.items() if not key.endswith("_SUDO_KEY")},
            resolver=candidates,
            runas_user=args.runas_user,
            runas_group=args.runas_group,
            cwd=working_directory,
        )
        directory = _state_directory(args.state_dir, args.binding_key, create=False)
        receipt = directory / f"{args.operation_id}.sudo.json"
        safe = result.safe_state()
        lookup_path = directory / f"{args.operation_id}.sudo-lookup.json"
        try:
            lookup = _read_json(lookup_path)
        except (OSError, ValueError, PaneError):
            lookup = {}
        if (lookup.get("binding_key") == args.binding_key
                and lookup.get("operation_id") == args.operation_id
                and lookup.get("credential_lookup_status") in {
                    "unavailable", "empty", "invalid", "loading_error", "available", "expired"
                }):
            safe["credential_lookup_status"] = lookup["credential_lookup_status"]
            if lookup["credential_lookup_status"] == "expired":
                safe["authentication_status"] = "expired"
                safe["authentication_detail"] = (
                    "Automatic authentication expired before credential delivery; reconcile status without replay."
                )
            candidates_state = safe.get("credential_candidates")
            if isinstance(candidates_state, list) and not any(
                isinstance(item, dict) and item.get("status") == "provided_to_askpass"
                for item in candidates_state
            ):
                safe["credential_candidates"] = [{
                    "variable": "GENERIC_SUDO_KEY",
                    "status": lookup["credential_lookup_status"],
                }]
            if result.status == "action_required" and lookup["credential_lookup_status"] in {
                "unavailable", "empty", "invalid", "loading_error"
            }:
                safe["manual_fallback"] = _manual_fallback(argv, args.runas_user, args.runas_group)
                safe["manual_fallback_instruction"] = (
                    "In the qualified pane, run this exact authorized command once; do not replay it afterward."
                )
        with _operation_guard(directory):
            _atomic_json(receipt, safe, exclusive=True)
        return result.returncode if result.returncode is not None else 2
    except (OSError, ValueError, TypeError) as exc:
        print(f"managed sudo unavailable: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
