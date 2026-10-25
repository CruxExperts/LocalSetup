"""Narrow native tmux control-event and canvas-layout-loop entrypoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .command_service import ShellCommandService
from .command_state import ShellCommandError


def _service(path: Path) -> ShellCommandService:
    return ShellCommandService(path.parent.parents[3],
                               source_root=Path(__file__).resolve().parents[3])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="localsetup native shell control")
    parser.add_argument("--session-record", type=Path, required=True)
    parser.add_argument("--serve", action="store_true",
                        help="run the single native layout control loop")
    parser.add_argument("--ui-generation")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress event JSON on native key and menu callbacks")
    parser.add_argument("--client-pid", type=int)
    parser.add_argument("--action", choices=(
        "f6", "panes_menu", "layout_menu", "page_step", "select", "mode",
        "ratio", "increase_ratio", "decrease_ratio",
        "expand", "restore", "page", "divider", "mouse", "border", "escape", "escape_pair",
        "show_logs", "show_panes", "help", "exit", "next_pane",
        "previous_pane", "activate", "menu", "theme", "theme_preview", "setting",
        "make_default",
    ))
    parser.add_argument("--operation-id")
    parser.add_argument("--mouse-x", type=int)
    parser.add_argument("--mouse-y", type=int)
    parser.add_argument("--mouse-event", choices=("down", "drag", "up"))
    parser.add_argument("--mouse-pane")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.session_record
    if not path.is_absolute() or path.name != "session.json":
        return 2
    service = _service(path)
    if args.serve:
        if args.action is not None or args.client_pid is not None:
            return 2
        return service.run_native_control_loop(path)
    if args.action is None or args.client_pid is None:
        return 2
    try:
        result = service.native_control_event(
            path, args.client_pid, args.action,
            operation_id=args.operation_id, mouse_x=args.mouse_x,
            mouse_y=args.mouse_y, mouse_event=args.mouse_event,
            mouse_pane=args.mouse_pane,
        )
    except (ShellCommandError, OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ShellCommandError):
            result = {"api_version": 1, "ok": False, **exc.as_dict()}
        else:
            result = {"api_version": 1, "ok": False,
                      "code": "native_control_failed", "message": str(exc),
                      "effect": "none"}
    if not args.quiet:
        sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
    # Native tmux run-shell callbacks must not echo their command line into a
    # workload or control pane when an event is rejected. Direct CLI callers
    # retain the structured JSON and nonzero status for diagnostics.
    return 0 if args.quiet or result.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
