"""JSON CLI for owned, observable tmux command sessions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Sequence

from .command_service import ShellCommandService
from .command_state import ShellCommandError, make_owner_binding
from ..application_profiles.service import resolve_profile


def _bounded_file(path: str, maximum: int, label: str) -> bytes:
    with Path(path).open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ShellCommandError("invalid_" + label, f"{label.replace('_', ' ')} exceeds the supported size.")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="localsetup shell")
    parser.add_argument("--state-root", help="absolute private shell state root (default: current user's XDG state)")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("capabilities", help="report qualified shell backend availability")
    ensure = sub.add_parser("ensure", help="create or join the current agent's owned shell session")
    ensure.add_argument("--cwd", default=".")
    ensure.add_argument("--profile", default="agent-shell")
    _owner_flags(ensure)

    for name in ("inspect", "status"):
        command = sub.add_parser(name, help="inspect an owned session or operation")
        command.add_argument("--handle", required=True)
        _owner_flags(command)
        if name == "status":
            command.add_argument("--wait", action="store_true", help="wait for an operation receipt without cancelling it")
            command.add_argument("--timeout", type=float, default=60.0)

    close = sub.add_parser("close", help="close an idle owned session")
    close.add_argument("--handle", required=True)
    _owner_flags(close)

    run = sub.add_parser("run", help="dispatch one command to a fresh owned window")
    run.add_argument("--handle", required=True, help="session handle returned by ensure")
    run.add_argument("--id", required=True, dest="operation_id")
    run.add_argument("--cwd")
    run.add_argument("--env-json", default="{}")
    run.add_argument("--stdin-file")
    run.add_argument("--stream", action="store_true", help="stream captured command output to stdout until completion or timeout")
    run.add_argument("--timeout", type=float, default=60.0)
    form = run.add_mutually_exclusive_group()
    form.add_argument("--script", help="explicit Bash script text")
    form.add_argument("--argv-json", help="JSON string array of command arguments")
    run.add_argument("command", nargs=argparse.REMAINDER, help="argv after --")
    _owner_flags(run)

    for name in ("view", "screen", "logs", "cancel", "attach"):
        command = sub.add_parser(name, help=f"{name} an owned shell session or operation")
        command.add_argument("--handle", required=True)
        _owner_flags(command)
        if name in {"view", "screen"}:
            command.add_argument("--lines", type=int)
        elif name == "logs":
            command.add_argument("--cursor", type=int)
            command.add_argument("--lines", type=int)
            command.add_argument("--max-bytes", type=int)

    interactive_input = sub.add_parser("input", help="send bytes to one verified active command PTY")
    interactive_input.add_argument("--handle", required=True)
    interactive_input.add_argument("--id", required=True, dest="input_id")
    interactive_input.add_argument("--data", default="")
    interactive_input.add_argument("--data-file")
    interactive_input.add_argument("--eof", action="store_true")
    _owner_flags(interactive_input)

    recover = sub.add_parser("recover", help="observe or explicitly adopt a proven abandoned session")
    recover.add_argument("--handle", required=True)
    recover.add_argument("--adopt", action="store_true")
    _owner_flags(recover)

    sub.add_parser("gc", help="collect only verified abandoned sessions")
    schedule = sub.add_parser("schedule", help="manage the fixed durable one-minute cleanup job")
    schedule_actions = schedule.add_subparsers(dest="schedule_action", required=True)
    install = schedule_actions.add_parser("install", help="enroll the exact cleanup recipe with a durable grant")
    install.add_argument("--grant-id", required=True)
    _owner_flags(install)
    schedule_actions.add_parser("status", help="inspect the cleanup binding and latest run receipt")
    schedule_actions.add_parser("disable", help="revoke future cleanup runs and remove the managed cron entry")
    return parser


def _owner_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--owner-starttime", type=int)
    parser.add_argument("--agent-id")


def _owner(args: argparse.Namespace) -> dict[str, object] | None:
    pid = getattr(args, "owner_pid", None)
    start = getattr(args, "owner_starttime", None)
    agent = getattr(args, "agent_id", None)
    if pid is None and start is None and agent is None:
        raw_pid = os.environ.get("LOCALSETUP_SHELL_OWNER_PID")
        raw_start = os.environ.get("LOCALSETUP_SHELL_OWNER_STARTTIME")
        if not raw_pid and not raw_start:
            raise ShellCommandError(
                "owner_identity_required",
                "Shell mutations require an explicit live owner binding; supply --owner-pid and --owner-starttime.",
            )
        if not raw_pid or not raw_start:
            raise ShellCommandError("owner_identity_invalid", "The configured owner PID and start time must be supplied together.")
        try:
            pid, start = int(raw_pid), int(raw_start)
        except ValueError as exc:
            raise ShellCommandError("owner_identity_invalid", "The configured owner process identity is malformed.") from exc
    if (pid is None) != (start is None):
        raise ShellCommandError("owner_identity_invalid", "--owner-pid and --owner-starttime must be supplied together.")
    if agent is not None and pid is None:
        raise ShellCommandError("owner_identity_invalid", "--agent-id requires an explicit owner PID and start time.")
    result = make_owner_binding(owner_pid=pid, owner_starttime=start, agent_id=agent)
    return result


def _json_object(raw: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ShellCommandError("invalid_" + label, f"{label} must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ShellCommandError("invalid_" + label, f"{label} must be a JSON object.")
    return value


def _json_argv(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ShellCommandError("invalid_argv", "--argv-json must be a JSON string array.") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ShellCommandError("invalid_argv", "--argv-json must be a JSON string array.")
    return value


def _print(value: object, *, file: object = None) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True), file=sys.stdout if file is None else file)


def _exit_code(result: dict[str, object], *, command_wait: bool) -> int:
    if command_wait and (result.get("stream_timed_out") or result.get("wait_timed_out")):
        return 124
    if not result.get("ok", True):
        return 1
    if not command_wait:
        return 0
    code = result.get("exit_code")
    if result.get("state") == "completed" and type(code) is int and 0 <= code <= 255:
        return code
    return 125


def _stream_operation(
    service: ShellCommandService, initial: dict[str, object], *,
    timeout: float, owner: dict[str, object] | None,
) -> dict[str, object]:
    operation_handle = initial.get("operation_handle")
    if not isinstance(operation_handle, str):
        return initial | {"stream_timed_out": False, "stream_error": "missing_operation_handle"}
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 86_400:
        raise ShellCommandError("invalid_timeout", "Stream timeout must be from 0 through 86400 seconds.")
    deadline = time.monotonic() + float(timeout)
    cursor = 0
    gap = False
    terminal_end = None
    while True:
        prior_cursor = cursor
        chunk = service.logs(operation_handle, cursor=cursor, owner=owner)
        if chunk.get("gap"):
            gap = True
        text = chunk.get("output", "")
        if isinstance(text, str) and text:
            sys.stdout.write(text)
            sys.stdout.flush()
        next_cursor = chunk.get("cursor")
        if isinstance(next_cursor, int):
            cursor = max(cursor, next_cursor)
        try:
            observed = service.status(operation_handle, owner=owner)
        except ShellCommandError as exc:
            if exc.code != "busy":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return initial | {"stream_timed_out": True, "stream_gap": gap, "stream_cursor": cursor}
            time.sleep(min(0.1, remaining))
            continue
        if observed.get("state") in {"completed", "failed", "collected", "unknown"}:
            ends = [value for value in (chunk.get("end_cursor"), observed.get("output_end_cursor"))
                    if type(value) is int]
            final_end = max(ends, default=cursor)
            if cursor >= final_end:
                return observed | {"stream_timed_out": False, "stream_gap": gap, "stream_cursor": cursor}
            if cursor <= prior_cursor and terminal_end == final_end:
                return observed | {"stream_timed_out": False, "stream_gap": True,
                                   "stream_incomplete": True, "stream_cursor": cursor}
            if time.monotonic() >= deadline:
                return observed | {"stream_timed_out": True, "stream_gap": gap, "stream_cursor": cursor}
            terminal_end = final_end
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return observed | {"stream_timed_out": True, "stream_gap": gap, "stream_cursor": cursor}
        time.sleep(min(0.1, remaining))


def main(
    argv: Sequence[str] | None = None,
    *,
    source_root: Path,
    home: Path,
) -> int:
    args = build_parser().parse_args(argv)
    service = ShellCommandService(args.state_root, source_root=source_root)
    try:
        owner = None
        if args.action in {"ensure", "run", "close", "input", "cancel", "recover"}:
            owner = _owner(args)
        streamed = False
        if args.action == "capabilities":
            result = service.capabilities()
        elif args.action == "ensure":
            profile = resolve_profile("tmux", args.profile, source_root=source_root, home=home)
            result = service.ensure(cwd=Path(args.cwd).expanduser().resolve(),
                                    settings=profile["settings"], settings_digest=profile["digest"],
                                    owner=owner, profile_name=profile["name"],
                                    profile_base=profile.get("base_profile"))
            if result.get("handle") and owner:
                result["owner"] = {k: owner[k] for k in ("pid", "starttime", "agent_id", "machine_generation")}
                result["join_command"] = f"localsetup shell attach --handle {result['handle']}"
        elif args.action in {"inspect", "status"}:
            result = service.wait(args.handle, timeout=args.timeout) if args.action == "status" and args.wait else service.status(args.handle, owner=owner)
        elif args.action == "close":
            result = service.close(args.handle, owner=owner)
        elif args.action == "run":
            if not 0 <= args.timeout <= 86_400:
                raise ShellCommandError("invalid_timeout", "Command timeout must be from 0 through 86400 seconds.")
            if args.command and args.argv_json:
                raise ShellCommandError("invalid_command", "Choose either positional argv or --argv-json.")
            if args.script is not None:
                result = service.run(args.handle, args.operation_id, script=args.script,
                                     cwd=args.cwd, env=_json_object(args.env_json, "env"),
                                     stdin=_bounded_file(args.stdin_file, 8 * 1024 * 1024, "stdin") if args.stdin_file else None,
                                     owner=owner)
            else:
                command = _json_argv(args.argv_json) if args.argv_json is not None else list(args.command)
                if command[:1] == ["--"]:
                    command = command[1:]
                if not command:
                    raise ShellCommandError("invalid_command", "Provide a command after -- or use --argv-json.")
                result = service.run(args.handle, args.operation_id, argv=command,
                                     cwd=args.cwd, env=_json_object(args.env_json, "env"),
                                     stdin=_bounded_file(args.stdin_file, 8 * 1024 * 1024, "stdin") if args.stdin_file else None,
                                     owner=owner)
            if args.stream:
                result = _stream_operation(service, result, timeout=args.timeout, owner=owner)
                streamed = True
        elif args.action in {"view", "screen"}:
            result = (service.view if args.action == "view" else service.screen)(
                args.handle, lines=args.lines, owner=owner)
        elif args.action == "logs":
            result = service.logs(args.handle, cursor=args.cursor, lines=args.lines,
                                  max_bytes=args.max_bytes, owner=owner)
        elif args.action == "input":
            if args.data_file and args.data:
                raise ShellCommandError("invalid_input", "Choose --data or --data-file.")
            data = _bounded_file(args.data_file, 1_048_576, "input") if args.data_file else args.data
            result = service.input(args.handle, args.input_id, data, eof=args.eof, owner=owner)
        elif args.action == "cancel":
            result = service.cancel(args.handle, owner=owner)
        elif args.action == "attach":
            result = service.attach(args.handle, owner=owner)
        elif args.action == "recover":
            if owner is None:
                raise ShellCommandError("owner_identity_required", "Recovery requires an explicit live owner binding.")
            result = service.recover(args.handle, owner=owner, adopt=args.adopt)
        elif args.action == "gc":
            result = service.gc()
        elif args.action == "schedule":
            result = _schedule(args, source_root=source_root)
        else:  # pragma: no cover
            raise ShellCommandError("unsupported_action", "Unsupported shell command.")
        _print(result, file=sys.stderr if streamed else sys.stdout)
        return _exit_code(result, command_wait=streamed or args.action == "status" and args.wait)
    except (ShellCommandError, OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ShellCommandError):
            payload = {"api_version": 1, "ok": False, **exc.as_dict()}
        else:
            payload = {"api_version": 1, "ok": False, "code": "request_failed", "message": str(exc), "effect": "none"}
        _print(payload)
        return 2


def _schedule(args: argparse.Namespace, *, source_root: Path) -> dict[str, object]:
    from .command_scheduler import (
        SchedulerError, disable_schedule, install_schedule, status_schedule,
    )

    try:
        if args.schedule_action == "install":
            owner = _owner(args)
            return install_schedule(
                source_root=source_root, state_root=args.state_root,
                grant_id=args.grant_id,
                authority_owner={key: owner[key] for key in ("pid", "starttime", "agent_id")},
            )
        if args.schedule_action == "status":
            return status_schedule(source_root=source_root, state_root=args.state_root)
        return disable_schedule(state_root=args.state_root)
    except SchedulerError as exc:
        return {"ok": False, "state": "refused", **exc.as_dict()}
