"""Run an operation-bound sudo command in the already qualified visible pane.

The controller retains credential resolution. This child obtains each candidate
only on demand over a private Unix socket; no secret enters argv, a file, or a
child environment. The process launched under sudo is the exact requested argv.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shlex
import socket
import stat
import struct
import sys
import time
from typing import Any, Iterator, Mapping

from .sudo import OperationBinding, payload_digest, run_sudo
from .sudo_channel import ChannelClosed, CredentialReply, receive_reply, send_request
from .dialog_channel import connect as connect_controller
from .command_state import process_info, process_state
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


def _private_input_reply(
    socket_path: str,
    operation_id: str,
    kind: str,
    ordinal: int,
    digest: str,
    current,
    deadline: float,
    controller_pid: int | None,
    controller_starttime: int | None,
) -> CredentialReply:
    if (type(controller_pid) is not int or controller_pid <= 1
            or type(controller_starttime) is not int or controller_starttime <= 0):
        return CredentialReply("blocked", "none")
    path = Path(socket_path)
    try:
        info = path.lstat()
        parent = path.parent.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o077):
            return CredentialReply("blocked", "none")
    except OSError:
        return CredentialReply("unavailable", "none")

    def active() -> bool:
        return current() is True and time.monotonic() < deadline

    submitted = False
    connection = None
    try:
        if not active():
            return CredentialReply("expired" if time.monotonic() >= deadline else "blocked", "none")
        try:
            connection = connect_controller(
                socket_path, controller_pid=controller_pid,
                controller_starttime=controller_starttime,
            )
        except ChannelClosed:
            return CredentialReply("blocked", "none")
        except OSError:
            return CredentialReply("unavailable", "none")
        connection.settimeout(0.2)
        try:
            send_request(connection, operation_id, kind, ordinal, digest, active)
            submitted = True
            return receive_reply(connection, active)
        except ChannelClosed:
            if not active():
                return CredentialReply("expired" if time.monotonic() >= deadline else "cancelled", "none")
            if submitted:
                return CredentialReply("delivery_unknown", "none")
            return CredentialReply("blocked", "none")
        except OSError:
            if not active():
                return CredentialReply("expired" if time.monotonic() >= deadline else "cancelled", "none")
            if submitted:
                return CredentialReply("delivery_unknown", "none")
            return CredentialReply("unavailable", "none")
        except ValueError:
            if submitted:
                return CredentialReply("delivery_unknown", "none")
            return CredentialReply("blocked", "none")
    finally:
        if connection is not None:
            connection.close()


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


def _controller_current(pid: int | None, starttime: int | None) -> bool:
    owner = process_info(pid) if type(pid) is int else None
    return (owner is not None and owner["starttime"] == starttime
            and process_state(pid) not in {None, "Z", "X"})


def _manual_fallback(argv: list[str], runas_user: str | None, runas_group: str | None) -> str:
    command = ["sudo"]
    if runas_user:
        command.extend(("-u", runas_user))
    if runas_group:
        command.extend(("-g", runas_group))
    command.extend(("--", *argv))
    return shlex.join(command)


def _apply_lookup_receipt(
    safe: dict[str, Any], lookup: Mapping[str, Any], binding_key: str, operation_id: str,
    argv: list[str], runas_user: str | None, runas_group: str | None,
    *, allow_manual_fallback: bool = True,
) -> None:
    """Merge controller lookup state without turning loading errors into retry advice."""
    if (lookup.get("binding_key") != binding_key
            or lookup.get("operation_id") != operation_id
            or lookup.get("credential_lookup_status") not in {
                "unavailable", "empty", "invalid", "loading_error", "available", "expired"
            }):
        return
    status = lookup["credential_lookup_status"]
    safe["credential_lookup_status"] = status
    if status == "expired":
        safe["authentication_status"] = "expired"
        safe["authentication_detail"] = (
            "Automatic authentication expired before credential delivery; reconcile status without replay."
        )
    candidates_state = safe.get("credential_candidates")
    delivered = isinstance(candidates_state, list) and any(
        isinstance(item, dict) and item.get("status") == "provided_to_askpass"
        for item in candidates_state
    )
    if not delivered:
        safe["credential_candidates"] = [{"variable": "GENERIC_SUDO_KEY", "status": status}]
        if status == "loading_error" and safe.get("status") == "action_required":
            safe.update(status="blocked", action_required=True,
                        detail="credential loading failed; check EnvMan before retry")
    if (allow_manual_fallback and safe.get("status") == "action_required"
            and status in {"unavailable", "empty", "invalid"}):
        safe["manual_fallback"] = _manual_fallback(argv, runas_user, runas_group)
        safe["manual_fallback_instruction"] = (
            "In the qualified pane, run this exact authorized command once; do not replay it afterward."
        )


def main() -> int:
    parser = argparse.ArgumentParser(prog="terminal-sudo-bridge")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--binding-key", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--single-password-qualified", action="store_true")
    parser.add_argument("--private-input-protocol", action="store_true")
    parser.add_argument("--manual-input-enabled", action="store_true")
    parser.add_argument("--authentication-deadline", type=float)
    parser.add_argument("--controller-pid", type=int)
    parser.add_argument("--controller-starttime", type=int)
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
        if args.authentication_deadline is not None and (
                not math.isfinite(args.authentication_deadline)
                or not time.monotonic() < args.authentication_deadline):
            return 2
        if args.manual_input_enabled and not args.private_input_protocol:
            return 2
        if args.private_input_protocol:
            controller = process_info(args.controller_pid) if args.controller_pid is not None else None
            if (controller is None or type(args.controller_starttime) is not int
                    or controller.get("starttime") != args.controller_starttime):
                return 2

        computed_digest = payload_digest(argv, runas_user=args.runas_user, runas_group=args.runas_group)
        deadline = (args.authentication_deadline if args.authentication_deadline is not None
                    else time.monotonic() + 120.0)

        def input_owner_current() -> bool:
            if not args.private_input_protocol:
                return True
            return _controller_current(args.controller_pid, args.controller_starttime)

        def current() -> bool:
            return (input_owner_current() and time.monotonic() < deadline
                    and _running(args.state_dir, args.binding_key, args.operation_id, shell_pid))

        binding = OperationBinding(
            operation_id=args.operation_id,
            owner_id=args.binding_key,
            target=target,
            endpoint_id=str(record.get("endpoint") or args.binding_key),
            live_owner_id=str(record.get("controller_id") or args.binding_key),
            payload_digest=computed_digest,
            single_password_qualified=args.single_password_qualified,
            validate_current=lambda: _running(args.state_dir, args.binding_key, args.operation_id, shell_pid),
        )
        request_digest = binding.payload_digest

        def candidates(_target: str, _environment: dict[str, str]) -> Iterator[object]:
            if args.private_input_protocol:
                for ordinal in range(1, 3):
                    yield _private_input_reply(
                        args.socket, args.operation_id, "automatic", ordinal,
                        request_digest, current, deadline,
                        args.controller_pid, args.controller_starttime,
                    )
                return
            for _ in range(2):
                candidate = _receive_candidate(args.socket, args.operation_id)
                if candidate is None:
                    return
                yield candidate

        def manual_provider(attempt: int, broker_current, authentication_deadline: float) -> CredentialReply:
            if not args.private_input_protocol:
                return CredentialReply("unavailable", "none")
            return _private_input_reply(
                args.socket, args.operation_id, "manual", attempt, request_digest,
                lambda: current() and broker_current(), authentication_deadline,
                args.controller_pid, args.controller_starttime,
            )

        result = run_sudo(
            binding,
            argv,
            environment={key: value for key, value in os.environ.items() if not key.endswith("_SUDO_KEY")},
            resolver=candidates,
            runas_user=args.runas_user,
            runas_group=args.runas_group,
            cwd=working_directory,
            manual_provider=manual_provider if args.manual_input_enabled else None,
            authentication_deadline=deadline if args.private_input_protocol else None,
            validate_input_current=input_owner_current,
            controller_pid=args.controller_pid if args.private_input_protocol else None,
            controller_starttime=args.controller_starttime if args.private_input_protocol else None,
        )
        directory = _state_directory(args.state_dir, args.binding_key, create=False)
        receipt = directory / f"{args.operation_id}.sudo.json"
        safe = result.safe_state()
        lookup_path = directory / f"{args.operation_id}.sudo-lookup.json"
        try:
            lookup = _read_json(lookup_path)
        except (OSError, ValueError, PaneError):
            lookup = {}
        _apply_lookup_receipt(safe, lookup, args.binding_key, args.operation_id,
                              argv, args.runas_user, args.runas_group,
                              allow_manual_fallback=not args.manual_input_enabled)
        with _operation_guard(directory):
            _atomic_json(receipt, safe, exclusive=True)
        return result.returncode if result.returncode is not None else 2
    except (OSError, ValueError, TypeError) as exc:
        print(f"managed sudo unavailable: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
