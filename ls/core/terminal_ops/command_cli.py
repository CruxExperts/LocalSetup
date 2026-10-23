"""JSON CLI for owned, observable tmux command sessions."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time
from typing import Sequence

from .command_service import ShellCommandService
from .command_state import ShellCommandError, make_owner_binding
from .join_banner import JoinBannerError, render_join_banner, validate_join_entry
from ..application_profiles.service import resolve_profile, resolve_tmux_runtime_profile


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
    ensure.add_argument("--layout", choices=("family", "legacy", "canvas", "canvas-v2", "canvas-v3", "native"), default=None,
                        help="canvas-v3/native uses qualified native panes; canvas/canvas-v2 retain the existing rendered canvas")
    ensure.add_argument("--replace-retiring", action="store_true",
                        help="start a family generation while the exact closed observer view retires")
    _owner_flags(ensure)

    admit = sub.add_parser("admit", help="issue a one-time family invitation for a native subagent")
    admit.add_argument("--handle", required=True)
    admit.add_argument("--member-agent-id", required=True, dest="member_agent_id")
    admit.add_argument("--label", required=True, dest="label_slug")
    _owner_flags(admit)

    join = sub.add_parser("join", help="claim a root-issued family invitation")
    join.add_argument("--handle", required=True)
    join.add_argument("--invite", required=True)
    _owner_flags(join)

    advertise = sub.add_parser("advertise", help="inspect and display attach commands for existing owned sessions")
    advertise.add_argument("--handle", required=True, action="append",
                           help="session or operation handle; repeat to advertise in the supplied order")
    _owner_flags(advertise)

    for name in ("inspect", "status", "doctor"):
        command = sub.add_parser(name, help="inspect an owned session or operation")
        command.add_argument("--handle", required=True)
        _owner_flags(command)
        if name == "status":
            command.add_argument("--wait", action="store_true", help="wait for an operation receipt without cancelling it")
            command.add_argument("--timeout", type=float, default=60.0)
        if name == "doctor":
            command.add_argument("--repair", action="store_true", help="attempt only exact idle family view or completed-pane repair")

    close = sub.add_parser("close", help="close an idle owned session")
    close.add_argument("--handle", required=True)
    _owner_flags(close)

    viewport = sub.add_parser("viewport", help="resize the one qualified family canvas")
    viewport.add_argument("--handle", required=True)
    viewport.add_argument("--height", type=int, required=True)
    viewport.add_argument("--width", type=int, help="observed canvas columns (canvas-v2 only)")
    _owner_flags(viewport)

    layout = sub.add_parser("layout", help="control a native canvas-v3 layout")
    layout.add_argument("--handle", required=True)
    layout.add_argument("--mode", choices=("follow", "stable", "fixed"))
    layout.add_argument("--select", dest="selected_operation_id")
    layout.add_argument("--ratio", action="append", default=[], metavar="OP=WEIGHT|PERCENT%",
                        help="set a relative weight or a percentage; repeat for multiple operations")
    layout.add_argument("--page", type=int)
    layout.add_argument("--expand", dest="expand_operation_id")
    layout.add_argument("--restore", dest="restore_operation_id")
    _owner_flags(layout)

    refresh = sub.add_parser("refresh-ui", help="reload only the owned native UI renderers")
    refresh.add_argument("--handle", required=True)
    _owner_flags(refresh)

    run = sub.add_parser("run", help="dispatch one command to a fresh owned window")
    run.add_argument("--handle", required=True, help="session handle returned by ensure")
    run.add_argument("--id", required=True, dest="operation_id")
    run.add_argument("--cwd")
    run.add_argument("--env-json", default="{}")
    run.add_argument("--stdin-file")
    run.add_argument("--stream", action="store_true", help="wait for one bounded final receipt and emit join reminders")
    run.add_argument("--controlled-app", choices=("less",),
                     help="run one file in a qualified pager so doctor may request its safe exit")
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--expanded-min-rows", type=int,
                     help="canvas operation minimum height including title")
    form = run.add_mutually_exclusive_group()
    form.add_argument("--script", help="explicit Bash script text")
    form.add_argument("--argv-json", help="JSON string array of command arguments")
    run.add_argument("command", nargs=argparse.REMAINDER, help="argv after --")
    _owner_flags(run)

    for name in ("view", "screen", "logs", "cancel", "attach"):
        command = sub.add_parser(name, help=f"{name} an owned shell session or operation")
        if name == "attach":
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("--handle")
            target.add_argument("--session", help="short collision-checked session alias")
        else:
            command.add_argument("--handle", required=True)
        _owner_flags(command)
        if name in {"view", "screen"}:
            command.add_argument("--lines", type=int)
        elif name == "logs":
            page = command.add_mutually_exclusive_group()
            page.add_argument("--cursor", type=int)
            page.add_argument("--before-cursor", type=int)
            command.add_argument("--lines", type=int)
            command.add_argument("--max-bytes", type=int)

    interactive_input = sub.add_parser("input", help="send bytes to one verified active command PTY")
    interactive_input.add_argument("--handle", required=True)
    interactive_input.add_argument("--id", required=True, dest="input_id")
    interactive_input.add_argument("--data", default="")
    interactive_input.add_argument("--data-file")
    interactive_input.add_argument("--eof", action="store_true")
    interactive_input.add_argument("--lease", dest="lease_id")
    interactive_input.add_argument("--grantee")
    _owner_flags(interactive_input)

    lease = sub.add_parser("input-lease", help="request or revoke bounded mediated human input")
    lease.add_argument("--handle", required=True)
    lease.add_argument("--grantee", required=True)
    lease.add_argument("--ttl", type=int, default=300)
    lease.add_argument("--revoke", action="store_true")
    _owner_flags(lease)

    recover = sub.add_parser("recover", help="observe or explicitly adopt a proven abandoned session")
    recover.add_argument("--handle", required=True)
    recover.add_argument("--adopt", action="store_true")
    _owner_flags(recover)

    gc = sub.add_parser("gc", help="collect only verified abandoned sessions")
    _owner_flags(gc)
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


def _optional_owner(args: argparse.Namespace) -> dict[str, object] | None:
    if (getattr(args, "owner_pid", None) is not None
            or getattr(args, "owner_starttime", None) is not None
            or getattr(args, "agent_id", None) is not None
            or os.environ.get("LOCALSETUP_SHELL_OWNER_PID")
            or os.environ.get("LOCALSETUP_SHELL_OWNER_STARTTIME")):
        return _owner(args)
    return None


def _explicit_owner(args: argparse.Namespace) -> dict[str, object] | None:
    if any(getattr(args, name, None) is not None for name in
           ("owner_pid", "owner_starttime", "agent_id")):
        return _owner(args)
    return None


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


def _bounded_response(value: object, byte_budget: int) -> object:
    """Share one text budget across every output field, including nested receipts."""
    remaining_bytes, remaining_lines = byte_budget, 200
    clipped = False

    def visit(node: object) -> object:
        nonlocal remaining_bytes, remaining_lines, clipped
        if isinstance(node, dict):
            result = {}
            for key, item in node.items():
                if key in {"output", "tail", "lines"} and (isinstance(item, str) or
                                                        isinstance(item, list) and all(isinstance(x, str) for x in item)):
                    is_list = isinstance(item, list)
                    original = "\n".join(item) if is_list else item
                    pieces = original.splitlines(keepends=True)
                    if len(pieces) > remaining_lines:
                        original = "".join(pieces[-remaining_lines:]) if remaining_lines else ""
                    raw = original.encode("utf-8")
                    if len(raw) > remaining_bytes:
                        original = raw[-remaining_bytes:].decode("utf-8", errors="ignore") if remaining_bytes else ""
                    if original != ("\n".join(item) if is_list else item):
                        clipped = True
                    remaining_bytes -= len(original.encode("utf-8"))
                    remaining_lines -= min(remaining_lines, len(original.splitlines()))
                    result[key] = original.splitlines() if is_list else original
                else:
                    result[key] = visit(item)
            return result
        if isinstance(node, list):
            return [visit(item) for item in node]
        return node

    bounded = visit(copy.deepcopy(value))
    if clipped and isinstance(bounded, dict):
        bounded["response_truncated"] = True
    return bounded


def _print(value: object, *, file: object = None) -> None:
    """Keep aggregate raw text and complete serialized JSON within one packet."""
    budget = 32_768
    while True:
        bounded = _bounded_response(value, budget)
        encoded = json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= 65_536:
            break
        if budget:
            budget //= 2
            continue
        if isinstance(bounded, dict):
            for key in ("operations", "profile_settings", "requested_settings", "actions", "attempts"):
                if key in bounded:
                    bounded.pop(key)
                    bounded["response_truncated"] = True
                    encoded = json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if len(encoded.encode("utf-8")) <= 65_536:
                        break
        if len(encoded.encode("utf-8")) > 65_536:
            encoded = json.dumps({"api_version": 1, "ok": False, "code": "response_too_large",
                                  "message": "The response metadata exceeded the safe JSON limit."})
        break
    print(encoded, file=sys.stdout if file is None else file)


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
    last_notice = time.monotonic()
    while True:
        try:
            observed = service.status(operation_handle, owner=owner)
        except ShellCommandError as exc:
            if exc.code != "busy":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return initial | {"stream_timed_out": True}
            time.sleep(min(0.1, remaining))
            continue
        if observed.get("state") in {"completed", "failed", "collected", "unknown"}:
            return observed | {"stream_timed_out": False,
                               "stream_cursor": observed.get("output_cursor"),
                               "stream_gap": observed.get("output_gap", False)}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return observed | {"stream_timed_out": True,
                               "stream_cursor": observed.get("output_cursor"),
                               "stream_gap": observed.get("output_gap", False)}
        if time.monotonic() - last_notice >= 300:
            _periodic_reminder({"api_version": 1, "event": "join_reminder",
                                "machine": observed.get("machine"),
                                "join_command": observed.get("join_command"),
                                "state": observed.get("state"),
                                "session_state": observed.get("session_state"),
                                "native_state": observed.get("native_state")})
            last_notice = time.monotonic()
        time.sleep(min(0.1, remaining))


def _verified_advertisement(
    service: ShellCommandService, handle: str, *, owner: dict[str, object] | None,
    fallback: dict[str, object] | None = None,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    """Inspect a target and return its verified banner fields or a per-target error."""
    try:
        observed = service.status(handle, owner=owner)
        if observed.get("ok") is False:
            return None, {"handle": handle, "ok": False,
                          "code": observed.get("code", "session_not_ready"),
                          "message": observed.get("message", "The shell target is not ready.")}
        if (observed.get("native_state") != "ready"
                or (observed.get("state") != "ready" and observed.get("session_state") != "ready")):
            return None, {"handle": handle, "ok": False, "code": "session_not_ready",
                          "message": "The exact owned session is not live and natively ready."}
        machine = observed.get("machine")
        command = observed.get("join_command")
        if isinstance(fallback, dict):
            if not isinstance(machine, dict) or not isinstance(machine.get("host"), str):
                machine = fallback.get("machine")
            if not isinstance(command, str):
                command = fallback.get("join_command")
        if not isinstance(machine, dict):
            raise JoinBannerError("The service did not provide a verified machine identity.")
        entry = {"host": machine.get("host"), "join_command": command}
        host, exact_command = validate_join_entry(entry)
        return ({"host": host, "join_command": exact_command},
                {"handle": handle, "ok": True, "state": observed.get("state"),
                 "machine": machine, "join_command": exact_command})
    except (ShellCommandError, OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ShellCommandError):
            code, message = exc.code, exc.message
        elif isinstance(exc, JoinBannerError):
            code, message = "advertisement_invalid", str(exc)
        else:
            code, message = "advertisement_unavailable", "The shell target could not be inspected safely."
        return None, {"handle": handle, "ok": False, "code": code, "message": message}


def _color_capable(stream: object, environ: dict[str, str] = os.environ) -> bool:
    try:
        is_tty = bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return is_tty and "NO_COLOR" not in environ and environ.get("TERM") not in {None, "", "dumb"}


def _write_advertisement(entries: list[dict[str, object]], *, file: object = None) -> None:
    output = sys.stderr if file is None else file
    if entries:
        print(render_join_banner(entries, color=_color_capable(output)), file=output)


def _periodic_reminder(
    notice: dict[str, object], *, service: ShellCommandService | None = None,
    handle: str | None = None, owner: dict[str, object] | None = None,
) -> None:
    entry = None
    if service is not None and handle is not None:
        entry, _ = _verified_advertisement(service, handle, owner=owner)
    elif (notice.get("native_state") == "ready"
          and (notice.get("state") == "ready" or notice.get("session_state") == "ready")):
        machine = notice.get("machine")
        candidate = {"host": machine.get("host") if isinstance(machine, dict) else None,
                     "join_command": notice.get("join_command")}
        try:
            host, command = validate_join_entry(candidate)
        except JoinBannerError:
            pass
        else:
            entry = {"host": host, "join_command": command}
    if entry is not None:
        _write_advertisement([entry])
    _print(notice, file=sys.stderr)


def _automatic_advertisement(
    service: ShellCommandService, handle: object, *, owner: dict[str, object] | None,
    fallback: dict[str, object],
) -> None:
    if not isinstance(handle, str) or fallback.get("ok") is False:
        return
    entry, _ = _verified_advertisement(service, handle, owner=owner, fallback=fallback)
    if entry is not None:
        _write_advertisement([entry])


def _advertise_batch(
    service: ShellCommandService, handles: list[str], *, owner: dict[str, object] | None,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for handle in handles:
        entry, item = _verified_advertisement(service, handle, owner=owner)
        results.append(item)
        if entry is not None:
            entries.append(entry)
    _write_advertisement(entries)
    failed = any(item.get("ok") is False for item in results)
    return {"api_version": 1, "ok": not failed,
            "state": "partial" if failed and entries else "refused" if failed else "ready",
            "effect": "none", "entries": results}


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
        if args.action in {"ensure", "run", "close", "viewport", "layout", "refresh-ui", "input-lease", "cancel", "recover", "admit", "join"}:
            owner = _owner(args)
        elif args.action == "input":
            owner = _explicit_owner(args) if args.lease_id else _owner(args)
        elif args.action in {"inspect", "status", "doctor", "view", "screen", "logs", "attach", "advertise", "gc"}:
            owner = (_explicit_owner(args) if args.action == "attach" and args.session
                     else _optional_owner(args))
            if args.action == "doctor" and args.repair:
                owner = _owner(args)
        streamed = False
        if args.action == "capabilities":
            result = service.capabilities()
        elif args.action == "ensure":
            runtime_profile = resolve_tmux_runtime_profile(source_root=source_root, home=home,
                                                           name=args.profile)
            profile = runtime_profile["profile"]
            result = service.ensure(cwd=Path(args.cwd).expanduser().resolve(),
                                    settings=profile["settings"], settings_digest=profile["digest"],
                                    owner=owner, profile_name=profile["name"],
                                    profile_base=profile.get("base_profile"),
                                    layout_mode=(None if args.layout is None else
                                                 "canvas-v3" if args.layout in {"canvas-v3", "native"}
                                                 else "canvas-v2" if args.layout in {"canvas", "canvas-v2"}
                                                 else f"{args.layout}-v1"),
                                    replace_retiring=args.replace_retiring,
                                    runtime_settings=runtime_profile["settings"],
                                    runtime_warnings=runtime_profile["warnings"])
            if result.get("handle") and owner:
                result["owner"] = {k: owner[k] for k in ("pid", "starttime", "agent_id", "machine_generation")}
            _automatic_advertisement(service, result.get("handle"), owner=owner, fallback=result)
        elif args.action == "admit":
            result = service.admit_member(args.handle, agent_id=args.member_agent_id,
                                          label_slug=args.label_slug, owner=owner)
        elif args.action == "join":
            result = service.join_family(args.handle, invitation=args.invite, owner=owner)
            _automatic_advertisement(service, result.get("handle"), owner=owner, fallback=result)
        elif args.action == "advertise":
            result = _advertise_batch(service, args.handle, owner=owner)
        elif args.action in {"inspect", "status"}:
            result = (service.wait(args.handle, timeout=args.timeout, owner=owner,
                                   progress=lambda notice: _periodic_reminder(
                                       notice, service=service, handle=args.handle, owner=owner))
                      if args.action == "status" and args.wait else service.status(args.handle, owner=owner))
        elif args.action == "doctor":
            result = service.doctor(args.handle, owner=owner, repair=args.repair)
        elif args.action == "close":
            result = service.close(args.handle, owner=owner)
        elif args.action == "viewport":
            result = service.viewport(args.handle, height=args.height, width=args.width, owner=owner)
        elif args.action == "refresh-ui":
            result = service.refresh_ui(args.handle, owner=owner)
        elif args.action == "layout":
            ratios: dict[str, object] = {}
            for item in args.ratio:
                if "=" not in item:
                    raise ShellCommandError("invalid_native_ratios", "Each --ratio value must be OP=WEIGHT or OP=PERCENT%.")
                operation_id, value = item.split("=", 1)
                if operation_id in ratios:
                    raise ShellCommandError("invalid_native_ratios", "An operation ratio may appear only once.")
                ratios[operation_id] = int(value) if value.isdigit() else value
            result = service.layout(
                args.handle, mode=args.mode,
                selected_operation_id=args.selected_operation_id,
                ratios=ratios or None, page=args.page,
                expand_operation_id=args.expand_operation_id,
                restore_operation_id=args.restore_operation_id, owner=owner,
            )
        elif args.action == "run":
            if not 0 <= args.timeout <= 86_400:
                raise ShellCommandError("invalid_timeout", "Command timeout must be from 0 through 86400 seconds.")
            if args.command and args.argv_json:
                raise ShellCommandError("invalid_command", "Choose either positional argv or --argv-json.")
            if args.script is not None:
                result = service.run(args.handle, args.operation_id, script=args.script,
                                     cwd=args.cwd, env=_json_object(args.env_json, "env"),
                                     stdin=_bounded_file(args.stdin_file, 8 * 1024 * 1024, "stdin") if args.stdin_file else None,
                                     owner=owner, controlled_app=args.controlled_app,
                                     expanded_min_rows=args.expanded_min_rows)
            else:
                command = _json_argv(args.argv_json) if args.argv_json is not None else list(args.command)
                if command[:1] == ["--"]:
                    command = command[1:]
                if not command:
                    raise ShellCommandError("invalid_command", "Provide a command after -- or use --argv-json.")
                result = service.run(args.handle, args.operation_id, argv=command,
                                     cwd=args.cwd, env=_json_object(args.env_json, "env"),
                                     stdin=_bounded_file(args.stdin_file, 8 * 1024 * 1024, "stdin") if args.stdin_file else None,
                                     owner=owner, controlled_app=args.controlled_app,
                                     expanded_min_rows=args.expanded_min_rows)
            _automatic_advertisement(service, result.get("operation_handle"),
                                     owner=owner, fallback=result)
            if args.stream:
                result = _stream_operation(service, result, timeout=args.timeout, owner=owner)
                streamed = True
        elif args.action in {"view", "screen"}:
            result = (service.view if args.action == "view" else service.screen)(
                args.handle, lines=args.lines, owner=owner)
        elif args.action == "logs":
            result = service.logs(args.handle, cursor=args.cursor, before_cursor=args.before_cursor,
                                  lines=args.lines,
                                  max_bytes=args.max_bytes, owner=owner)
        elif args.action == "input":
            if args.data_file and args.data:
                raise ShellCommandError("invalid_input", "Choose --data or --data-file.")
            data = _bounded_file(args.data_file, 1_048_576, "input") if args.data_file else args.data
            result = service.input(args.handle, args.input_id, data, eof=args.eof, owner=owner,
                                   lease_id=args.lease_id, grantee=args.grantee)
        elif args.action == "input-lease":
            result = service.input_lease(args.handle, grantee=args.grantee, ttl=args.ttl,
                                         revoke=args.revoke, owner=owner)
        elif args.action == "cancel":
            result = service.cancel(args.handle, owner=owner)
        elif args.action == "attach":
            result = service.attach(service.resolve_alias(args.session) if args.session else args.handle,
                                    owner=owner, human_observer=bool(args.session and owner is None))
        elif args.action == "recover":
            if owner is None:
                raise ShellCommandError("owner_identity_required", "Recovery requires an explicit live owner binding.")
            result = service.recover(args.handle, owner=owner, adopt=args.adopt)
        elif args.action == "gc":
            result = service.gc(owner=owner)
        elif args.action == "schedule":
            result = _schedule(args, source_root=source_root)
        else:  # pragma: no cover
            raise ShellCommandError("unsupported_action", "Unsupported shell command.")
        _print(result)
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
