"""Explicit display-name formatting, verification and container creation."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys

from .process_identity import NameContext, NameFormat, resolve_workload, verify_process_name
from .process_naming_container import create_container


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="localsetup process-name")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("format", "verify", "create", "resolve"):
        sub = actions.add_parser(action)
        sub.add_argument("--who", required=True, help="public application name, not a command or private path")
        sub.add_argument("--scope")
        sub.add_argument("--version", help="application version supplied by the caller")
        sub.add_argument("--version-verified", action="store_true", help="caller assertion for formatting, not independent runtime evidence")
        sub.add_argument("--index", type=int, default=0)
        sub.add_argument("--no-scope", action="store_true")
        sub.add_argument("--no-version", action="store_true")
        sub.add_argument("--dispatch", action="append", default=[], help="public logical command component, in order; repeat for each level")
        if action in ("verify", "resolve"):
            sub.add_argument("--pid", type=int, required=True)
        if action == "create":
            sub.add_argument("--engine", choices=("docker", "podman"), default="docker")
            sub.add_argument("--orchestrated-name", help="preserve this orchestrator-owned name instead of generating one")
            sub.add_argument("image")
            sub.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    from .process_identity import set_process_name
    set_process_name(command_path=("localsetup", "process-name", args.action))
    context = NameContext(args.who, args.scope, args.version, args.version_verified, args.index,
                          command_path=tuple(args.dispatch))
    options = NameFormat(not args.no_scope, not args.no_version)
    try:
        resolution = resolve_workload(
            context, pid=args.pid if args.action in ("verify", "resolve") else None, options=options)
        context = resolution.context
        if args.action == "format":
            ok = resolution.compact_name is not None
            payload = {"status": "formatted" if ok else "unsupported", "full_name": resolution.full_name,
                       "expected_linux_name": resolution.compact_name,
                       "detail": resolution.compact_detail, "resolution": asdict(resolution)}
        elif args.action == "verify":
            result = verify_process_name(context, args.pid, options)
            payload = asdict(result)
            payload["expected_linux_name"] = resolution.compact_name
            payload["resolution"] = asdict(resolution)
            ok = result.status in ("verified", "truncated")
        elif args.action == "resolve":
            payload = asdict(resolution)
            ok = resolution.status == "resolved"
        else:
            declared_context = resolve_workload(context, options=options).context
            command = args.command
            if command[:1] == ["--"]:
                command = command[1:]
            result = create_container(args.engine, args.image, context, options=options, command=command,
                                      orchestrated=args.orchestrated_name is not None,
                                      create_options=["--name", args.orchestrated_name] if args.orchestrated_name is not None else ())
            payload = asdict(result)
            payload["declared_context"] = asdict(declared_context)
            ok = result.status in ("verified", "truncated", "orchestrator_name_preserved")
    except ValueError as exc:
        print(f"localsetup process-name: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0 if ok else 1
