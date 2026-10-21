"""Shared CLI helpers for the TDD guide skill scripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class SkillCliError(Exception):
    """User-correctable CLI failure."""


def read_text(path: str | None = None, inline: str | None = None) -> str:
    """Read text from a file, inline argument, or stdin."""
    if path and inline:
        raise SkillCliError("Use either a file path or inline text, not both.")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillCliError(f"Unable to read {path}: {exc}") from exc
    if inline is not None:
        return inline
    data = sys.stdin.read()
    if not data:
        raise SkillCliError("No input provided. Pass a file, inline text, or stdin.")
    return data


def read_json(path: str | None = None, inline: str | None = None) -> Any:
    """Read JSON from a file or inline argument with actionable errors."""
    source = path or "inline JSON"
    try:
        return json.loads(read_text(path=path, inline=inline))
    except json.JSONDecodeError as exc:
        raise SkillCliError(f"Invalid JSON in {source}: {exc}") from exc


def emit_json(data: Any) -> None:
    """Write JSON output."""
    print(json.dumps(data, indent=2, sort_keys=False))


def fail(message: str) -> int:
    """Print an actionable failure message for agents and operators."""
    print(f"ERROR: {message}", file=sys.stderr)
    return 2
