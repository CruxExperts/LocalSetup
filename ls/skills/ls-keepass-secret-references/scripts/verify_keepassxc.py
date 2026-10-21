#!/usr/bin/env python3
"""Verify local KeePassXC CLI capabilities without opening a vault."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from typing import Any


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        shell=False,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def detect(binary: str) -> dict[str, Any]:
    path = shutil.which(binary)
    if not path:
        return {
            "installed": False,
            "binary": binary,
            "path": None,
            "version": None,
            "capabilities": {},
            "warnings": [f"{binary} not found on PATH"],
        }

    version_result = _run([path, "--version"])
    help_result = _run([path, "--help"])
    text = "\n".join([version_result.stdout, version_result.stderr]).strip()
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
    version = match.group(1) if match else None
    show_help = _run([path, "show", "--help"])
    edit_help = _run([path, "edit", "--help"])
    help_text = "\n".join([help_result.stdout, help_result.stderr])
    show_text = "\n".join([show_help.stdout, show_help.stderr])
    edit_text = "\n".join([edit_help.stdout, edit_help.stderr])
    capabilities = {
        "json_output_flag": "--format" in help_text and "json" in help_text.lower(),
        "show_attribute": "--attributes" in show_text or re.search(r"(^|\s)-a[, ]", show_text) is not None,
        "custom_attribute_edit": "--attributes" in edit_text or "--attribute" in edit_text,
        "safe_standard_fields": ["UserName", "Password", "URL", "Notes"],
    }
    return {
        "installed": True,
        "binary": binary,
        "path": path,
        "version": version,
        "capabilities": capabilities,
        "warnings": [] if version else ["Could not parse keepassxc-cli version"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="keepassxc-cli")
    parser.add_argument("--format", choices=("json", "human"), default="json")
    args = parser.parse_args(argv)
    data = detect(args.binary)
    envelope = {
        "ok": bool(data["installed"]),
        "command": "verify_keepassxc",
        "data": data,
        "warnings": data.get("warnings", []),
        "errors": [] if data["installed"] else [{"code": "missing_binary", "message": f"{args.binary} not found"}],
        "sources": [],
        "sensitive": False,
        "redactions": [],
    }
    if args.format == "json":
        print(json.dumps(envelope, indent=2, sort_keys=True))
    else:
        print(f"keepassxc-cli: {data.get('path') or 'not found'}")
        if data.get("version"):
            print(f"version: {data['version']}")
    return 0 if data["installed"] else 1


if __name__ == "__main__":
    sys.exit(main())
