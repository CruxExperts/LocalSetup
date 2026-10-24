from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .constants import DEFAULT_RUN_TIMEOUT, DEFAULT_TAIL_LINES, DEFAULT_WAIT_TIMEOUT
from .keepalive import (
    MAX_REFRESHES,
    MAX_TTL_SECONDS,
    cmd_keepalive_clear,
    cmd_keepalive_refresh,
    cmd_keepalive_request,
    cmd_keepalive_status,
    cmd_keepalive_sweep,
)
from .legacy import cmd_send, cmd_wait
from .run_control import _command_from_remainder, cmd_cancel, cmd_run, cmd_status
from . import managed
from ls.core.terminal_ops import PaneError
from .sanitize import _compile_idle_re
from .session import cmd_pick, cmd_probe
from ls.core.framework_version import framework_version
from ls.core.terminal_ops import API_VERSION as TERMINAL_OPS_API_VERSION
from ls.core.terminal_ops.command_state import ShellCommandError, make_owner_binding


def _emit_error(out: dict[str, Any]) -> None:
    err = out.get("error", "unknown error")
    detail = out.get("detail", "")
    source = out.get("source", "")
    parts = [f"tmux_ops: {err}"]
    if detail:
        parts.append(f" detail={detail}")
    if source:
        parts.append(f" source={source}")
    sys.stderr.write("".join(parts) + "\n")


