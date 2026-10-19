"""Thin argument and output layer for explicit Herdr operations."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any, Sequence
import uuid

from .errors import HerdrOpsError, safe_text
from .layout import (
    cleanup_owned_pane,
    create_owned_pane,
    get_pane,
    inspect_pane,
    move_owned_pane,
    resize_owned_pane,
)
from .tasks import cancel_task, cleanup_task, run_task, task_status, wait_task
from .transport import machine_snapshot, request, resolve_endpoint


def add_common_arguments(parser: argparse.ArgumentParser, *, suppress_defaults: bool = True) -> argparse.ArgumentParser:
    """Add the shared Herdr target/state flags to an operation parser."""
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--socket", "--endpoint", dest="endpoint", default=default, help="explicit local Herdr Unix socket (or SSH-forwarded local socket)")
    parser.add_argument("--session", default=default, help="explicit existing Herdr session name")
    parser.add_argument("--machine", default=default, help="saved Herdr machine for supported read-only CLI forwarding")
    parser.add_argument("--workspace", default=default, help="explicit workspace ID")
    parser.add_argument("--pane", default=default, help="explicit caller or target pane ID")
    parser.add_argument("--state-dir", default=default, help="private operation-state directory")
    parser.add_argument("--json", action="store_true", default=default, help="emit structured JSON")
    parser.add_argument("--dry-run", action="store_true", default=default, help="describe a mutation without applying it")
    return parser


def _leaf(parent: argparse._SubParsersAction[argparse.ArgumentParser], name: str, help_text: str) -> argparse.ArgumentParser:
    parser = parent.add_parser(name, help=help_text)
    add_common_arguments(parser)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="localsetup herdr", description="Safely inspect and operate an explicit Herdr endpoint.")
    add_common_arguments(parser, suppress_defaults=False)
    sub = parser.add_subparsers(dest="cmd", required=True)

    context = _leaf(sub, "context", "show a read-only session snapshot")
    pane = _leaf(sub, "pane", "inspect or manage an owned pane")
    pane_sub = pane.add_subparsers(dest="pane_cmd", required=True)
    inspect = _leaf(pane_sub, "inspect", "inspect one explicit pane and its layout")
    inspect.add_argument("--pane-id", dest="pane_id")
    list_panes = _leaf(pane_sub, "list", "list panes in an explicit workspace")
    create = _leaf(pane_sub, "create", "create a non-focused temporary pane below the caller pane")
    create.add_argument("--cwd", required=True, help="explicit absolute pane working directory")
    ensure = _leaf(pane_sub, "ensure", "create or safely reuse one stable idle owned pane")
    ensure.add_argument("--cwd", required=True, help="explicit absolute pane working directory")
    ensure.add_argument("--owner-id", help="stable 32-character owner ID to reuse; generated when omitted")
    resize = _leaf(pane_sub, "resize", "resize one pane owned by a prior operation")
    resize.add_argument("owner_id")
    resize.add_argument("--direction", choices=("up", "down", "left", "right"), required=True)
    resize.add_argument("--amount", type=float, default=0.1)
    move = _leaf(pane_sub, "move", "move one pane owned by a prior operation")
    move.add_argument("owner_id")
    move.add_argument("--target-pane", required=True)
    move.add_argument("--split", choices=("right", "down"), default="right")
    move.add_argument("--ratio", type=float, default=0.5)
    cleanup = _leaf(pane_sub, "cleanup", "close only the pane owned by a prior operation")
    cleanup.add_argument("owner_id")
    read = _leaf(pane_sub, "read", "read output from one explicit pane")
    read.add_argument("--pane-id", dest="pane_id")
    read.add_argument("--source", choices=("visible", "recent", "recent-unwrapped", "detection"), default="recent")
    read.add_argument("--lines", type=int, default=80)

    task = _leaf(sub, "task", "run and reconcile commands in owned panes")
    task_sub = task.add_subparsers(dest="task_cmd", required=True)
    run = _leaf(task_sub, "run", "run one argv command in a newly owned pane")
    run.add_argument("--cwd", required=True, help="explicit absolute command working directory")
    run.add_argument("--wait", type=float, default=0, help="wait up to this many seconds for the real exit receipt")
    run.add_argument("--ssh-target", help="explicit SSH alias for remote runner staging and receipt readback")
    run.add_argument("--remote-state-dir", help="absolute private receipt directory on the SSH target")
    run.add_argument("command", nargs=argparse.REMAINDER, help="argv following --")
    status = _leaf(task_sub, "status", "read a task receipt and current completion state")
    status.add_argument("task_id")
    wait = _leaf(task_sub, "wait", "wait for an exit receipt for a bounded time")
    wait.add_argument("task_id")
    wait.add_argument("--timeout", type=float, default=60)
    cancel = _leaf(task_sub, "cancel", "close exactly the pane owned by a task")
    cancel.add_argument("task_id")
    task_cleanup = _leaf(task_sub, "cleanup", "close a completed task pane while preserving its exit receipt")
    task_cleanup.add_argument("task_id")

    remote_module = importlib.import_module(".remote", __package__)
    provenance_module = importlib.import_module(".provenance", __package__)
    remote_module.add_parser(sub)
    provenance_module.add_parser(sub)
    return parser


def _state_dir(args: argparse.Namespace) -> str | None:
    return getattr(args, "state_dir", None)


def _endpoint(args: argparse.Namespace) -> str:
    if getattr(args, "machine", None):
        raise HerdrOpsError("endpoint_required", "This operation needs a local socket or SSH-forwarded endpoint; saved-machine forwarding is read-only here.")
    return resolve_endpoint(endpoint=getattr(args, "endpoint", None), session=getattr(args, "session", None))


def _caller_scope(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Use inherited IDs only when the selected socket is the managed local one."""
    workspace_id = getattr(args, "workspace", None)
    pane_id = getattr(args, "pane", None)
    if getattr(args, "machine", None) or os.environ.get("HERDR_ENV") != "1":
        return workspace_id, pane_id
    inherited_workspace = os.environ.get("HERDR_WORKSPACE_ID")
    inherited_pane = os.environ.get("HERDR_PANE_ID")
    if not inherited_workspace and not inherited_pane:
        return workspace_id, pane_id
    try:
        selected = Path(_endpoint(args)).expanduser().resolve(strict=False)
        inherited = Path(resolve_endpoint(environ=os.environ)).expanduser().resolve(strict=False)
    except (HerdrOpsError, OSError, RuntimeError):
        return workspace_id, pane_id
    if selected != inherited:
        return workspace_id, pane_id
    return workspace_id or inherited_workspace, pane_id or inherited_pane


