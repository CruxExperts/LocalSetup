"""Read-only common preference discovery alongside the signed configuration CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .service import resolve
from .validation import schema_document


ACTIONS = {"show", "explain", "schema", "validate", "presets"}


def main(argv, *, source_root: Path, home: Path):
    parser = argparse.ArgumentParser(prog="localsetup config")
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--file", type=Path, help="Explicit invocation preference overlay")
    args = parser.parse_args(argv)
    try:
        if args.action == "schema":
            payload = schema_document(source_root)
        elif args.action == "presets":
            payload = json.loads((source_root / "ls/config/framework-presets.json").read_text())
        else:
            payload = resolve(args.project_root.resolve(), home.resolve(), source_root=source_root,
                              explicit=args.file)
            if args.action == "validate":
                payload = {"ok": True, "schema_version": 1, "loaded": payload["loaded"]}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "code": "invalid_framework_configuration",
                          "message": str(exc)}))
        return 2
