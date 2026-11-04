"""Test-only clock and isolated launcher for the real routing selector."""
from __future__ import annotations

import builtins
import datetime
import json
from pathlib import Path
import runpy
import sys
import types


SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "skills/ls-agent-capability-routing/resources/model-capability-matrix/snapshot.json"
)


def fixture_date(field: str = "reviewed_at") -> datetime.date:
    return datetime.date.fromisoformat(json.loads(SNAPSHOT.read_text(encoding="utf-8"))[field])


def frozen_date(today: datetime.date) -> type[datetime.date]:
    class FrozenDate(datetime.date):
        @classmethod
        def today(cls) -> datetime.date:
            return cls(today.year, today.month, today.day)

    return FrozenDate


def freeze_selector(module, today: datetime.date) -> None:
    # Replace the selector's module binding, not the interpreter-wide datetime module.
    module.dt = types.SimpleNamespace(date=frozen_date(today))


def selector_command(script: Path, *args: str, today: datetime.date | None = None) -> list[str]:
    return [
        sys.executable, "-I", str(Path(__file__).resolve()),
        (today or fixture_date()).isoformat(), str(script), *args,
    ]


if __name__ == "__main__":
    today = datetime.date.fromisoformat(sys.argv[1])
    script = sys.argv[2]
    sys.argv = [script, *sys.argv[3:]]
    original_import = builtins.__import__

    def import_with_clock(name, globals=None, locals=None, fromlist=(), level=0):
        imported = original_import(name, globals, locals, fromlist, level)
        if name == "datetime" and globals and globals.get("__file__") == script:
            builtins.__import__ = original_import
            return types.SimpleNamespace(date=frozen_date(today))
        return imported

    builtins.__import__ = import_with_clock
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        builtins.__import__ = original_import
