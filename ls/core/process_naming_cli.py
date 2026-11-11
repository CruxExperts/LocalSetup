"""Explicit display-name formatting, verification and container creation."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys

from .process_identity import NameContext, NameFormat, format_name, verify_process_name
from .process_naming_container import create_container


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="localsetup process-name")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("format", "verify", "create"):
        sub = actions.add_parser(action)
        sub.add_argument("--who", required=True, help="public application name, not a command or private path")
        sub.add_argument("--scope")
        sub.add_argument("--version", help="application version; included only with --version-verified")
        sub.add_argument("--version-verified", action="store_true", help="assert that the supplied version was verified")
        sub.add_argument("--index", type=int, default=0)
        sub.add_argument("--no-scope", action="store_true")
        sub.add_argument("--no-version", action="store_true")
        if action == "verify":
            sub.add_argument("--pid", type=int, required=True)
        if action == "create":
            sub.add_argument("--engine", choices=("docker", "podman"), default="docker")
            sub.add_argument("--orchestrated-name", help="preserve this orchestrator-owned name instead of generating one")
            sub.add_argument("image")
            sub.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    context = NameContext(args.who, args.scope, args.version, args.version_verified, args.index)
    options = NameFormat(not args.no_scope, not args.no_version)
    try:
        if args.action == "format":
            payload = {"status": "formatted", "full_name": format_name(context, options)}
            ok = True
        elif args.action == "verify":
            result = verify_process_name(context, args.pid, options)
            payload = asdict(result)
            ok = result.status in ("verified", "truncated")
        else:
            command = args.command
            if command[:1] == ["--"]:
                command = command[1:]
            result = create_container(args.engine, args.image, context, options=options, command=command,
                                      orchestrated=args.orchestrated_name is not None,
                                      create_options=["--name", args.orchestrated_name] if args.orchestrated_name is not None else ())
            payload = asdict(result)
            ok = result.status in ("verified", "truncated", "orchestrator_name_preserved")
    except ValueError as exc:
        print(f"localsetup process-name: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0 if ok else 1
