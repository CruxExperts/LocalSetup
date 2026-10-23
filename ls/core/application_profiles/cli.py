"""Thin JSON command-line interface for application profile management."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from .service import (
    MAX_PROFILE_BYTES,
    ApplicationProfileError,
    _bounded_json,
    list_profiles,
    plan_profile,
    reset_profile,
    resolve_profile,
    save_profile,
    validate_profile,
)


ApplyCallback = Callable[[str, str, dict[str, Any]], Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="localsetup app-profile")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list registered application profiles")
    listing.add_argument("application", nargs="?", help="limit results to one application")

    show = commands.add_parser("show", help="show a resolved profile")
    _identity(show)

    save = commands.add_parser("save", help="save a validated partial user override")
    _identity(save)
    save.add_argument("--as", dest="as_name", help="save as a named variant of the registered base profile")
    save.add_argument("--settings-json", required=True, help="JSON object containing partial settings")

    reset = commands.add_parser("reset", help="remove one user override and restore defaults")
    _identity(reset)

    validate = commands.add_parser("validate", help="validate proposed or currently resolved settings")
    _identity(validate)
    validate.add_argument("--settings-json", help="JSON object containing partial settings")

    plan = commands.add_parser("plan", help="describe the resolved profile for a native adapter")
    _identity(plan)
    _native_target_options(plan)

    apply = commands.add_parser("apply", help="send resolved settings to an explicitly supplied native adapter")
    _identity(apply)
    _native_target_options(apply)

    return parser


def _identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("application", nargs="?", default="tmux")
    parser.add_argument("name", nargs="?", default="agent-shell")


def _native_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-handle", help="exact owned native session handle")
    parser.add_argument("--state-root", help="private terminal operation state root")
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--owner-starttime", type=int)
    parser.add_argument("--agent-id")


def _parse_settings(raw: str) -> dict[str, Any]:
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ApplicationProfileError("settings JSON must be valid UTF-8") from exc
    payload = _bounded_json(encoded, label="settings JSON", limit=MAX_PROFILE_BYTES)
    if not isinstance(payload, dict):
        raise ApplicationProfileError("settings JSON must be an object")
    return payload


def main(
    argv: Sequence[str] | None = None,
    *,
    source_root: Path,
    home: Path,
    apply_callback: ApplyCallback | None = None,
) -> int:
    """Run profile management with a lazy tmux adapter for explicit native targets."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            result: Any = {"ok": True, "profiles": list_profiles(source_root=source_root, home=home, application=args.application)}
        elif args.command == "show":
            result = {"ok": True, "profile": resolve_profile(args.application, args.name, source_root=source_root, home=home)}
        elif args.command == "save":
            result = save_profile(
                args.application,
                args.name,
                _parse_settings(args.settings_json),
                source_root=source_root,
                home=home,
                as_name=args.as_name,
            )
        elif args.command == "reset":
            result = reset_profile(args.application, args.name, source_root=source_root, home=home)
        elif args.command == "validate":
            profile = resolve_profile(args.application, args.name, source_root=source_root, home=home)
            if args.settings_json is None:
                result = validate_profile(
                    args.application, args.name, profile["settings"], source_root=source_root,
                    base_profile=profile["base_profile"],
                )
                result["digest"] = profile["digest"]
            else:
                result = validate_profile(
                    args.application,
                    args.name,
                    _parse_settings(args.settings_json),
                    source_root=source_root,
                    base_profile=profile["base_profile"],
                )
        elif args.command == "plan":
            result = plan_profile(args.application, args.name, source_root=source_root, home=home)
            if args.session_handle:
                from .native import plan_tmux_profile

                result = plan_tmux_profile(
                    result,
                    session_handle=args.session_handle,
                    source_root=source_root,
                    state_root=args.state_root,
                    owner_pid=args.owner_pid,
                    owner_starttime=args.owner_starttime,
                    agent_id=args.agent_id,
                )
        elif args.command == "apply":
            profile = resolve_profile(args.application, args.name, source_root=source_root, home=home)
            if apply_callback is not None and args.session_handle is None:
                adapter_result = apply_callback(args.application, profile["name"], profile["settings"])
                result_ok = _callback_success(adapter_result)
                result = {
                    "ok": result_ok,
                    "application": args.application,
                    "name": profile["name"],
                    "base_profile": profile["base_profile"],
                    "result": adapter_result,
                }
            elif not args.session_handle:
                result = {
                    "ok": False,
                    "application": args.application,
                    "name": profile["name"],
                    "error": "native application apply requires --session-handle",
                }
            else:
                from .native import apply_tmux_profile

                result = apply_tmux_profile(
                    profile,
                    session_handle=args.session_handle,
                    source_root=source_root,
                    state_root=args.state_root,
                    owner_pid=args.owner_pid,
                    owner_starttime=args.owner_starttime,
                    agent_id=args.agent_id,
                )
        else:  # pragma: no cover - argparse rejects unknown commands.
            raise ApplicationProfileError("unsupported application profile command")
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result.get("ok") is True else 2
    except (ApplicationProfileError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2


def _callback_success(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, dict) and isinstance(result.get("ok"), bool):
        return result["ok"]
    return True
