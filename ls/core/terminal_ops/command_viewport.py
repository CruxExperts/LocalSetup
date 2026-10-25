"""Primary-viewer handoff and native canvas sizing, separate from CLI rows."""
from __future__ import annotations

import math
import re
import time

from .command_state import atomic_json, process_info


def set_viewport(session, *, columns, rows):
    """Record one desired geometry; the existing layout loop debounces it."""
    state = session["native_layout"]
    if (columns, rows) == (state["columns"], state["rows"]):
        return False
    state.update(columns=columns, rows=rows,
                 desired_generation=state["desired_generation"] + 1,
                 pending=True, activity_badge=True,
                 last_resize_monotonic=time.monotonic())
    if state.get("layout_state") not in {"applying", "dispatching", "unknown"}:
        state["layout_state"] = "resize_pending"
    session.update(primary_viewport_height=rows, canvas_width=columns,
                   canvas_width_warning=False,
                   viewport_intent={"height": rows, "width": columns})
    return True


def reconcile_primary(service, directory, session):
    """Use only known live viewers; never let a secondary shrink the canvas."""
    pins, unknown = service._pins(directory, remove_dead=False)
    if unknown or not pins:
        return False
    if any(not isinstance(pin.get("pin_id"), str)
           or re.fullmatch(r"[a-f0-9]{32}", pin["pin_id"]) is None
           or type(pin.get("created_at")) not in {int, float}
           or not math.isfinite(pin["created_at"]) for pin in pins):
        return False
    primary = [pin for pin in pins if pin.get("primary_viewport") is True]
    if len(primary) > 1:
        return False
    clients = service.backend._clients(session)
    eligible = []
    for pin in pins:
        viewer = process_info(pin.get("viewer_pid"))
        attach = process_info(pin.get("pid"))
        matches = [c for c in clients if c["pid"] == pin.get("viewer_pid")
                   and "ignore-size" in c["flags"] and "read-only" not in c["flags"]]
        if (pin.get("kind") == "attachment" and viewer and attach
                and attach["starttime"] == pin.get("starttime")
                and viewer["ppid"] == pin.get("pid") and len(matches) == 1):
            eligible.append((pin, matches[0]))
    if primary:
        chosen = next((item for item in eligible if item[0] == primary[0]), None)
        if chosen is None:
            return False  # A live primary still starting/exiting is not abandoned.
    elif eligible:
        chosen = min(eligible, key=lambda item: (item[0]["created_at"], item[0]["pin_id"]))
        pin = chosen[0]
        pin["primary_viewport"] = True
        atomic_json(directory / "pins" / ("attach-" + pin["pin_id"] + ".json"), pin)
    else:
        return False
    geometry = service.backend.observer_geometry(session, chosen[1])
    if geometry is None:
        return False
    columns, rows = geometry
    rows -= int(session["runtime_settings"]["baseline"]["status_rows"])
    if not 80 <= columns <= 500 or not 1 <= rows <= 512:
        return False  # Preserve the healthy canvas; the observer shows its warning.
    return set_viewport(session, columns=columns, rows=rows)
