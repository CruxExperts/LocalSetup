from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .scanner import load_policy, scan


def domain_main(argv=None, *, source_root=None, home=None) -> int:
    parser = argparse.ArgumentParser(prog="localsetup branding")
    parser.add_argument("action", choices=("validate", "render"))
    arguments = list(argv if argv is not None else sys.argv[1:])
    if not arguments or arguments[0] not in {"validate", "render"}:
        parser.parse_args(arguments)
        return 2
    action, remainder = arguments[0], arguments[1:]
    if action == "validate":
        if "--repo-root" not in remainder and source_root is not None:
            remainder = ["--repo-root", str(source_root), *remainder]
        return main(remainder)
    # Raster dependencies are optional and must not affect ordinary CLI startup.
    from .graphics import main as render_main
    return render_main(remainder, source_root=source_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory LocalSetup branding and exact review exceptions.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path, help="Defaults to ls/config/branding.json in the repository.")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero for unresolved references or visual reviews.")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        result = scan(root, load_policy(args.policy or root / "ls/config/branding.json"))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"branding validation failed ({type(exc).__name__})", file=sys.stderr)
        return 2
    result["mode"] = "strict" if args.strict else "report"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if args.strict and not result["ok"] else 0