def main() -> int:
    try:
        parser = argparse.ArgumentParser(description="Managed tmux ops: pick, probe, run, status, cancel.")
        subparsers = parser.add_subparsers(dest="command", required=True)

        subparsers.add_parser("capabilities", help="Report local managed terminal capabilities without side effects")

        pick_p = subparsers.add_parser("pick", help="Reuse or create the exact controller's lower managed pane")
        pick_p.add_argument("--repository", default=None)
        pick_p.add_argument("--host-alias", default=None)
        pick_p.add_argument("--cwd", default=None)
        pick_p.add_argument("--state-dir", default=None)
        pick_p.add_argument("--legacy-session", action="store_true", help="Use the historical explicit ops session workflow")

        probe_p = subparsers.add_parser("probe", help="Check sudo readiness without fixed sleeps")
        probe_p.add_argument("-t", "--target", required=True, metavar="SESSION")
        probe_p.add_argument("--state-dir", default=None)

        run_p = subparsers.add_parser("run", help="Run a command in a managed tmux session")
        run_p.add_argument("-t", "--target", required=True, metavar="SESSION")
        run_p.add_argument("--timeout", type=float, default=DEFAULT_RUN_TIMEOUT, metavar="SECS")
        run_p.add_argument("--auth-timeout", type=float, default=120.0, metavar="SECS",
                           help="Bound automatic and private-dialog sudo authentication together (default: 120)")
        run_p.add_argument("--tail", type=int, default=DEFAULT_TAIL_LINES, metavar="N")
        run_p.add_argument("--state-dir", default=None)
        run_p.add_argument("--legacy-session", action="store_true", help="Run against an explicit historical ops session")
        run_p.add_argument("--dialog-handle", default=None, help="Owned native shell session for private sudo input")
        run_p.add_argument("--dialog-state-root", default=None, help="State root of the owned native shell session")
        run_p.add_argument("--owner-pid", type=int, default=None, help="Live controller ancestor PID")
        run_p.add_argument("--owner-starttime", type=int, default=None, help="Controller process generation")
        run_p.add_argument("--agent-id", default=None, help="Optional native controller identity")
        run_p.add_argument("--single-password-qualified", action="store_true",
                           help="Assert a verified single-password policy before allowing a second sudo prompt")
        run_p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- CMD")

        status_p = subparsers.add_parser("status", help="Report active or completed run status")
        status_p.add_argument("-t", "--target", default=None, metavar="SESSION_OR_HANDLE")
        status_p.add_argument("--status-handle", default=None, metavar="HANDLE",
                              help="Read an operation receipt without current pane or directory context")
        status_p.add_argument("--run-id", default=None, metavar="ID")
        status_p.add_argument("--wait", action="store_true", default=False)
        status_p.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT, metavar="SECS")
        status_p.add_argument("--tail", type=int, default=DEFAULT_TAIL_LINES, metavar="N")
        status_p.add_argument("--state-dir", default=None)

        cancel_p = subparsers.add_parser("cancel", help="Interrupt the active managed run")
        cancel_p.add_argument("-t", "--target", required=True, metavar="SESSION")
        cancel_p.add_argument("--run-id", required=True, metavar="ID")

        send_p = subparsers.add_parser("send", help="Legacy: send one command to pane")
        send_p.add_argument("-t", "--target", required=True, metavar="SESSION")
        send_p.add_argument("-d", "--delay", type=float, default=None, metavar="SECS")
        send_p.add_argument("--wait", action="store_true", default=False)
        send_p.add_argument("--wait-timeout", type=float, default=DEFAULT_WAIT_TIMEOUT, metavar="SECS")
        send_p.add_argument("--idle-re", default=None, metavar="PATTERN")
        send_p.add_argument("cmd", nargs=1, metavar="CMD")

        wait_p = subparsers.add_parser("wait", help="Legacy: poll pane until prompt idle or timeout")
        wait_p.add_argument("-t", "--target", required=True, metavar="SESSION")
        wait_p.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT, metavar="SECS")
        wait_p.add_argument("--idle-re", default=None, metavar="PATTERN")
        wait_p.add_argument("--pre-cursor-y", type=int, default=None, metavar="N")

        keepalive_p = subparsers.add_parser("keepalive", help="Manage sudo keepalive markers")
        keepalive_sub = keepalive_p.add_subparsers(dest="keepalive_command", required=True)

        keepalive_request_p = keepalive_sub.add_parser("request", help="Request bounded sudo keepalive refreshes")
        keepalive_request_p.add_argument("-t", "--target", required=True, metavar="SESSION")
        keepalive_request_p.add_argument("--owner", required=True, metavar="ID")
        keepalive_request_p.add_argument("--ttl-seconds", type=int, default=MAX_TTL_SECONDS, metavar="N")
        keepalive_request_p.add_argument("--max-refreshes", type=int, default=MAX_REFRESHES, metavar="N")
        keepalive_request_p.add_argument("--reason", required=True, metavar="TEXT")

        keepalive_refresh_p = keepalive_sub.add_parser("refresh", help="Refresh sudo credentials once")
        keepalive_refresh_p.add_argument("-t", "--target", required=True, metavar="SESSION")

        keepalive_sub.add_parser("sweep", help="Disable expired or invalid keepalive markers")

        keepalive_status_p = keepalive_sub.add_parser("status", help="Report keepalive marker status")
        keepalive_status_p.add_argument("-t", "--target", default=None, metavar="SESSION")

        keepalive_clear_p = keepalive_sub.add_parser("clear", help="Disable a keepalive marker")
        keepalive_clear_p.add_argument("-t", "--target", required=True, metavar="SESSION")
        keepalive_clear_p.add_argument("--owner", required=True, metavar="ID")

        args = parser.parse_args()
        if args.command == "capabilities":
            out = {
                "api_version": 1,
                "framework": "LocalSetup",
                "framework_version": framework_version(),
                "terminal_ops_api_version": TERMINAL_OPS_API_VERSION,
                "capabilities": [
                    "generic-sudo-key",
                    "framed-credential-channel",
                    "bounded-sudo-authentication",
                    "durable-operation-status-handle",
                    "managed-pane-api-v1",
                ],
                "remote_helper_version_check": "probe.capability_status",
            }
        elif args.command == "pick":
            out = (cmd_pick() if args.legacy_session else
                   managed.pick(repository=args.repository, host_alias=args.host_alias,
                                cwd=args.cwd, state_dir=args.state_dir))
        elif args.command == "probe":
            if args.target and args.target.startswith("%"):
                out = managed.status(args.target, state_dir=args.state_dir)
                out["sudo"] = "operation_bound"
            else:
                out = cmd_probe(args.target)
        elif args.command == "run":
            dialog_options = (args.dialog_state_root is not None or args.owner_pid is not None
                              or args.owner_starttime is not None or args.agent_id is not None)
            if dialog_options and args.dialog_handle is None:
                raise PaneError("dialog_handle_required", "Dialog owner and state options require --dialog-handle.")
            if args.legacy_session and (args.dialog_handle is not None or args.single_password_qualified):
                raise PaneError("legacy_dialog_unsupported", "The historical session cannot use dialog or single-password options.")
            dialog_context = None
            if args.dialog_handle is not None:
                if args.owner_pid is None or args.owner_starttime is None:
                    raise PaneError("dialog_owner_required", "Supply the live --owner-pid and --owner-starttime for a dialog.")
                try:
                    owner = make_owner_binding(owner_pid=args.owner_pid,
                                               owner_starttime=args.owner_starttime,
                                               agent_id=args.agent_id)
                except ShellCommandError as exc:
                    raise PaneError(exc.code, exc.message) from exc
                from ls.core.terminal_ops.sudo_dialog import SudoDialogContext

                dialog_context = SudoDialogContext(args.dialog_handle, owner=owner,
                                                   state_root=args.dialog_state_root)
            if args.target.startswith("%"):
                argv = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
                extra = {}
                if dialog_context is not None:
                    extra["dialog_context"] = dialog_context
                if args.single_password_qualified:
                    extra["single_password_qualified"] = True
                out = managed.run(args.target, argv, timeout=args.timeout,
                                  authentication_timeout=args.auth_timeout, state_dir=args.state_dir,
                                  **extra)
            elif args.legacy_session:
                cmd, cmd_err = _command_from_remainder(args.cmd)
                out = ({"error": "invalid command", "detail": cmd_err, "source": "run"}
                       if cmd_err else cmd_run(args.target, cmd or "", args.timeout, args.tail))
            else:
                out = {"error": "managed pane target required", "detail": "Use the exact %pane target returned by pick.", "source": "run"}
        elif args.command == "status":
            if args.status_handle is not None and args.target is not None:
                parser.error("use either -t <target-or-handle> or --status-handle, not both")
            if args.target and args.target.startswith("%"):
                out = managed.status(args.target, args.run_id, state_dir=args.state_dir,
                                     wait=args.wait, timeout=args.timeout)
            elif args.status_handle is not None or (args.target and args.target.startswith("lsop1.")):
                handle = args.status_handle or args.target
                out = managed.status(None, state_dir=args.state_dir, wait=args.wait,
                                     timeout=args.timeout, status_handle=handle)
            else:
                if args.target is None:
                    parser.error("status requires -t <target>, -t <status-handle>, or --status-handle")
                out = cmd_status(args.target, args.run_id, args.wait, args.timeout, args.tail)
        elif args.command == "cancel":
            out = ({"error": "managed pane interruption requires owner reconciliation", "source": "cancel"}
                   if args.target.startswith("%") else cmd_cancel(args.target, args.run_id))
        elif args.command == "send":
            idle_re, re_err = _compile_idle_re(args.idle_re)
            out = (
                {"error": "invalid --idle-re pattern", "detail": re_err, "source": "send"}
                if re_err
                else cmd_send(
                    args.target,
                    args.cmd[0],
                    args.delay,
                    wait=args.wait,
                    wait_timeout=args.wait_timeout,
                    idle_re=idle_re,
                )
            )
        elif args.command == "wait":
            idle_re, re_err = _compile_idle_re(args.idle_re)
            out = (
                {"error": "invalid --idle-re pattern", "detail": re_err, "source": "wait"}
                if re_err
                else cmd_wait(
                    args.target,
                    timeout=args.timeout,
                    idle_re=idle_re,
                    pre_cursor_y=args.pre_cursor_y,
                )
            )
        elif args.command == "keepalive":
            if args.keepalive_command == "request":
                out = cmd_keepalive_request(
                    args.target,
                    args.owner,
                    args.ttl_seconds,
                    args.max_refreshes,
                    args.reason,
                )
            elif args.keepalive_command == "refresh":
                out = cmd_keepalive_refresh(args.target)
            elif args.keepalive_command == "sweep":
                out = cmd_keepalive_sweep()
            elif args.keepalive_command == "status":
                out = cmd_keepalive_status(args.target)
            elif args.keepalive_command == "clear":
                out = cmd_keepalive_clear(args.target, args.owner)
            else:
                out = {"error": "unknown keepalive command", "source": "keepalive"}
        else:
            out = {"error": "unknown command", "source": "main"}

        if "error" in out:
            _emit_error(out)
        print(json.dumps(out))
        return 0 if "error" not in out else 1
    except PaneError as e:
        payload = {"error": e.code, "detail": e.message, "effect": e.effect, "source": "managed-pane"}
        _emit_error(payload)
        print(json.dumps(payload))
        return 1
    except Exception as e:
        err_payload = {
            "error": "unexpected exception",
            "exception_type": type(e).__name__,
            "exception_message": str(e),
            "source": "main",
        }
        _emit_error(err_payload)
        print(json.dumps(err_payload))
        if os.environ.get("LOCALSETUP_DEBUG"):
            import traceback

            sys.stderr.write(traceback.format_exc())
        return 1