def _snapshot_body(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("type") == "session_snapshot" and isinstance(value.get("snapshot"), dict):
        return value["snapshot"]
    if isinstance(value.get("result"), dict):
        result = value["result"]
        if result.get("type") == "session_snapshot" and isinstance(result.get("snapshot"), dict):
            return result["snapshot"]
    if isinstance(value.get("snapshot"), dict):
        return value["snapshot"]
    return value


def _safe_snapshot(snapshot: dict[str, Any], workspace_id: str | None, pane_id: str | None) -> dict[str, Any]:
    panes = snapshot.get("panes")
    if not isinstance(panes, list):
        raise HerdrOpsError("invalid_response", "Herdr snapshot has no pane list.")
    visible = [item for item in panes if isinstance(item, dict) and item.get("label") != "sudo"]
    protected_ids = {item.get("pane_id") for item in panes if isinstance(item, dict) and item.get("label") == "sudo"}
    if pane_id:
        selected = [item for item in visible if item.get("pane_id") == pane_id]
        if not selected:
            if pane_id in protected_ids:
                raise HerdrOpsError("protected_pane", "The sudo pane is reserved for its controller-owned workflow.")
            raise HerdrOpsError("not_found", "The explicit pane ID was not found in this session.")
        visible = selected
        if workspace_id and selected[0].get("workspace_id") != workspace_id:
            raise HerdrOpsError("scope_mismatch", "The pane is outside the explicitly selected workspace.")
    if workspace_id:
        workspaces = snapshot.get("workspaces", [])
        if not any(isinstance(item, dict) and item.get("workspace_id") == workspace_id for item in workspaces):
            raise HerdrOpsError("not_found", "The explicit workspace ID was not found in this session.")
        visible = [item for item in visible if item.get("workspace_id") == workspace_id]
    visible_ids = {item.get("pane_id") for item in visible}
    protected_or_filtered = {item.get("pane_id") for item in panes if isinstance(item, dict)} - visible_ids
    result = dict(snapshot)
    result["panes"] = visible
    if workspace_id:
        result["workspaces"] = [x for x in snapshot.get("workspaces", []) if isinstance(x, dict) and x.get("workspace_id") == workspace_id]
        result["tabs"] = [x for x in snapshot.get("tabs", []) if isinstance(x, dict) and x.get("workspace_id") == workspace_id]
        result["layouts"] = [x for x in snapshot.get("layouts", []) if isinstance(x, dict) and x.get("workspace_id") == workspace_id]
    result["agents"] = [x for x in snapshot.get("agents", []) if isinstance(x, dict) and x.get("pane_id") not in protected_or_filtered]
    layouts = result.get("layouts")
    if isinstance(layouts, list):
        safe_layouts = []
        for item in layouts:
            if not isinstance(item, dict):
                continue
            layout = dict(item)
            layout["panes"] = [p for p in layout.get("panes", []) if isinstance(p, dict) and p.get("pane_id") not in protected_or_filtered]
            if layout.get("focused_pane_id") in protected_or_filtered:
                layout["focused_pane_id"] = None
            safe_layouts.append(layout)
        result["layouts"] = safe_layouts
    if result.get("focused_pane_id") in protected_or_filtered:
        result["focused_pane_id"] = None
    return result


def _context(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "machine", None):
        if getattr(args, "endpoint", None):
            raise HerdrOpsError("target_conflict", "Use either a saved machine or an explicit socket endpoint.")
        raw = machine_snapshot(args.machine)
    else:
        raw = request(_endpoint(args), "session.snapshot", {})
    snapshot = _snapshot_body(raw)
    workspace_id, pane_id = _caller_scope(args)
    return {"target": {"machine": getattr(args, "machine", None), "endpoint": getattr(args, "endpoint", None), "session": getattr(args, "session", None)}, "snapshot": _safe_snapshot(snapshot, workspace_id, pane_id)}


def _dispatch_core(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    if args.cmd == "context":
        return _context(args)
    if args.cmd == "pane":
        workspace_id, caller_pane_id = _caller_scope(args)
        if args.pane_cmd == "list":
            if not workspace_id:
                raise HerdrOpsError("target_required", "Pane listing requires an explicit --workspace ID.")
            snapshot = _snapshot_body(request(_endpoint(args), "session.snapshot", {}))
            result = _safe_snapshot(snapshot, workspace_id, None)
            return {"workspace_id": workspace_id, "panes": result["panes"]}
        if args.pane_cmd == "inspect":
            pane_id = getattr(args, "pane_id", None) or caller_pane_id
            if not workspace_id:
                raise HerdrOpsError("target_required", "Pane inspection requires an explicit --workspace ID.")
            return inspect_pane(_endpoint(args), workspace_id, pane_id)
        if args.pane_cmd == "read":
            pane_id = getattr(args, "pane_id", None) or caller_pane_id
            if not workspace_id:
                raise HerdrOpsError("target_required", "Pane reading requires an explicit --workspace ID.")
            if args.lines < 0 or args.lines > 10000:
                raise HerdrOpsError("invalid_lines", "Pane read lines must be between 0 and 10000.")
            pane = get_pane(_endpoint(args), pane_id, workspace_id=workspace_id)
            source = "recent_unwrapped" if args.source == "recent-unwrapped" else args.source
            read = request(_endpoint(args), "pane.read", {"pane_id": pane["pane_id"], "source": source, "lines": args.lines}).get("read")
            if not isinstance(read, dict):
                raise HerdrOpsError("invalid_response", "Herdr did not return pane output.")
            return {"pane": pane, "read": read}
        if args.pane_cmd == "create":
            if not workspace_id or not caller_pane_id:
                raise HerdrOpsError("target_required", "Pane creation requires a workspace and caller pane from the explicit target or matching managed local context.")
            if args.dry_run:
                pane = get_pane(_endpoint(args), caller_pane_id, workspace_id=workspace_id)
                cwd = Path(args.cwd).expanduser()
                if not cwd.is_absolute():
                    raise HerdrOpsError("invalid_cwd", "A temporary pane requires an explicit absolute working directory.")
                return {"planned": "create-owned-pane", "workspace_id": workspace_id, "caller_pane_id": pane["pane_id"], "cwd": str(cwd), "direction": "down", "ratio": 0.3, "focus": False}
            return create_owned_pane(_endpoint(args), workspace_id, caller_pane_id, args.cwd, state_dir=_state_dir(args))
        if args.pane_cmd == "ensure":
            if not workspace_id or not caller_pane_id:
                raise HerdrOpsError("target_required", "Pane ensure requires a workspace and caller pane from the explicit target or matching managed local context.")
            owner_id = args.owner_id or uuid.uuid4().hex
            if args.dry_run:
                caller = get_pane(_endpoint(args), caller_pane_id, workspace_id=workspace_id)
                cwd = Path(args.cwd).expanduser()
                if not cwd.is_absolute():
                    raise HerdrOpsError("invalid_cwd", "A temporary pane requires an explicit absolute working directory.")
                return {"planned": "ensure-owned-pane", "owner_id": owner_id, "workspace_id": workspace_id, "caller_pane_id": caller["pane_id"], "cwd": str(cwd), "direction": "down", "ratio": 0.3, "focus": False}
            from .layout import ensure_owned_pane

            return ensure_owned_pane(_endpoint(args), workspace_id, caller_pane_id, args.cwd, owner_id, state_dir=_state_dir(args))
        if args.pane_cmd == "resize":
            if args.dry_run:
                return {"planned": "resize-owned-pane", "owner_id": args.owner_id, "direction": args.direction, "amount": args.amount}
            return resize_owned_pane(_endpoint(args), args.owner_id, args.direction, args.amount, state_dir=_state_dir(args))
        if args.pane_cmd == "move":
            if not workspace_id:
                raise HerdrOpsError("target_required", "Pane movement requires explicit destination --workspace ID.")
            if args.dry_run:
                target = get_pane(_endpoint(args), args.target_pane, workspace_id=workspace_id)
                return {"planned": "move-owned-pane", "owner_id": args.owner_id, "target_workspace_id": workspace_id, "target_pane_id": target["pane_id"], "split": args.split, "ratio": args.ratio, "focus": False}
            return move_owned_pane(_endpoint(args), args.owner_id, args.target_pane, target_workspace_id=workspace_id, split=args.split, ratio=args.ratio, state_dir=_state_dir(args))
        if args.pane_cmd == "cleanup":
            return cleanup_owned_pane(_endpoint(args), args.owner_id, state_dir=_state_dir(args), dry_run=bool(args.dry_run))
    if args.cmd == "task":
        if args.task_cmd == "run":
            command = args.command[1:] if args.command and args.command[0] == "--" else args.command
            workspace_id, caller_pane_id = _caller_scope(args)
            if not workspace_id or not caller_pane_id:
                raise HerdrOpsError("target_required", "Task execution requires a workspace and caller pane from the explicit target or matching managed local context.")
            if args.dry_run:
                from .tasks import _remote_receipt_store, _validate_argv

                argv = _validate_argv(command)
                receipt_transport = _remote_receipt_store(args.ssh_target, args.remote_state_dir)
                caller = get_pane(_endpoint(args), caller_pane_id, workspace_id=workspace_id)
                cwd = Path(args.cwd).expanduser()
                if not cwd.is_absolute():
                    raise HerdrOpsError("invalid_cwd", "Task execution requires an explicit absolute working directory.")
                return {"planned": "run-owned-task", "workspace_id": workspace_id, "caller_pane_id": caller["pane_id"], "cwd": str(cwd), "command_bytes": len(shlex.join(argv).encode("utf-8")), "receipt_transport": "ssh" if receipt_transport else "local", "completion": "pending-until-exit-receipt"}
            return run_task(
                _endpoint(args),
                workspace_id,
                caller_pane_id,
                args.cwd,
                command,
                state_dir=_state_dir(args),
                wait_seconds=args.wait,
                ssh_target=args.ssh_target,
                remote_state_dir=args.remote_state_dir,
            )
        if args.task_cmd == "status":
            return task_status(_endpoint(args), args.task_id, state_dir=_state_dir(args))
        if args.task_cmd == "wait":
            return wait_task(_endpoint(args), args.task_id, state_dir=_state_dir(args), timeout=args.timeout)
        if args.task_cmd == "cancel":
            return cancel_task(_endpoint(args), args.task_id, state_dir=_state_dir(args), dry_run=bool(args.dry_run))
        if args.task_cmd == "cleanup":
            return cleanup_task(_endpoint(args), args.task_id, state_dir=_state_dir(args), dry_run=bool(args.dry_run))
    module = importlib.import_module(".remote" if args.cmd == "remote" else ".provenance", __package__)
    dispatch = module.dispatch
    if args.cmd == "remote":
        return dispatch(args, root)
    return dispatch(args, root)


def main(argv: Sequence[str] | None = None, *, root: Path | str | None = None) -> int:
    source_root = Path(root).expanduser().resolve() if root is not None else Path(__file__).resolve().parents[3]
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
        result = _dispatch_core(args, source_root)
        payload = {"ok": True, "result": result}
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 0
    except HerdrOpsError as exc:
        print(json.dumps({"ok": False, "error": exc.as_dict()}, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        error = HerdrOpsError("local_error", safe_text(exc), "none")
        print(json.dumps({"ok": False, "error": error.as_dict()}, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2
    except Exception:
        error = HerdrOpsError("internal_error", "Herdr operation failed unexpectedly.", "unknown")
        print(json.dumps({"ok": False, "error": error.as_dict()}, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2
