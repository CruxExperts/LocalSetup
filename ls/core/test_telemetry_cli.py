"""A presentation-only reader of the deterministic test telemetry snapshot."""
import json
import shutil
import sys
import time

from .test_telemetry import render, status
from .branding.terminal import color_capable


def show(project, *, watch=False, json_output=False):
    previous = None
    previous_rows = 1
    terminal = watch and not json_output and sys.stdout.isatty()
    try:
        while True:
            value = status(project)
            snapshot = value.get("snapshot")
            identity = (snapshot or {}).get("run_id"), (snapshot or {}).get("sequence"), value["status"]
            if identity != previous:
                if json_output:
                    print(json.dumps(value, sort_keys=True), flush=True)
                else:
                    width = min(80, max(1, shutil.get_terminal_size().columns)) if terminal else 80
                    display = snapshot | {"state": "stale"} if snapshot and value["status"] == "stale" else snapshot
                    line = render(display, color=color_capable(sys.stdout), width=width) if display else ("[LS] tests: " + value["status"])[:width].ljust(width)
                    if terminal:
                        rows = line.splitlines()
                        rows.extend([""] * max(0, previous_rows - len(rows)))
                        up = f"\x1b[{previous_rows - 1}A" if previous_rows > 1 else ""
                        sys.stdout.write("\r" + up + "\n".join("\r\x1b[2K" + row for row in rows))
                        sys.stdout.flush()
                        previous_rows = len(rows)
                    else:
                        print(line, flush=True)
                previous = identity
            if not watch or value["status"] != "live":
                return 0 if value["ok"] else 3
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        if terminal:
            print()
