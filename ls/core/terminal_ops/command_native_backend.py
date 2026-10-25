"""Native tmux canvas placement primitives for canvas-v3 sessions.

Workload panes remain tmux-owned PTYs. Layout changes only move a workload
pane into a verified, equal-sized placeholder; placeholders absorb all layout
reflow so an application is never repainted by a synthetic renderer.
"""

from __future__ import annotations

import re
import shlex
import os
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .command_state import ShellCommandError, process_info, process_state


_ROLE_VALUES = {"status", "operation", "parking", "placeholder"}
_PANE_READBACK = (
    "#{window_id}\t#{pane_index}\t#{pane_pid}\t#{pane_tty}\t#{pane_dead}\t#{pane_active}\t"
    "#{pane_input_off}\t#{pane_width}\t#{pane_height}\t#{pane_in_mode}\t"
    "#{pane_mode}\t#{history_size}\t#{history_limit}\t#{history_bytes}\t"
    "#{window_zoomed_flag}\t#{pane_left}\t#{pane_top}"
)
_WINDOW_READBACK = "#{window_width}\t#{window_height}\t#{window_layout}\t#{window_zoomed_flag}"
_BATCH_INSPECT = """S\t#{session_id}\t#{session_name}\t#{session_created}\t#{session_attached}\t#{@localsetup_shell_api}\t#{@localsetup_shell_handle}\t#{@localsetup_shell_key}\t#{@localsetup_shell_machine}\t#{@localsetup_shell_owner}\t#{@localsetup_shell_profile}\t#{@localsetup_shell_status_window}\t#{@localsetup_shell_layout}\t#{@localsetup_shell_layout_generation}
L\t#{session_id}\t#{session_name}
W\t#{window_id}\t#{window_name}\t#{window_active}\t#{window_panes}\t#{window_width}\t#{window_height}\t#{window_layout}\t#{window_zoomed_flag}\t#{@localsetup_shell_window_role}\t#{@localsetup_shell_generation}\t#{@localsetup_shell_session}
P\t#{window_id}\t#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{pane_dead}\t#{pane_active}\t#{pane_input_off}\t#{pane_width}\t#{pane_height}\t#{pane_in_mode}\t#{pane_mode}\t#{history_size}\t#{history_limit}\t#{history_bytes}\t#{window_zoomed_flag}\t#{pane_left}\t#{pane_top}\t#{@localsetup_shell_role}\t#{@localsetup_shell_generation}\t#{@localsetup_shell_session}\t#{@localsetup_shell_operation}\t#{@localsetup_shell_request}\t#{@localsetup_shell_label}"""


def _positive_int(value: object, *, maximum: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ShellCommandError("invalid_canvas_geometry", f"The native canvas {name} is outside its qualified range.")
    return value


def _stack_layout(columns: int, rows: int, slots: Sequence[tuple[str, int]]) -> str:
    """Encode exact tmux cells, including the first pane's top title row."""
    leaves = []
    top = 0
    for index, (pane_id, content_rows) in enumerate(slots):
        cell_rows = content_rows + (1 if index == 0 else 0)
        if not re.fullmatch(r"%[0-9]+", pane_id) or content_rows < 1:
            raise ShellCommandError("invalid_canvas_plan", "A native stack cell is invalid.")
        leaves.append(f"{columns}x{cell_rows},0,{top},{pane_id[1:]}")
        top += cell_rows + 1
    if top - 1 != rows or len(slots) < 2:
        raise ShellCommandError("invalid_canvas_plan", "Native stack cells do not fill the viewport.")
    layout = f"{columns}x{rows},0,0[" + ",".join(leaves) + "]"
    checksum = 0
    for byte in layout.encode("ascii"):
        checksum = (((checksum >> 1) | ((checksum & 1) << 15)) + byte) & 0xffff
    return f"{checksum:04x},{layout}"


def _history_retention_report(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Describe tmux reflow and capacity pressure without inventing loss counts."""
    items: dict[str, dict[str, Any]] = {}
    before_total = 0
    after_total = 0
    unknown = False
    reflow = False
    at_limit = False
    for operation_id in sorted(before):
        old = before[operation_id]
        new = after.get(operation_id)
        if not isinstance(new, Mapping):
            unknown = True
            continue
        old_history, new_history = old.get("history_size"), new.get("history_size")
        old_limit, new_limit = old.get("history_limit"), new.get("history_limit")
        old_width, new_width = old.get("width"), new.get("width")
        old_height, new_height = old.get("height"), new.get("height")
        measurements = (old_history, new_history, old_limit, new_limit,
                        old_width, new_width, old_height, new_height)
        if any(type(value) is not int for value in measurements):
            unknown = True
            continue
        before_total += old_history
        after_total += new_history
        changed_geometry = old_width != new_width or old_height != new_height
        capacity_pressure = (changed_geometry
                             and (new_width < old_width or new_height < old_height)
                             and (old_history >= old_limit or new_history >= new_limit))
        reflow |= changed_geometry
        at_limit |= capacity_pressure
        items[operation_id] = {
            "pane_id": new.get("pane_id"),
            "history_limit": new_limit, "history_size": new_history,
            "history_bytes": new.get("history_bytes"),
            "content_rows": new_height,
            "before_history_rows": old_history,
            "before_columns": old_width, "after_columns": new_width,
            "before_content_rows": old_height,
            "capacity_pressure": capacity_pressure,
        }
    state = "unknown" if unknown or len(after) != len(before) else (
        "limit" if at_limit else "reflow" if reflow else "observed")
    return {
        "state": state,
        "rows_before": before_total if not unknown else None,
        "rows_after": after_total if not unknown else None,
        "lost_rows": None,
        "operation_ids": sorted(before),
        "items": items,
        "reason": ("tmux history counters change during reflow and output; they cannot prove "
                   "how many rows were lost. Use the bounded durable log cursor for explicit gaps."),
    }


def _native_control_command(
    record: Mapping[str, Any], action: str, *, mouse: bool = False,
    mouse_event: str | None = None, mouse_pane: bool = False,
) -> str:
    """Build a tmux-expanded, shell-quoted route to the service controller."""
    command: list[str] = [
        "env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
        os.sys.executable, "-m", "ls.core.terminal_ops.command_native_control",
        "--session-record", str(record["session_record_path"]),
        "--client-pid", "#{client_pid}", "--action", action, "--quiet",
    ]
    if mouse:
        command.extend(["--mouse-x", "#{mouse_x}", "--mouse-y", "#{mouse_y}"])
    if mouse_event is not None:
        command.extend(["--mouse-event", mouse_event])
    if mouse_pane:
        command.extend(["--mouse-pane", "#{mouse_pane}"])
    return shlex.join(command)


def native_control_bindings(record: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """The bounded root-table routes for canvas-v3 keyboard and mouse controls."""
    bindings = {
        "F6": ("run-shell", "-b", _native_control_command(record, "f6")),
        "Escape": ("run-shell", "-b", _native_control_command(record, "escape")),
        "Left": ("run-shell", "-b", _native_control_command(record, "decrease_ratio")),
        "Right": ("run-shell", "-b", _native_control_command(record, "increase_ratio")),
        "MouseDown1Status": ("run-shell", "-b", _native_control_command(
            record, "mouse", mouse=True, mouse_event="down", mouse_pane=True)),
        "MouseDown1Pane": ("run-shell", "-b", _native_control_command(
            record, "mouse", mouse=True, mouse_event="down", mouse_pane=True)),
        "MouseDrag1Pane": ("run-shell", "-b", _native_control_command(
            record, "mouse", mouse=True, mouse_event="drag", mouse_pane=True)),
        "MouseUp1Pane": ("run-shell", "-b", _native_control_command(
            record, "mouse", mouse=True, mouse_event="up", mouse_pane=True)),
        "MouseDragEnd1Pane": ("run-shell", "-b", _native_control_command(
            record, "mouse", mouse=True, mouse_event="up", mouse_pane=True)),
        "MouseDown1Border": ("run-shell", "-b", _native_control_command(
            record, "border", mouse_event="down", mouse_pane=True)),
        "MouseDrag1Border": ("run-shell", "-b", _native_control_command(
            record, "border", mouse_event="drag", mouse_pane=True)),
        "MouseUp1Border": ("run-shell", "-b", _native_control_command(
            record, "border", mouse_event="up", mouse_pane=True)),
        "MouseDragEnd1Border": ("run-shell", "-b", _native_control_command(
            record, "border", mouse_event="up", mouse_pane=True)),
    }
    if (record.get("native_layout") or {}).get("grouped_sections"):
        bindings["M-Escape"] = ("run-shell", "-b", _native_control_command(record, "escape_pair"))
        shortcuts = (record.get("runtime_settings") or {}).get("native_shortcuts") or {
            "menu": "F6", "focus_next": "Tab", "focus_previous": "BTab",
            "activate": "Enter", "previous": "Up", "next": "Down",
        }
        allowed = {f"F{index}" for index in range(1, 13)} | {
            "Tab", "BTab", "Enter", "Up", "Down"}
        if (not isinstance(shortcuts, Mapping)
                or set(shortcuts) != {"menu", "focus_next", "focus_previous", "activate", "previous", "next"}
                or any(not isinstance(value, str) for value in shortcuts.values())
                or len(set(shortcuts.values())) != 6
                or any(value not in allowed for value in shortcuts.values())):
            raise ShellCommandError("native_shortcuts_invalid", "The native shortcut map is invalid.")
        bindings.pop("F6")
        for name, action in (("menu", "f6"), ("focus_next", "next_pane"),
                             ("focus_previous", "previous_pane"), ("activate", "activate"),
                             ("previous", "previous_pane"), ("next", "next_pane")):
            bindings[shortcuts[name]] = ("run-shell", "-b", _native_control_command(record, action))
    # tmux promotes a quick second or third press to its own click key name,
    # even when the pointer moved from a footer radio to a divider. Route the
    # same qualified down action so rapid controls remain responsive.
    for suffix in ("Status", "Pane", "Border"):
        for click in ("DoubleClick1", "TripleClick1"):
            bindings[click + suffix] = bindings["MouseDown1" + suffix]
    return bindings


def native_copy_escape_binding(record: Mapping[str, Any], *, bind: bool = False,
                               pair: bool = False) -> tuple[str, ...]:
    """Consume a grouped viewer's Escape before tmux leaves copy mode."""
    return ("run-shell", _native_control_command(record, "escape_pair" if pair else "escape"),
            r"\;" if bind else ";", "send-keys", "-X", "cancel")


class NativeCanvasV3:
    """Exact tmux inventory, parked native operations, and placeholder swaps."""

    def __init__(self, backend: Any):
        self.backend = backend

    @staticmethod
    def _generation(record: Mapping[str, Any]) -> int:
        layout = record.get("native_layout")
        generation = layout.get("desired_generation") if isinstance(layout, Mapping) else 0
        if type(generation) is not int or generation < 0:
            raise ShellCommandError("invalid_canvas_generation", "The native canvas generation is invalid.")
        return generation

    def inspect(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Read every window and pane, including copy, zoom, size, and role tags."""
        endpoint = str(record.get("endpoint", ""))
        endpoint_state = self.backend._endpoint_state(endpoint)
        if endpoint_state in {"missing", "stale"}:
            return {"state": "missing", "session": None, "options": {}, "identity": {},
                    "inventory": {"session": None, "options": {}, "windows": []},
                    "windows": [], "geometry": {}, "controls": {},
                    "history_retention": {"state": "not_observed", "rows_before": None,
                                          "rows_after": None, "lost_rows": None,
                                          "operation_ids": [], "reason": "Native endpoint is absent."},
                    "desired_generation": None, "applied_generation": None}
        if endpoint_state != "live":
            return {"state": "unknown", "session": None, "options": {}, "identity": {},
                    "inventory": {"session": None, "options": {}, "windows": []}, "windows": [],
                    "geometry": {}, "controls": {}, "history_retention": {},
                    "desired_generation": None, "applied_generation": None}

        target = self.backend._private_target(str(record.get("native_session_id", "")))
        command = ["display-message", "-t", target, "-p", "-F", _BATCH_INSPECT.splitlines()[0],
                   ";", "list-sessions", "-F", _BATCH_INSPECT.splitlines()[1],
                   ";", "list-windows", "-t", target, "-F", _BATCH_INSPECT.splitlines()[2],
                   ";", "list-panes", "-s", "-t", target, "-F", _BATCH_INSPECT.splitlines()[3]]
        output = self.backend._checked(endpoint, command)
        rows = output.splitlines()
        session_rows = [line.split("\t") for line in rows if line.startswith("S\t")]
        listed_sessions = [line.split("\t") for line in rows if line.startswith("L\t")]
        window_rows = [line.split("\t") for line in rows if line.startswith("W\t")]
        pane_rows = [line.split("\t") for line in rows if line.startswith("P\t")]
        if (len(session_rows) != 1 or len(session_rows[0]) != 14
                or len(listed_sessions) != 1 or len(listed_sessions[0]) != 3
                or listed_sessions[0][1] != record.get("native_session_id")
                or not window_rows or any(len(row) != 12 for row in window_rows)
                or any(len(row) != 25 for row in pane_rows)):
            raise ShellCommandError("native_inventory_unknown", "The batched native canvas readback was incomplete.", effect="unknown")
        s = session_rows[0]
        session = {"id": s[1], "name": s[2], "created": s[3],
                   "attached": int(s[4]) if s[4].isdigit() else None}
        options = dict(zip(("api", "handle", "key", "machine", "owner", "profile",
                            "status_window", "layout", "layout_generation"), s[5:14]))
        options = {key: value or None for key, value in options.items()}
        native_windows: list[dict[str, Any]] = []
        history: dict[str, dict[str, Any]] = {}
        inventory_well_formed = True
        panes_by_window: dict[str, list[dict[str, Any]]] = {}
        for fields in pane_rows:
            fields = fields[1:]
            window_id, index, pane_id, pid, tty, dead, active, input_off, width, height, in_mode, mode, hsize, hlimit, hbytes, zoom, x, y, role, generation, owner, op_id, request, label = fields
            pane = {
                "window_id": window_id, "pane_index": int(index) if index.isdigit() else None,
                "pane_id": pane_id, "pane_pid": int(pid) if pid.isdigit() else None,
                "pane_tty": tty or None, "dead": dead == "1", "active": active == "1",
                "input_off": input_off == "1", "width": int(width) if width.isdigit() else None,
                "height": int(height) if height.isdigit() else None, "in_mode": in_mode == "1",
                "mode": mode or None, "history_size": int(hsize) if hsize.isdigit() else None,
                "history_limit": int(hlimit) if hlimit.isdigit() else None,
                "history_bytes": int(hbytes) if hbytes.isdigit() else None,
                "window_zoomed": zoom == "1", "x": int(x) if x.isdigit() else None,
                "y": int(y) if y.isdigit() else None, "role": role or None,
                "operation_id": op_id or None, "generation": generation or None,
                "session": owner or None, "request": request or None,
                "label": label or None, "tagged": True,
            }
            panes_by_window.setdefault(window_id, []).append(pane)

        for fields in window_rows:
            fields = fields[1:]
            (window_id, name, active, pane_count, width, height, layout, zoom,
             role, generation, owner) = fields
            metadata = {"role": role or None, "generation": generation or None,
                        "session": owner or None}
            if metadata.get("role") not in {"status", "parking", "placeholder", "summary"}:
                inventory_well_formed = False
            if metadata.get("session") != record.get("handle"):
                inventory_well_formed = False
            if window_id == record.get("status_window_id") and metadata.get("role") != "status":
                inventory_well_formed = False
            if window_id != record.get("status_window_id") and metadata.get("role") not in {"parking", "placeholder", "summary"}:
                inventory_well_formed = False
            native_panes = panes_by_window.get(window_id, [])
            if not pane_count.isdigit() or len(native_panes) != int(pane_count):
                inventory_well_formed = False
            for pane in native_panes:
                pane_role = pane["role"]
                operation_id = pane["operation_id"]
                if (pane_role not in {"status", "operation", "placeholder", "summary"}
                        or pane.get("session") != record.get("handle")
                        or pane.get("generation") is None):
                    inventory_well_formed = False
                if pane_role in {"operation", "placeholder"} and not operation_id:
                    inventory_well_formed = False
                if pane["window_id"] != window_id or pane["width"] is None or pane["height"] is None:
                    inventory_well_formed = False
                if pane_role == "status" and pane["pane_id"] != record.get("status_pane_id"):
                    inventory_well_formed = False
                if pane_role != "status" and pane["pane_id"] == record.get("status_pane_id"):
                    inventory_well_formed = False
                if pane_role == "summary":
                    info = process_info(pane.get("pane_pid"))
                    known = record.get("summary_panes") if isinstance(record.get("summary_panes"), list) else [record]
                    expected = next((entry for entry in known
                                     if entry.get("summary_pane_id") == pane["pane_id"]), None)
                    if (expected is None
                            or pane["pane_pid"] != expected.get("summary_pane_pid")
                            or info is None or info["starttime"] != expected.get("summary_pane_starttime")):
                        inventory_well_formed = False
                if pane_role == "operation":
                    history[str(operation_id)] = {
                        "pane_id": pane["pane_id"], "history_limit": pane["history_limit"],
                        "history_size": pane["history_size"], "history_bytes": pane["history_bytes"],
                        "content_rows": pane["height"],
                    }

            native_windows.append({
                "window_id": window_id, "name": name,
                "active": active == "1", "role": metadata["role"], "metadata": metadata,
                "width": int(width) if width.isdigit() else None,
                "height": int(height) if height.isdigit() else None,
                "layout": layout or None, "zoomed": zoom == "1",
                "panes": native_panes,
            })

        current_generation = options.get("layout_generation")
        desired_generation = self._generation(record)
        pair_map: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for window in native_windows:
            for pane in window["panes"]:
                if pane["role"] in {"operation", "placeholder"}:
                    pair_map.setdefault(str(pane["operation_id"]), {"operation": [], "placeholder": []})[
                        str(pane["role"])].append(pane)
        if (len([pane for window in native_windows for pane in window["panes"]
                 if pane["role"] == "status"]) != 1):
            inventory_well_formed = False
        summary_count = (len(record["summary_panes"]) if isinstance(record.get("summary_panes"), list)
                         else int(bool(record.get("summary_pane_id"))))
        if len([pane for window in native_windows for pane in window["panes"]
                if pane["role"] == "summary"]) != summary_count:
            inventory_well_formed = False
        for pair in pair_map.values():
            if len(pair["operation"]) != 1 or len(pair["placeholder"]) != 1:
                inventory_well_formed = False
                continue
            task, placeholder = pair["operation"][0], pair["placeholder"][0]
            if (not task["window_zoomed"] and not placeholder["window_zoomed"]
                    and (task["width"] != placeholder["width"]
                         or task["height"] != placeholder["height"])):
                inventory_well_formed = False

        # A pane or window with uncertain identity is kept in the returned
        # inventory for read-only reconciliation, but cannot authorize writes.
        identity = {
            "handle": options.get("handle"), "session_key": options.get("key"),
            "machine_generation": options.get("machine"),
            "owner_fingerprint": options.get("owner"),
            "settings_digest": options.get("profile"),
            "native_session_id": session.get("id"),
            "session": session,
            "options": {name: options.get(name) for name in
                        ("api", "handle", "key", "machine", "owner", "profile", "status_window", "layout")},
            "windows": sorted(({
                "window_id": window["window_id"], "role": window["role"],
                "metadata": {key: window["metadata"].get(key)
                             for key in ("role", "generation", "session")},
                "width": window["width"], "height": window["height"],
                "layout": window["layout"], "zoomed": window["zoomed"],
                "panes": sorted(({
                    key: pane.get(key) for key in
                    ("window_id", "pane_id", "pane_index", "pane_pid", "pane_tty", "dead",
                     "active", "input_off", "width", "height", "in_mode", "mode", "window_zoomed",
                     "role", "operation_id", "generation", "request", "label", "tagged")
                } for pane in window["panes"]), key=lambda item: str(item["pane_id"])),
            } for window in native_windows), key=lambda item: str(item["window_id"])),
        }
        ready = (inventory_well_formed
                 and options.get("layout") == "canvas-v3"
                 and options.get("handle") == record.get("handle")
                 and options.get("key") == record.get("session_key")
                 and options.get("machine") == record.get("machine_generation")
                 and options.get("owner") == record.get("owner_fingerprint")
                 and options.get("profile") == record.get("settings_digest")
                 and options.get("status_window") == record.get("status_window_id"))
        if ready:
            ready = all(pane["input_off"] for window in native_windows
                        for pane in window["panes"])
        status_window = next((window for window in native_windows
                              if window["window_id"] == record.get("status_window_id")), {})
        geometry = {
            "columns": status_window.get("width"), "rows": status_window.get("height"),
            "desired_columns": record.get("canvas_width"),
            "desired_rows": record.get("primary_viewport_height"),
            "status_window_id": record.get("status_window_id"),
            "visible_operation_ids": [pane["operation_id"]
                                       for window in native_windows
                                       if window["window_id"] == record.get("status_window_id")
                                       for pane in sorted(window["panes"], key=lambda item: item["pane_index"] or 0)
                                       if pane["role"] == "operation"],
        }
        copy_mode = any(pane["in_mode"] for window in native_windows
                        for pane in window["panes"])
        zoomed = any(window["zoomed"] for window in native_windows)
        controls = {"layout_generation": current_generation,
                    "status_pane_id": record.get("status_pane_id"),
                    "copy_mode": copy_mode, "zoomed": zoomed,
                    "freeze_layout": copy_mode or zoomed,
                    "panes": [{"operation_id": pane["operation_id"],
                               "pane_id": pane["pane_id"], "x": pane["x"],
                               "y": pane["y"], "width": pane["width"],
                               "height": pane["height"], "rows": pane["height"]}
                              for window in native_windows for pane in window["panes"]
                              if pane["role"] == "operation" and pane["window_id"] == record.get("status_window_id")]}
        inventory = {"session": session,
                     "options": {name: options.get(name) for name in
                                 ("api", "handle", "key", "machine", "owner", "profile", "status_window", "layout")},
                     "windows": identity["windows"]}
        return {
            "state": "ready" if ready else "unknown", "session": session,
            "options": inventory["options"], "identity": identity,
            "inventory": inventory, "windows": native_windows, "geometry": geometry,
            "controls": controls,
            "history_retention": {"state": "not_observed", "rows_before": None,
                                  "rows_after": sum(value["history_size"] or 0 for value in history.values()),
                                  "lost_rows": None, "operation_ids": sorted(history),
                                  "items": history,
                                  "reason": "Native history counters are available; no prior placement baseline is attached to inspection."},
            "desired_generation": desired_generation,
            "applied_generation": int(current_generation) if current_generation and current_generation.isdigit() else None,
        }

    def tag_status(self, record: Mapping[str, Any]) -> None:
        """Tag the permanent control pane so it is distinct from every workload."""
        endpoint = str(record["endpoint"])
        generation = str(self._generation(record))
        for key, value in {
            "@localsetup_shell_session": str(record["handle"]),
            "@localsetup_shell_window_role": "status",
            "@localsetup_shell_generation": generation,
        }.items():
            self.backend._checked(endpoint, ["set-window-option", "-t", str(record["status_window_id"]), key, value], mutation=True)
        for key, value in {
            "@localsetup_shell_session": str(record["handle"]),
            "@localsetup_shell_role": "status",
            "@localsetup_shell_generation": generation,
        }.items():
            self.backend._checked(endpoint, ["set-option", "-p", "-t", str(record["status_pane_id"]), key, value], mutation=True)

    @staticmethod
    def _menu_text(value: object, *, maximum: int = 72) -> str:
        text = str(value)
        safe = "".join(" " if ch in "\r\n\t" or ord(ch) < 32 or ord(ch) == 127
                       else "＃" if ch == "#" else ch for ch in text)
        return safe[:maximum]

    def _client(self, record: Mapping[str, Any], client_pid: int) -> dict[str, Any]:
        if type(client_pid) is not int or client_pid <= 0:
            raise ShellCommandError("observer_identity_unknown", "The native menu client PID is invalid.")
        clients = [client for client in self.backend._clients(record)
                   if client["pid"] == client_pid]
        if (len(clients) != 1 or "read-only" in clients[0]["flags"]
                or "ignore-size" not in clients[0]["flags"]):
            raise ShellCommandError("observer_identity_unknown", "The exact primary control client could not be qualified.", effect="unknown")
        binding = record.get("native_layout", {}).get("overlay_lock")
        if isinstance(binding, Mapping) and binding.get("client_pid") == client_pid and "client_starttime" in binding:
            actual = process_info(client_pid)
            if (actual is None or actual["starttime"] != binding.get("client_starttime")
                    or actual["tty_nr"] != binding.get("client_tty_nr")):
                raise ShellCommandError("observer_identity_unknown", "The overlay client generation changed.", effect="unknown")
        return clients[0]

    def redraw_client(self, record: Mapping[str, Any], client_pid: int) -> None:
        """Repaint the exact native viewer without sending input or restarting panes."""
        client = self._client(record, client_pid)
        self.backend._checked(str(record["endpoint"]),
                              ["refresh-client", "-t", client["name"]], mutation=True)

    def detach_exact_client(self, record: Mapping[str, Any], client_pid: int) -> None:
        """Detach only the qualified viewer; leave the owner and other viewers running."""
        client = self._client(record, client_pid)
        self.backend._checked(str(record["endpoint"]),
                              ["detach-client", "-t", client["name"]], mutation=True)
        if any(item.get("pid") == client_pid for item in self.backend._clients(record)):
            raise ShellCommandError("viewer_detach_unknown", "The exact viewer detach did not read back.", effect="unknown")

    @staticmethod
    def _process_command(pid: int) -> list[bytes] | None:
        try:
            values = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return None
        return [value for value in values if value]

    def show_menu(
        self, record: Mapping[str, Any], client_pid: int, mode_label: str,
        entries: Sequence[Mapping[str, Any]], *, on_spawn: Any,
    ) -> dict[str, Any]:
        """Run a native, client-targeted menu and publish its exact helper identity."""
        client = self._client(record, client_pid)
        if not callable(on_spawn):
            raise ShellCommandError("overlay_receipt_required", "The menu needs a durable helper-identity callback.")
        if len(entries) > 100:
            raise ShellCommandError("invalid_native_menu", "The native menu exceeds its bounded entry count.")
        endpoint = str(record["endpoint"])
        actions = {"select", "mode", "ratio", "increase_ratio", "decrease_ratio",
                   "expand", "restore", "show_logs", "show_panes", "help", "page",
                   "menu", "theme", "theme_preview", "setting", "make_default"}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ShellCommandError("invalid_native_menu", "A native menu item is malformed.")
            action, operation_id, key = (entry.get("action"), entry.get("operation_id"),
                                         entry.get("key", ""))
            if (action not in actions or not isinstance(key, str) or len(key) > 8
                    or any(ord(ch) < 33 or ord(ch) > 126 for ch in key)
                    or operation_id is not None and (not isinstance(operation_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", operation_id))):
                raise ShellCommandError("invalid_native_menu", "A native menu action or shortcut is invalid.")
        if (record.get("native_layout") or {}).get("grouped_sections"):
            return self._show_grouped_menu(record, client, mode_label, entries, on_spawn=on_spawn)
        from .command_theme import menu_options
        argv: list[str] = [self.backend.tmux_binary or "tmux", "-f",
                           self.backend.config_file or "/dev/null", "-S", endpoint,
                           "display-menu", "-c", client["name"], "-t", str(record["status_pane_id"]),
                           "-T", self._menu_text(mode_label), "-x", "C", "-y", "C",
                           *menu_options(record)]
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ShellCommandError("invalid_native_menu", "A native menu item is malformed.")
            action = entry.get("action")
            operation_id = entry.get("operation_id")
            key = entry.get("key", "")
            if (action not in actions or not isinstance(key, str) or len(key) > 8
                    or any(ord(ch) < 33 or ord(ch) > 126 for ch in key)):
                raise ShellCommandError("invalid_native_menu", "A native menu action or shortcut is invalid.")
            callback = [
                "env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
                os.sys.executable, "-m", "ls.core.terminal_ops.command_native_control",
                "--session-record", str(record["session_record_path"]),
                "--client-pid", str(client_pid), "--action", str(action), "--quiet",
            ]
            if operation_id is not None:
                if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", operation_id):
                    raise ShellCommandError("invalid_native_menu", "A native menu operation identity is malformed.")
                callback.extend(["--operation-id", operation_id])
            callback_command = shlex.join(["run-shell", "-b", shlex.join(callback)])
            argv.extend([self._menu_text(entry.get("label", "")), key, callback_command])
        try:
            process = subprocess.Popen(argv, env=self.backend._environment(),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, shell=False, close_fds=True)
        except OSError as exc:
            raise ShellCommandError("native_menu_unavailable", "The exact-client native menu could not start.", effect="none") from exc
        pid = process.pid
        info = process_info(pid)
        command = self._process_command(pid)
        tmux_path = Path(self.backend.tmux_binary or "tmux").resolve()
        if (info is None or command is None or not command
                or Path(os.fsdecode(command[0])).resolve() != tmux_path
                or b"display-menu" not in command
                or os.fsencode(endpoint) not in command
                or os.fsencode(client["name"]) not in command):
            try:
                if info is not None:
                    self.backend._stop_status_bootstrap(pid, info["starttime"])
            except ShellCommandError:
                pass
            raise ShellCommandError("overlay_identity_unknown", "The menu helper did not match its exact client and endpoint.", effect="unknown")
        try:
            on_spawn(pid, info["starttime"])
        except Exception:
            closed = self.dismiss_overlay(record, client_pid, pid, info["starttime"])
            if closed.get("verified") is True:
                self.backend._stop_status_bootstrap(pid, info["starttime"])
                process.wait(timeout=1.0)
            raise ShellCommandError("overlay_receipt_unavailable", "The menu helper identity could not be durably recorded.", effect="unknown")
        return_code = process.wait()
        if return_code != 0:
            return {"state": "unknown", "verified": False,
                    "client_pid": client_pid, "overlay_pid": pid,
                    "overlay_starttime": info["starttime"], "returncode": return_code}
        dismissed = self.dismiss_overlay(record, client_pid, pid, info["starttime"])
        if dismissed.get("state") != "dismissed" or dismissed.get("verified") is not True:
            return {"state": "unknown", "verified": False,
                    "client_pid": client_pid, "overlay_pid": pid,
                    "overlay_starttime": info["starttime"], "returncode": return_code}
        return {"state": "dismissed", "verified": True,
                "client_pid": client_pid, "overlay_pid": pid,
                "overlay_starttime": info["starttime"], "returncode": return_code}

    def _show_grouped_menu(
        self, record: Mapping[str, Any], client: Mapping[str, Any], title: str,
        entries: Sequence[Mapping[str, Any]], *, on_spawn: Any,
    ) -> dict[str, Any]:
        """Navigate a qualified popup; return only an index after verified cleanup."""
        receipt = Path(str(record["session_record_path"])).parent / (
            ".menu-choice-" + uuid.uuid4().hex)
        labels = [self._menu_text(item.get("label", ""), maximum=180) for item in entries]
        program = r'''import curses, json, os, sys
labels = json.loads(sys.argv[1]); receipt_path = sys.argv[2]
def choose(index):
    descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (str(index) + '\n').encode('ascii'))
    finally:
        os.close(descriptor)
def run(screen):
    curses.curs_set(0)
    screen.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    selected = 0; top = 0
    while True:
        height, width = screen.getmaxyx()
        visible = max(1, height - 2)
        if selected < top: top = selected
        if selected >= top + visible: top = selected - visible + 1
        screen.erase()
        try:
            screen.addnstr(0, 0, 'Tab/Shift-Tab or arrows move · Enter selects · Esc closes', max(0, width - 1))
            for row, index in enumerate(range(top, min(len(labels), top + visible)), 1):
                screen.addnstr(row, 0, ('› ' if index == selected else '  ') + labels[index],
                               max(0, width - 1), curses.A_REVERSE if index == selected else 0)
            if height > 1:
                screen.addnstr(height - 1, 0, f'{selected + 1}/{len(labels)}', max(0, width - 1))
        except curses.error:
            pass
        screen.refresh()
        key = screen.getch()
        if key in (27, ord('q'), ord('Q')): return
        if not labels: continue
        if key in (9, curses.KEY_DOWN, curses.KEY_RIGHT):
            selected = (selected + 1) % len(labels)
        elif key in (curses.KEY_BTAB, curses.KEY_UP, curses.KEY_LEFT):
            selected = (selected - 1) % len(labels)
        elif key in (10, 13, curses.KEY_ENTER):
            choose(selected); return
        elif key == curses.KEY_MOUSE:
            try: _id, x, y, z, buttons = curses.getmouse()
            except curses.error: continue
            index = top + y - 1
            if (1 <= y < height - 1 and 0 <= index < len(labels)
                    and buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED |
                                   curses.BUTTON1_DOUBLE_CLICKED)):
                choose(index); return
curses.wrapper(run)'''
        from .command_theme import popup_options
        argv = [self.backend.tmux_binary or "tmux", "-f",
                self.backend.config_file or "/dev/null", "-S", str(record["endpoint"]),
                "display-popup", "-c", str(client["name"]), "-t", str(record["status_pane_id"]),
                "-T", self._menu_text(title), "-w", "80%", "-h", "80%",
                *popup_options(record), "-E", os.sys.executable, "-c", program,
                json.dumps(labels, ensure_ascii=True), str(receipt)]
        try:
            outcome = self._run_overlay_client(record, client, argv, on_spawn=on_spawn)
            if outcome.get("verified") is not True:
                return outcome
            try:
                value = receipt.read_text(encoding="ascii")
            except FileNotFoundError:
                return outcome | {"selected_index": None}
            if (not re.fullmatch(r"[0-9]+\n", value)
                    or not 0 <= int(value) < len(entries)):
                return dict(outcome) | {"state": "unknown", "verified": False}
            return dict(outcome) | {"selected_index": int(value)}
        finally:
            receipt.unlink(missing_ok=True)

    def show_overlay(
        self, record: Mapping[str, Any], client_pid: int, title: str,
        lines: Sequence[str], *, on_spawn: Any,
    ) -> dict[str, Any]:
        """Display a scrollable text popup on the exact primary client."""
        client = self._client(record, client_pid)
        if not callable(on_spawn):
            raise ShellCommandError("overlay_receipt_required", "The native overlay needs a durable helper-identity callback.")
        safe_lines = [self._menu_text(line, maximum=500) for line in lines[:256]]
        content = "\n".join(safe_lines) or "No information is available."
        prospective = record.get("_preview_theme")
        preview_receipt: Path | None = None
        if isinstance(prospective, Mapping):
            from .command_theme import validate_theme
            validate_theme(prospective)
            preview_receipt = Path(str(record["session_record_path"])).parent / (
                ".theme-preview-" + uuid.uuid4().hex)
            preview_program = r'''import json, os, sys, termios, tty
roles = json.loads(sys.argv[1])
def rgb(value):
    return ';'.join(str(int(value[i:i + 2], 16)) for i in (1, 3, 5))
old = termios.tcgetattr(0)
try:
    tty.setcbreak(0)
    bg = roles['background']; fg = roles['text']
    os.write(1, (f"\x1b[2J\x1b[H\x1b[48;2;{rgb(bg)};38;2;{rgb(fg)}m"
                 "  Candidate palette — Enter applies · Escape cancels\r\n\r\n").encode())
    for name, sample in [('surface', 'Pane background'), ('toolbar', 'Toolbar'),
                         ('selected', 'Selected control'), ('running', 'Running'),
                         ('success', 'Completed'), ('error', 'Failed'),
                         ('attention', 'Input needed')]:
        if name in ('running', 'success', 'error'):
            color = bg; ink = roles.get(name, fg)
        else:
            color = roles.get(name, bg)
            ink = roles.get('selection_text' if name in ('selected', 'attention') else 'text', fg)
        os.write(1, (f"\x1b[48;2;{rgb(color)};38;2;{rgb(ink)}m"
                     f"  {sample:<20}  {color}  \x1b[0m\r\n").encode())
    os.write(1, b"\r\n  Enter applies this theme; Escape cancels.\r\n")
    while True:
        key = os.read(0, 1)
        if key in (b'\x1b', b'q', b'Q', b''):
            break
        if key in (b'\r', b'\n'):
            receipt = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(receipt, b'accepted\n')
            finally:
                os.close(receipt)
            break
finally:
    termios.tcsetattr(0, termios.TCSADRAIN, old)
    os.write(1, b'\x1b[0m')'''
            program = preview_program
            content = json.dumps(dict(prospective["roles"]), sort_keys=True)
        else:
            program = None
        if program is None:
            program = r'''import curses, sys
lines = sys.argv[1].split("\n")
def run(screen):
    curses.curs_set(0)
    if curses.has_colors():
        curses.use_default_colors()
    screen.keypad(True)
    top = 0
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 3 or width < 2:
            screen.refresh()
            key = screen.getch()
            if key in (27, ord("q"), ord("Q")):
                return
            continue
        screen.addnstr(0, 0, "Up/Down scroll  Esc/q close", width - 1)
        limit = max(1, height - 2)
        top = min(top, max(0, len(lines) - limit))
        for index, line in enumerate(lines[top:top + limit]):
            screen.addnstr(index + 1, 0, line, width - 1)
        screen.refresh()
        key = screen.getch()
        if key in (27, ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            top = min(max(0, len(lines) - limit), top + 1)
        elif key == curses.KEY_PPAGE:
            top = max(0, top - limit)
        elif key == curses.KEY_NPAGE:
            top = min(max(0, len(lines) - limit), top + limit)
curses.wrapper(run)'''
        endpoint = str(record["endpoint"])
        from .command_theme import popup_options
        argv = [self.backend.tmux_binary or "tmux", "-f",
                self.backend.config_file or "/dev/null", "-S", endpoint,
                "display-popup", "-c", client["name"], "-t", str(record["status_pane_id"]),
                "-T", self._menu_text(title), "-w", "80%", "-h", "80%", *popup_options(record), "-E",
                os.sys.executable, "-c", program, content]
        if preview_receipt is not None:
            argv.append(str(preview_receipt))
        try:
            outcome = self._run_overlay_client(record, client, argv, on_spawn=on_spawn)
            if preview_receipt is not None and outcome.get("verified") is True:
                try:
                    accepted = preview_receipt.read_bytes() == b"accepted\n"
                except FileNotFoundError:
                    accepted = False
                return dict(outcome) | {"accepted": accepted}
            return outcome
        finally:
            if preview_receipt is not None:
                preview_receipt.unlink(missing_ok=True)

    def show_dialog(self, record: Mapping[str, Any], client_pid: int, channel: Any, *,
                    on_spawn: Any) -> dict[str, Any]:
        """Launch only the fixed renderer; form values travel over its channel."""
        from .dialog_channel import HelperIdentity

        client = self._client(record, client_pid)
        controller = process_info(os.getpid())
        endpoint = str(record["endpoint"])
        try:
            server_pid = int(self.backend._checked(endpoint, [
                "display-message", "-p", "-t", str(record["status_pane_id"]), "#{pid}",
            ]).strip())
        except (ValueError, ShellCommandError) as exc:
            raise ShellCommandError("native_dialog_unavailable", "The native server identity is unavailable.") from exc
        server = process_info(server_pid)
        if controller is None or server is None:
            raise ShellCommandError("native_dialog_unavailable", "The dialog process identities are unavailable.")
        renderer = [os.sys.executable, "-m", "ls.core.terminal_ops.dialog_helper",
                    "--socket", channel.path, "--request-id", channel.request_id,
                    "--controller-pid", str(controller["pid"]),
                    "--controller-starttime", str(controller["starttime"]),
                    "--deadline", str(channel.deadline)]

        def spawned(pid: int, starttime: int) -> None:
            on_spawn(pid, starttime)
            channel.admit(HelperIdentity(tuple(renderer), os.sys.executable,
                                         server["pid"], server["starttime"]))

        from .command_theme import popup_options
        argv = [self.backend.tmux_binary or "tmux", "-f", self.backend.config_file or "/dev/null",
                "-S", endpoint, "display-popup", "-c", client["name"],
                "-t", str(record["status_pane_id"]), "-T", "LocalSetup form",
                "-w", "90%", "-h", "90%", *popup_options(record), "-E",
                "env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]), *renderer]
        return self._run_overlay_client(record, client, argv, on_spawn=spawned, deadline=channel.deadline)

    def _run_overlay_client(
        self, record: Mapping[str, Any], client: Mapping[str, Any],
        argv: Sequence[str], *, on_spawn: Any, deadline: float | None = None,
    ) -> dict[str, Any]:
        endpoint = str(record["endpoint"])
        try:
            process = subprocess.Popen(argv, env=self.backend._environment(),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, shell=False, close_fds=True)
        except OSError as exc:
            raise ShellCommandError("native_overlay_unavailable", "The exact-client native overlay could not start.", effect="none") from exc
        pid = process.pid
        info = process_info(pid)
        command = self._process_command(pid)
        tmux_path = Path(self.backend.tmux_binary or "tmux").resolve()
        if (info is None or command is None or not command
                or Path(os.fsdecode(command[0])).resolve() != tmux_path
                or not any(b"display-menu" in value or b"display-popup" in value for value in command)
                or os.fsencode(endpoint) not in command
                or os.fsencode(client["name"]) not in command):
            raise ShellCommandError("overlay_identity_unknown", "The overlay helper did not match its exact client and endpoint.", effect="unknown")
        try:
            on_spawn(pid, info["starttime"])
        except Exception as exc:
            closed = self.dismiss_overlay(record, int(client["pid"]), pid, info["starttime"])
            if closed.get("verified") is True:
                self.backend._stop_status_bootstrap(pid, info["starttime"])
                process.wait(timeout=1.0)
            raise ShellCommandError("overlay_receipt_unavailable", "The overlay could not persist its exact helper identity.", effect="unknown") from exc
        try:
            return_code = process.wait(timeout=None if deadline is None else max(0.1, deadline - time.monotonic() + 1))
        except subprocess.TimeoutExpired:
            closed = self.dismiss_overlay(record, int(client["pid"]), pid, info["starttime"])
            if closed.get("verified") is True:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            return {"state": "expired" if process.poll() is not None else "unknown",
                    "verified": closed.get("verified") is True and process.poll() is not None,
                    "client_pid": client["pid"], "overlay_pid": pid,
                    "overlay_starttime": info["starttime"]}
        if return_code != 0:
            return {"state": "unknown", "verified": False,
                    "client_pid": client["pid"], "overlay_pid": pid,
                    "overlay_starttime": info["starttime"], "returncode": return_code}
        dismissed = self.dismiss_overlay(record, int(client["pid"]), pid, info["starttime"])
        if dismissed.get("state") != "dismissed" or dismissed.get("verified") is not True:
            return {"state": "unknown", "verified": False,
                    "client_pid": client["pid"], "overlay_pid": pid,
                    "overlay_starttime": info["starttime"], "returncode": return_code}
        return {"state": "dismissed", "verified": True,
                "client_pid": client["pid"], "overlay_pid": pid,
                "overlay_starttime": info["starttime"], "returncode": return_code}

    def dismiss_overlay(
        self, record: Mapping[str, Any], client_pid: int,
        expected_overlay_pid: int | None, expected_overlay_starttime: int | None,
    ) -> dict[str, Any]:
        """Clear only the exact primary client's menu, using tmux's native close."""
        try:
            client = self._client(record, client_pid)
        except ShellCommandError:
            binding = record.get("native_layout", {}).get("overlay_lock")
            if (isinstance(binding, Mapping) and binding.get("client_pid") == client_pid
                    and type(binding.get("client_starttime")) is int
                    and type(expected_overlay_pid) is int and type(expected_overlay_starttime) is int):
                client_info = process_info(client_pid)
                client_gone = (client_info is None or client_info["starttime"] != binding["client_starttime"]
                               or process_state(client_pid) in {"Z", "X"})
                if client_gone:
                    helper_info = process_info(expected_overlay_pid)
                    helper_gone = (helper_info is None or helper_info["starttime"] != expected_overlay_starttime
                                   or process_state(expected_overlay_pid) in {"Z", "X"})
                    if helper_gone:
                        return {"state": "not_open", "verified": True, "effect_certainty": "none"}
                    return {"state": "pending", "verified": False, "reason": "waiting_client_cleanup",
                            "effect_certainty": "none"}
            return {"state": "unknown", "verified": False}
        if expected_overlay_pid is not None:
            if type(expected_overlay_pid) is not int or type(expected_overlay_starttime) is not int:
                return {"state": "unknown", "verified": False}
            live = process_info(expected_overlay_pid)
            if live is not None:
                command = self._process_command(expected_overlay_pid)
                if (live.get("starttime") != expected_overlay_starttime or command is None
                        or not any(b"display-menu" in value or b"display-popup" in value for value in command)
                        or os.fsencode(str(record["endpoint"])) not in command
                        or os.fsencode(client["name"]) not in command):
                    return {"state": "unknown", "verified": False}
        endpoint = str(record["endpoint"])
        try:
            self.backend._checked(endpoint, ["display-popup", "-C", "-c", client["name"]], mutation=True)
            after = self._client(record, client_pid)
        except ShellCommandError:
            return {"state": "unknown", "verified": False}
        if after["name"] != client["name"] or after["pid"] != client_pid:
            return {"state": "unknown", "verified": False}
        return {"state": "dismissed", "verified": True,
                "client_pid": client_pid, "client_name": client["name"]}

    def create_operation(
        self, record: Mapping[str, Any], operation: Mapping[str, Any], *,
        command: Sequence[str], pane_rows: int | None = None,
    ) -> dict[str, Any]:
        """Create a native workload PTY and its equal-sized inert parking peer."""
        if record.get("layout_mode") != "canvas-v3":
            raise ShellCommandError("invalid_canvas_mode", "Native operation allocation requires canvas-v3.")
        from .command_backend import _family_width

        columns = _positive_int(_family_width(record), maximum=500, name="width")
        viewport_rows = _positive_int(record.get("primary_viewport_height"), maximum=512, name="height")
        rows = viewport_rows - 2 if pane_rows is None else pane_rows
        rows = _positive_int(rows, maximum=512, name="operation rows")
        if rows < 3 or rows + 2 > viewport_rows:
            raise ShellCommandError("invalid_canvas_geometry", "A native operation needs at least three content rows plus its control area.")
        endpoint = str(record["endpoint"])
        target = self.backend._private_target(str(record["native_session_id"]))
        op_id = str(operation["operation_id"])
        request = str(operation["request_digest"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", op_id):
            raise ShellCommandError("invalid_operation_identity", "The native operation identity is malformed.")
        if not re.fullmatch(r"[a-f0-9]{64}", request):
            raise ShellCommandError("invalid_operation_identity", "The native operation request digest is malformed.")
        name = str(operation.get("native_window_name", "op-" + op_id[:48]))
        output = self.backend._checked(
            endpoint,
            ["new-window", "-d", "-P", "-F", "#{window_id}\t#{pane_id}",
             "-t", target, "-n", name, "-c", str(operation["cwd"]),
             shlex.join(list(command))],
            mutation=True,
        )
        fields = output.rstrip("\n").split("\t")
        if len(fields) != 2 or not fields[0].startswith("@") or not fields[1].startswith("%"):
            raise ShellCommandError("native_identity_unknown", "tmux created no verifiable native operation pane.", effect="unknown")
        operation_window, operation_pane = fields
        self.backend._checked(endpoint, ["set-window-option", "-t", operation_window,
                                         "remain-on-exit", "on"], mutation=True)
        self.backend._blank_exit_notice(endpoint, operation_window, pane=False)
        self.backend._blank_exit_notice(endpoint, operation_pane, pane=True)
        self.backend._checked(endpoint, ["set-window-option", "-t", operation_window,
                                         "history-limit", str(record["settings"]["history_limit"])], mutation=True)
        self.backend._checked(endpoint, ["set-option", "-t", target, "window-size", "manual"], mutation=True)
        self.backend._checked(endpoint, ["resize-window", "-t", operation_window,
                                         "-x", str(columns), "-y", str(rows)], mutation=True)
        self._tag_window(record, operation_window, "parking", op_id, request)
        self._tag_pane(record, operation, operation_pane, "operation")
        self.backend._checked(endpoint, ["select-pane", "-d", "-t", operation_pane], mutation=True)

        sleep_binary = shutil.which("sleep", path=self.backend._environment().get("PATH", ""))
        if not sleep_binary:
            raise ShellCommandError("sleep_unavailable", "The inert native placeholder command is unavailable.", effect="unknown")
        placeholder_output = self.backend._checked(
            endpoint,
            ["new-window", "-d", "-P", "-F", "#{window_id}\t#{pane_id}",
             "-t", target, "-n", "ph-" + op_id[:48], "-c", str(operation["cwd"]),
             sleep_binary, "86400"],
            mutation=True,
        )
        placeholder_fields = placeholder_output.rstrip("\n").split("\t")
        if (len(placeholder_fields) != 2 or not placeholder_fields[0].startswith("@")
                or not placeholder_fields[1].startswith("%")):
            raise ShellCommandError("native_identity_unknown", "tmux created no verifiable parking placeholder.", effect="unknown")
        placeholder_window, placeholder_pane = placeholder_fields
        self.backend._checked(endpoint, ["set-window-option", "-t", placeholder_window,
                                         "remain-on-exit", "on"], mutation=True)
        self.backend._blank_exit_notice(endpoint, placeholder_window, pane=False)
        self.backend._blank_exit_notice(endpoint, placeholder_pane, pane=True)
        self.backend._checked(endpoint, ["set-window-option", "-t", placeholder_window,
                                         "history-limit", str(record["settings"]["history_limit"])], mutation=True)
        self.backend._checked(endpoint, ["set-option", "-t", target, "window-size", "manual"], mutation=True)
        self.backend._checked(endpoint, ["resize-window", "-t", placeholder_window,
                                         "-x", str(columns), "-y", str(rows)], mutation=True)
        placeholder_text = self.backend._checked(
            endpoint, ["display-message", "-p", "-t", placeholder_pane, "-F",
                       "#{pane_pid}\t#{pane_dead}\t#{pane_width}\t#{pane_height}"]
        ).strip().split("\t")
        if len(placeholder_text) != 4 or placeholder_text[1] != "0" or not placeholder_text[0].isdigit():
            raise ShellCommandError("placeholder_identity_unknown", "The inert parking process could not be qualified.", effect="unknown")
        pid = int(placeholder_text[0])
        info = process_info(pid)
        if info is None:
            raise ShellCommandError("placeholder_identity_unknown", "The inert parking process disappeared before qualification.", effect="unknown")
        self._tag_window(record, placeholder_window, "placeholder", op_id, request)
        self._tag_pane(record, operation, placeholder_pane, "placeholder")
        self.backend._checked(endpoint, ["select-pane", "-d", "-t", placeholder_pane], mutation=True)
        self.backend._stop_status_bootstrap(pid, info["starttime"])
        self._verify_dead_placeholder(record, placeholder_pane, rows, columns)
        return {
            "window_id": operation_window, "pane_id": operation_pane,
            "parking_window_id": placeholder_window, "parking_pane_id": placeholder_pane,
            "pane_rows": rows,
        }

    def remove_operation(
        self, record: Mapping[str, Any], operation: Mapping[str, Any], *,
        expected_inventory: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Remove only a completed, collected workload whose paired panes are parked."""
        observed = self.inspect(record)
        if observed.get("state") != "ready" or observed.get("inventory") != expected_inventory:
            raise ShellCommandError("native_inventory_unknown", "The complete tagged canvas changed before operation cleanup.", effect="unknown")
        if (operation.get("state") != "completed"
                or operation.get("process_scope") != "collected"
                or operation.get("output_drain") not in {"available", "unavailable"}):
            raise ShellCommandError("operation_cleanup_unqualified", "The native operation is not fully completed and collected.")
        op_id = operation.get("operation_id")
        request = operation.get("request_digest")
        pane_id = operation.get("pane_id")
        panes = {pane["pane_id"]: pane for window in observed["windows"] for pane in window["panes"]}
        task = panes.get(pane_id)
        pair = [pane for pane in panes.values()
                if pane.get("role") == "placeholder" and pane.get("operation_id") == op_id]
        if (task is None or task.get("role") != "operation"
                or task.get("operation_id") != op_id or task.get("request") != request
                or task.get("dead") is not True or task.get("window_id") == record.get("status_window_id")
                or (operation.get("runner_pid") is not None and task.get("pane_pid") != operation.get("runner_pid"))
                or len(pair) != 1 or pair[0].get("window_id") == record.get("status_window_id")):
            raise ShellCommandError("operation_cleanup_unqualified", "Only the exact dead native operation with a parked paired placeholder can be removed.", effect="unknown")
        windows = {window["window_id"]: window for window in observed["windows"]}
        task_window, placeholder_window = windows.get(task["window_id"]), windows.get(pair[0]["window_id"])
        if (task_window is None or placeholder_window is None
                or len(task_window["panes"]) != 1 or len(placeholder_window["panes"]) != 1
                or task_window["role"] != "parking"
                or placeholder_window["role"] not in {"parking", "placeholder"}):
            raise ShellCommandError("operation_cleanup_unqualified", "The native operation and placeholder are not isolated in owned parking windows.", effect="unknown")
        endpoint = str(record["endpoint"])
        self.backend._checked(endpoint, ["kill-window", "-t", placeholder_window["window_id"],
                                         ";", "kill-window", "-t", task_window["window_id"]], mutation=True)
        final = self.inspect(record)
        if (final.get("state") != "ready"
                or any(pane.get("operation_id") == op_id
                       for window in final["windows"] for pane in window["panes"])):
            raise ShellCommandError("operation_cleanup_readback_unknown", "The exact native operation and placeholder did not both disappear.", effect="unknown")
        return self._result(final, state="removed")

    def create_summary_pane(self, record: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
        """Create a read-only renderer in this newly allocated private server."""
        endpoint = str(record["endpoint"])
        command = ["env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
                   os.sys.executable, "-m", "ls.core.terminal_ops.command_canvas",
                   "--session-record", str(record["session_record_path"]), "--summaries"]
        if (record.get("native_layout") or {}).get("grouped_sections"):
            command.extend(["--summary-index", str(index)])
        output = self.backend._checked(endpoint, [
            "new-window", "-d", "-P", "-F", "#{window_id}\t#{pane_id}\t#{pane_pid}",
            "-t", self.backend._private_target(str(record["native_session_id"])),
            "-n", f"Earlier work {index + 1}", shlex.join(command)], mutation=True)
        fields = output.strip().split("\t")
        if len(fields) != 3 or not fields[0].startswith("@") or not fields[1].startswith("%") or not fields[2].isdigit():
            raise ShellCommandError("summary_identity_unknown", "The summary renderer identity is unavailable.", effect="unknown")
        window_id, pane_id, pid_text = fields
        self.backend._blank_exit_notice(endpoint, window_id, pane=False)
        self.backend._blank_exit_notice(endpoint, pane_id, pane=True)
        pid = int(pid_text)
        info = process_info(pid)
        if info is None:
            raise ShellCommandError("summary_identity_unknown", "The summary renderer process disappeared.", effect="unknown")
        self._tag_window(record, window_id, "summary")
        for key, value in (("role", "summary"), ("generation", str(self._generation(record))),
                           ("session", str(record["handle"])), ("label", f"Earlier work {index + 1}")):
            self.backend._checked(endpoint, ["set-option", "-p", "-t", pane_id,
                                             "@localsetup_shell_" + key, value], mutation=True)
        self.backend._checked(endpoint, ["select-pane", "-d", "-t", pane_id], mutation=True)
        actual = self._process_command(pid)
        current = process_info(pid)
        if (current is None or current["starttime"] != info["starttime"]
                or not actual or Path(os.fsdecode(actual[0])).resolve() != Path(os.sys.executable).resolve()
                or b"ls.core.terminal_ops.command_canvas" not in actual or b"--summaries" not in actual
                or os.fsencode(record["session_record_path"]) not in actual):
            raise ShellCommandError("summary_identity_unknown", "The summary renderer command could not be qualified.", effect="unknown")
        return {"summary_pane_id": pane_id, "summary_pane_pid": pid,
                "summary_pane_starttime": info["starttime"]}

    def create_summary_panes(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Preallocate a bounded renderer for each possible collapsed run on a page."""
        layout = record.get("native_layout", {})
        count = min(8, max(1, int(layout.get("max_sections", 8)))) if layout.get("grouped_sections") else 1
        panes = [self.create_summary_pane(record, index) for index in range(count)]
        return panes[0] | {"summary_panes": panes}

    def _tag_window(self, record: Mapping[str, Any], window_id: str, role: str,
                    operation_id: str | None = None, request: str | None = None) -> None:
        endpoint = str(record["endpoint"])
        values = {
            "@localsetup_shell_session": str(record["handle"]),
            "@localsetup_shell_window_role": role,
            "@localsetup_shell_generation": str(self._generation(record)),
        }
        if operation_id is not None:
            values["@localsetup_shell_operation"] = operation_id
        if request is not None:
            values["@localsetup_shell_request"] = request
        for key, value in values.items():
            self.backend._checked(endpoint, ["set-window-option", "-t", window_id, key, value], mutation=True)

    def _tag_pane(self, record: Mapping[str, Any], operation: Mapping[str, Any],
                  pane_id: str, role: str) -> None:
        endpoint = str(record["endpoint"])
        values = {
            "@localsetup_shell_session": str(record["handle"]),
            "@localsetup_shell_role": role,
            "@localsetup_shell_generation": str(self._generation(record)),
            "@localsetup_shell_operation": str(operation["operation_id"]),
            "@localsetup_shell_request": str(operation["request_digest"]),
            "@localsetup_shell_label": str(operation.get("pane_label", "Parked" if role == "placeholder" else "Operation")),
        }
        if role == "operation" and (record.get("native_layout") or {}).get("grouped_sections"):
            values["@localsetup_shell_state"] = "Running"
        for key, value in values.items():
            self.backend._checked(endpoint, ["set-option", "-p", "-t", pane_id, key, value], mutation=True)

    def update_title_states(self, record: Mapping[str, Any], states: Mapping[str, str],
                            expected_inventory: Mapping[str, Any]) -> None:
        """Update only qualified pane-title tags; application output is untouched."""
        observed = self.inspect(record)
        if observed.get("state") != "ready" or observed.get("inventory") != expected_inventory:
            raise ShellCommandError("native_inventory_unknown", "Pane title inventory changed before repaint.", effect="unknown")
        panes = {pane["operation_id"]: pane for window in observed["windows"]
                 for pane in window["panes"] if pane["role"] == "operation"}
        if set(states) != set(panes) or any(value not in {"Idle", "Running", "Input needed", "Completed", "Failed", "Finishing"}
                                            for value in states.values()):
            raise ShellCommandError("native_title_invalid", "Pane title states do not match the qualified operations.")
        for operation_id, state in states.items():
            pane_id = panes[operation_id]["pane_id"]
            self.backend._checked(str(record["endpoint"]),
                                  ["set-option", "-p", "-t", pane_id,
                                   "@localsetup_shell_state", state], mutation=True)
            actual = self.backend._checked(str(record["endpoint"]),
                                           ["show-options", "-p", "-t", pane_id,
                                            "-qv", "@localsetup_shell_state"]).strip()
            if actual != state:
                raise ShellCommandError("native_title_readback_unknown", "A native title state failed readback.", effect="unknown")

    def suppress_exit_notices(self, record: Mapping[str, Any],
                              expected_inventory: Mapping[str, Any]) -> None:
        """Blank tmux's dead-pane text for every exact owned canvas pane."""
        observed = self.inspect(record)
        if observed.get("state") != "ready" or observed.get("inventory") != expected_inventory:
            raise ShellCommandError("native_inventory_unknown", "Exit-notice refresh requires the exact owned inventory.", effect="unknown")
        endpoint = str(record["endpoint"])
        for window in observed["windows"]:
            self.backend._blank_exit_notice(endpoint, str(window["window_id"]), pane=False)
            for pane in window["panes"]:
                self.backend._blank_exit_notice(endpoint, str(pane["pane_id"]), pane=True)
        after = self.inspect(record)
        if after.get("state") != "ready" or after.get("inventory") != expected_inventory:
            raise ShellCommandError("native_inventory_unknown", "Exit-notice refresh changed a pane identity.", effect="unknown")

    def _verify_dead_placeholder(self, record: Mapping[str, Any], pane_id: str,
                                 rows: int, columns: int) -> None:
        endpoint = str(record["endpoint"])
        result = self.backend._checked(endpoint, ["display-message", "-p", "-t", pane_id,
                                                   "-F", "#{pane_dead}\t#{pane_width}\t#{pane_height}\t#{pane_input_off}"])
        if result.strip() != f"1\t{columns}\t{rows}\t1":
            raise ShellCommandError("placeholder_identity_unknown", "The inert native placeholder failed its exact geometry or input readback.", effect="unknown")

    def apply(self, record: Mapping[str, Any], plan: Mapping[str, Any],
              expected_inventory: Mapping[str, Any]) -> dict[str, Any]:
        """Reflow only placeholders, then swap workload panes into equal cells."""
        if record.get("layout_mode") != "canvas-v3":
            raise ShellCommandError("invalid_canvas_mode", "Native layout application requires canvas-v3.")
        if not isinstance(plan, Mapping) or plan.get("version") != 1:
            raise ShellCommandError("invalid_canvas_plan", "The native canvas plan version is unsupported.")
        generation = plan.get("generation")
        if type(generation) is not int or generation != self._generation(record):
            raise ShellCommandError("canvas_generation_mismatch", "The native canvas plan is not the persisted desired generation.")
        mode = plan.get("mode")
        if mode not in {"follow", "stable", "fixed"}:
            raise ShellCommandError("invalid_canvas_plan", "The native canvas mode is invalid.")
        viewport = plan.get("viewport")
        if not isinstance(viewport, Mapping):
            raise ShellCommandError("invalid_canvas_plan", "The native canvas viewport is missing.")
        columns = _positive_int(viewport.get("columns"), maximum=500, name="width")
        rows = _positive_int(viewport.get("rows"), maximum=512, name="height")
        if columns < 80:
            raise ShellCommandError("invalid_canvas_geometry", "The native canvas width must be at least 80 columns.")
        assignments = plan.get("assignments")
        if not isinstance(assignments, list):
            raise ShellCommandError("invalid_canvas_plan", "The native canvas assignments are malformed.")
        anchor_rows = plan.get("anchor_rows", 1)
        divider_rows = plan.get("divider_rows", 1)
        if type(anchor_rows) is not int or type(divider_rows) is not int or anchor_rows != 1 or divider_rows != 1:
            raise ShellCommandError("invalid_canvas_plan", "The native tmux backend requires a one-row control pane and one-row pane separators.")
        ids: set[str] = set()
        visible: list[dict[str, Any]] = []
        for item in assignments:
            if not isinstance(item, Mapping):
                raise ShellCommandError("invalid_canvas_plan", "A native canvas assignment is malformed.")
            op_id, pane_rows, order, show = (item.get("operation_id"), item.get("rows"),
                                              item.get("order"), item.get("visible"))
            if (not isinstance(op_id, str) or not op_id or op_id in ids
                    or type(pane_rows) is not int or not 3 <= pane_rows <= 512
                    or type(order) is not int or order < 0 or type(show) is not bool):
                raise ShellCommandError("invalid_canvas_plan", "A native canvas assignment is outside its qualified bounds.")
            ids.add(op_id)
            if show:
                visible.append(dict(item))
        visible.sort(key=lambda item: (item["order"], item["operation_id"]))
        if len({item["order"] for item in visible}) != len(visible):
            raise ShellCommandError("invalid_canvas_plan", "Visible canvas assignments have duplicate positions.")
        selected = plan.get("selected_operation_id")
        collapsed = plan.get("collapsed_sections", [])
        if (not isinstance(collapsed, list)
                or any(not isinstance(item, Mapping) or item.get("operation_id") not in ids
                       for item in collapsed)
                or len({item["operation_id"] for item in collapsed}) != len(collapsed)
                or any(item["operation_id"] in {row["operation_id"] for row in visible}
                       for item in collapsed)
                or collapsed and not record.get("summary_pane_id")):
            raise ShellCommandError("invalid_canvas_plan", "The compact summary assignments are invalid.")
        grouped = (record.get("native_layout") or {}).get("grouped_sections") is True
        sections = plan.get("sections", []) if grouped else []
        if grouped:
            if (plan.get("mode") not in {"stable", "follow", "fixed"}
                    or type(plan.get("page")) is not int or plan["page"] < 0):
                raise ShellCommandError("invalid_canvas_plan", "The grouped view label is invalid.")
            if (not isinstance(sections, list) or len(sections) > 8
                    or any(not isinstance(item, Mapping) or item.get("kind") not in {"summary", "operation"}
                           for item in sections)):
                raise ShellCommandError("invalid_canvas_plan", "The ordered native sections are invalid.")
            flattened = [op_id for item in sections for op_id in
                         (item.get("operation_ids", []) if item["kind"] == "summary" else [item.get("operation_id")])]
            if (flattened != plan.get("page_operation_ids")
                    or len(flattened) != len(set(flattened))
                    or any(item.get("rows") != len(item.get("operation_ids", []))
                           for item in sections if item["kind"] == "summary")
                    or any(item.get("rows") != next((row["rows"] for row in visible
                                                    if row["operation_id"] == item.get("operation_id")), None)
                           for item in sections if item["kind"] == "operation")):
                raise ShellCommandError("invalid_canvas_plan", "The ordered native sections do not match the page.")
        summary_rows = len(collapsed)
        summary_count = len([item for item in sections if item["kind"] == "summary"]) if grouped else int(bool(summary_rows))
        summary_cost = summary_rows + summary_count * divider_rows
        if selected is not None and (not isinstance(selected, str) or selected not in ids):
            raise ShellCommandError("invalid_canvas_plan", "The selected native operation is outside the plan.")
        if selected is not None and selected not in {item["operation_id"] for item in visible}:
            raise ShellCommandError("invalid_canvas_plan", "The selected native operation is not visible.")
        if (anchor_rows + summary_cost + sum(item["rows"] for item in visible)
                + (len(visible) + 1) * divider_rows != rows
                and visible):
            raise ShellCommandError("invalid_canvas_plan", "Native operation rows do not fill the viewport with its control row and separators.")
        if not visible and rows < 1:
            raise ShellCommandError("invalid_canvas_plan", "The native control viewport is empty.")

        before = self.inspect(record)
        if before.get("state") != "ready" or before.get("inventory") != expected_inventory:
            raise ShellCommandError("native_inventory_unknown", "The complete tagged canvas inventory changed before placement.", effect="unknown")
        if before.get("controls", {}).get("freeze_layout") is True:
            return self._result(before, state="frozen")
        panes = {pane["pane_id"]: pane for window in before["windows"] for pane in window["panes"]}
        pairs: dict[str, dict[str, dict[str, Any]]] = {}
        for pane in panes.values():
            if pane["role"] in {"operation", "placeholder"}:
                pairs.setdefault(pane["operation_id"], {})[pane["role"]] = pane
        if set(pairs) != ids or any(set(pair) != {"operation", "placeholder"} for pair in pairs.values()):
            raise ShellCommandError("native_inventory_unknown", "The tagged operation and placeholder identities do not match the plan.", effect="unknown")
        status_window = str(record["status_window_id"])
        status_pane = str(record["status_pane_id"])
        summary_slots = (record.get("summary_panes") if grouped else None) or [record]
        summary_pane_ids = [slot["summary_pane_id"] for slot in summary_slots
                            if isinstance(slot.get("summary_pane_id"), str)]
        if (summary_count > len(summary_pane_ids)
                or any(pane_id not in panes or panes[pane_id].get("role") != "summary"
                       for pane_id in summary_pane_ids)):
            raise ShellCommandError("native_inventory_unknown", "The exact summary pane is missing.", effect="unknown")
        summary_pane = summary_pane_ids[0] if summary_pane_ids else None
        summary = panes.get(summary_pane)
        desired_slots = ([(summary_pane_ids[item["summary_index"]], item["rows"])
                          if item["kind"] == "summary" else
                          (pairs[item["operation_id"]]["operation"]["pane_id"], item["rows"])
                          for item in sections] if grouped else
                         ([(summary_pane, summary_rows)] if summary_rows else []) +
                         [(pairs[item["operation_id"]]["operation"]["pane_id"], item["rows"])
                          for item in visible])
        target_rows = {item["operation_id"]: item["rows"] for item in assignments}
        current_visible = [pane["operation_id"] for pane in sorted(
            [pane for pane in panes.values() if pane["role"] == "operation" and pane["window_id"] == status_window],
            key=lambda pane: pane["pane_index"] or 0)]
        desired_visible = [item["operation_id"] for item in visible]
        layout_state = before.get("controls", {}).get("layout_generation")
        current_order_rows = {pane["operation_id"]: pane["height"] for pane in panes.values()
                              if pane["role"] == "operation"}
        no_op = (
            layout_state == str(generation)
            and before["geometry"].get("columns") == columns
            and before["geometry"].get("rows") == rows
            and current_visible == desired_visible
            and all(current_order_rows.get(item["operation_id"]) == item["rows"] for item in visible)
            and all(pane["generation"] == str(generation) for pane in panes.values())
            and panes[status_pane]["y"] + panes[status_pane]["height"] == rows
            and [(pane["pane_id"], pane["height"]) for pane in sorted(
                (pane for pane in panes.values() if pane["window_id"] == status_window
                 and pane["role"] in {"operation", "summary"}), key=lambda item: item["y"])] == desired_slots
            and (selected is None or any(pane["pane_id"] == pairs[selected]["operation"]["pane_id"]
                                         and pane["window_id"] == status_window
                                         and pane["active"]
                                         for pane in panes.values()))
        )
        if no_op:
            return self._result(self.inspect(record), state="unchanged")

        endpoint = str(record["endpoint"])
        mutations: list[str] = []

        def command(*parts: str) -> None:
            if mutations:
                mutations.append(";")
            mutations.extend(parts)

        # A single tmux command queue prevents clients from drawing the
        # temporary placeholder-only geometry between the park and restore
        # phases. All decisions below are based on the one exact preflight.
        status_placeholders = {
            pane["pane_id"]: pane for pane in panes.values()
            if pane["role"] == "placeholder" and pane["window_id"] == status_window
        }
        for op_id in current_visible:
            task, placeholder = pairs[op_id]["operation"], pairs[op_id]["placeholder"]
            self._require_equal_slot(task, placeholder)
            command("swap-pane", "-d", "-s", task["pane_id"], "-t", placeholder["pane_id"])
            status_placeholders[placeholder["pane_id"]] = placeholder
        for placeholder in status_placeholders.values():
            command("break-pane", "-d", "-s", placeholder["pane_id"])
            command("set-window-option", "-t", placeholder["pane_id"],
                    "@localsetup_shell_window_role", "placeholder")
            command("set-window-option", "-t", placeholder["pane_id"],
                    "@localsetup_shell_generation", str(generation))
            # break-pane gives its new window the status window's dimensions.
            # Restore the inert pane to its workload's existing cell before a
            # page can hide that pair; the workload PTY must never be resized.
            task = pairs[placeholder["operation_id"]]["operation"]
            command("resize-window", "-t", placeholder["pane_id"],
                    "-x", str(task["width"]), "-y", str(task["height"]))

        for pane_id in summary_pane_ids:
            if panes[pane_id]["window_id"] == status_window:
                command("break-pane", "-d", "-s", str(pane_id))
                command("set-window-option", "-t", str(pane_id),
                        "@localsetup_shell_window_role", "summary")
                command("set-window-option", "-t", str(pane_id), "pane-border-status", "off")

        # With workloads parked, only the control pane and inert slots absorb
        # the new viewport. Hidden workloads keep their last native geometry.
        command("resize-window", "-t", status_window, "-x", str(columns), "-y", str(rows))
        for item in visible:
            pair = pairs[item["operation_id"]]
            for pane in (pair["operation"], pair["placeholder"]):
                if pane["width"] != columns or pane["height"] != item["rows"]:
                    command("resize-window", "-t", pane["pane_id"],
                            "-x", str(columns), "-y", str(item["rows"]))
        desired_prepared = ([(summary_pane_ids[item["summary_index"]], item["rows"])
                            if item["kind"] == "summary" else
                            (pairs[item["operation_id"]]["placeholder"]["pane_id"], item["rows"])
                            for item in sections] if grouped else
                           ([(summary_pane, summary_rows)] if summary_rows else []) +
                           [(pairs[item["operation_id"]]["placeholder"]["pane_id"], item["rows"])
                            for item in visible])
        for pane_id, section_rows in desired_prepared:
            # Insert above the still-spacious control pane. Each insertion
            # leaves the one-row radio pane at the physical bottom.
            command("join-pane", "-d", "-v", "-b", "-l", str(section_rows),
                    "-s", str(pane_id), "-t", status_pane)
        slots = list(desired_prepared)
        if slots:
            footer_rows = rows - sum(height for _pane, height in slots) - len(slots) - 1
            slots.append((status_pane, footer_rows))
            # tmux resize-pane sizes layout cells, whose first title row differs
            # from pane_height. Apply exact cells only while workloads are parked.
            command("select-layout", "-t", status_window, _stack_layout(columns, rows, slots))
        for item in visible:
            pair = pairs[item["operation_id"]]
            self._require_equal_slot(pair["operation"], pair["placeholder"])
            command("swap-pane", "-d", "-s", pair["operation"]["pane_id"],
                    "-t", pair["placeholder"]["pane_id"])
        selected_pane = pairs[selected]["operation"]["pane_id"] if selected is not None else status_pane
        command("select-pane", "-d", "-t", selected_pane)
        target = self.backend._private_target(str(record["native_session_id"]))
        if grouped:
            command("set-option", "-t", target, "@localsetup_shell_view_mode", str(plan["mode"]))
            command("set-option", "-t", target, "@localsetup_shell_page", str(plan["page"]))
        command("set-option", "-t", target,
                "@localsetup_shell_layout_generation", str(generation))
        for pane in panes.values():
            command("set-option", "-p", "-t", pane["pane_id"],
                    "@localsetup_shell_generation", str(generation))
            command("set-window-option", "-t", pane["pane_id"],
                    "@localsetup_shell_generation", str(generation))

        history_before = {op_id: pair["operation"] for op_id, pair in pairs.items()}
        self.backend._checked_layout_queue(endpoint, mutations)
        final = self.inspect(record)
        if grouped and any(
                self.backend._checked(endpoint, ["show-options", "-t", target, "-qv", option]).strip() != expected
                for option, expected in (("@localsetup_shell_view_mode", str(plan["mode"])),
                                         ("@localsetup_shell_page", str(plan["page"])))):
            raise ShellCommandError("native_layout_readback_mismatch", "The grouped view labels did not read back.", effect="unknown")
        if (final.get("state") != "ready"
                or final.get("geometry", {}).get("visible_operation_ids") != desired_visible
                or final.get("geometry", {}).get("columns") != columns
                or final.get("geometry", {}).get("rows") != rows
                or final.get("controls", {}).get("layout_generation") != str(generation)):
            raise ShellCommandError("native_layout_readback_mismatch", "The applied native layout failed complete readback.", effect="unknown")
        final_panes = {pane["pane_id"]: pane for window in final["windows"] for pane in window["panes"]}
        footer = final_panes.get(status_pane)
        actual_slots = [(pane["pane_id"], pane["height"]) for pane in sorted(
            (pane for pane in final_panes.values() if pane["window_id"] == status_window
             and pane["role"] in {"operation", "summary"}), key=lambda item: item["y"])]
        if actual_slots != desired_slots:
            raise ShellCommandError("native_layout_readback_mismatch", "The ordered canvas sections did not read back.", effect="unknown")
        if (footer is None or footer["window_id"] != status_window
                or (visible and footer["height"] != anchor_rows)
                or footer["y"] + footer["height"] != rows):
            raise ShellCommandError("native_layout_readback_mismatch", "The native radio control pane is not the bottom row.", effect="unknown")
        for op_id, pair in pairs.items():
            task = final_panes.get(pair["operation"]["pane_id"])
            placeholder = final_panes.get(pair["placeholder"]["pane_id"])
            if task is None or placeholder is None:
                raise ShellCommandError("native_layout_readback_mismatch", "A native pane identity disappeared during placement.", effect="unknown")
            if (task["pane_pid"] != pair["operation"]["pane_pid"]
                    or task["pane_tty"] != pair["operation"]["pane_tty"]
                    or task["history_limit"] != pair["operation"]["history_limit"]
                    or task["width"] != placeholder["width"]
                    or task["height"] != placeholder["height"]):
                raise ShellCommandError("native_layout_readback_mismatch", "The workload PTY or same-size parking peer changed during placement.", effect="unknown")
            if op_id in desired_visible and (task["height"] != target_rows[op_id]
                                             or task["width"] != columns):
                raise ShellCommandError("native_layout_readback_mismatch", "A visible native operation missed its planned geometry.", effect="unknown")
        result = self._result(final, state="applied")
        result["history_retention"] = _history_retention_report(
            history_before,
            {op_id: final_panes[pair["operation"]["pane_id"]]
             for op_id, pair in pairs.items()},
        )
        return result

    def _resize_control_window(self, record: Mapping[str, Any], columns: int, rows: int) -> None:
        observed = self.inspect(record)
        windows = [window for window in observed.get("windows", [])
                   if window["window_id"] == record.get("status_window_id")]
        if (observed.get("state") != "ready" or len(windows) != 1
                or len(windows[0]["panes"]) != 1
                or windows[0]["panes"][0]["pane_id"] != record.get("status_pane_id")
                or not windows[0]["panes"][0]["input_off"]
                or windows[0]["panes"][0]["in_mode"] or windows[0]["zoomed"]):
            raise ShellCommandError("native_inventory_unknown", "Only the isolated, owned control pane can prepare canvas geometry.", effect="unknown")
        if windows[0]["width"] == columns and windows[0]["height"] == rows:
            return
        endpoint = str(record["endpoint"])
        self.backend._checked(endpoint, ["resize-window", "-t", str(record["status_window_id"]),
                                         "-x", str(columns), "-y", str(rows)], mutation=True)
        after = self.inspect(record)
        matching = [window for window in after.get("windows", [])
                    if window["window_id"] == record.get("status_window_id")]
        if (after.get("state") != "ready" or len(matching) != 1
                or matching[0]["width"] != columns or matching[0]["height"] != rows):
            raise ShellCommandError("native_resize_readback_unknown", "The control window geometry did not read back.", effect="unknown")

    def _resize_canvas_pane(self, record: Mapping[str, Any], pane_id: str, rows: int) -> None:
        observed = self.inspect(record)
        matching = [pane for window in observed.get("windows", []) for pane in window["panes"]
                    if pane["pane_id"] == pane_id]
        if (observed.get("state") != "ready" or len(matching) != 1
                or matching[0]["window_id"] != record.get("status_window_id")
                or matching[0]["role"] not in {"status", "placeholder"}
                or not matching[0]["input_off"] or matching[0]["in_mode"]
                or matching[0]["window_zoomed"]
                or any(pane["role"] == "operation" for window in observed["windows"]
                       if window["window_id"] == record.get("status_window_id")
                       for pane in window["panes"])):
            raise ShellCommandError("native_inventory_unknown", "Only inert prepared canvas panes can absorb layout changes.", effect="unknown")
        self.backend._checked(str(record["endpoint"]), ["resize-pane", "-y", str(rows),
                                                        "-t", pane_id], mutation=True)
        after = self.inspect(record)
        matching = [pane for window in after.get("windows", []) for pane in window["panes"]
                    if pane["pane_id"] == pane_id]
        if (after.get("state") != "ready" or len(matching) != 1 or matching[0]["height"] != rows):
            raise ShellCommandError("native_resize_readback_unknown", "The prepared canvas pane height did not read back.", effect="unknown")

    def _resize_parked(self, record: Mapping[str, Any], pane_id: str,
                       columns: int, rows: int) -> None:
        endpoint = str(record["endpoint"])
        observed = self.inspect(record)
        if observed.get("state") != "ready":
            raise ShellCommandError("native_inventory_unknown", "A parked pane changed before resize.", effect="unknown")
        panes = [pane for window in observed["windows"] for pane in window["panes"]
                 if pane["pane_id"] == pane_id]
        if (len(panes) != 1 or panes[0]["role"] == "status"
                or panes[0]["window_id"] == record.get("status_window_id")
                or not panes[0]["input_off"] or panes[0]["in_mode"] or panes[0]["window_zoomed"]):
            raise ShellCommandError("native_inventory_unknown", "Only an exact parked native pane can be resized.", effect="unknown")
        window_id = str(panes[0]["window_id"])
        self.backend._checked(endpoint, ["resize-window", "-t", window_id,
                                         "-x", str(columns), "-y", str(rows)], mutation=True)
        after = self.inspect(record)
        matches = [pane for window in after.get("windows", []) for pane in window["panes"]
                   if pane["pane_id"] == pane_id]
        if (after.get("state") != "ready" or len(matches) != 1
                or matches[0]["width"] != columns or matches[0]["height"] != rows):
            raise ShellCommandError("native_resize_readback_unknown", "The parked pane geometry did not read back.", effect="unknown")

    @staticmethod
    def _require_equal_slot(operation: Mapping[str, Any], placeholder: Mapping[str, Any]) -> None:
        if (operation.get("role") != "operation" or placeholder.get("role") != "placeholder"
                or operation.get("operation_id") != placeholder.get("operation_id")
                or operation.get("width") != placeholder.get("width")
                or operation.get("height") != placeholder.get("height")
                or not operation.get("input_off") or not placeholder.get("input_off")
                or operation.get("in_mode") or placeholder.get("in_mode")
                or operation.get("window_zoomed") or placeholder.get("window_zoomed")):
            raise ShellCommandError("native_slot_mismatch", "A workload pane has no exact same-size inert placeholder.", effect="unknown")

    def _set_generation(self, record: Mapping[str, Any], generation: int) -> None:
        endpoint = str(record["endpoint"])
        target = self.backend._private_target(str(record["native_session_id"]))
        self.backend._checked(endpoint, ["set-option", "-t", target,
                                         "@localsetup_shell_layout_generation", str(generation)], mutation=True)
        observed = self.inspect(record)
        if observed.get("state") != "ready":
            raise ShellCommandError("native_generation_unknown", "The native layout generation could not be read back.", effect="unknown")
        for window in observed["windows"]:
            self.backend._checked(endpoint, ["set-window-option", "-t", window["window_id"],
                                             "@localsetup_shell_generation", str(generation)], mutation=True)
            for pane in window["panes"]:
                self.backend._checked(endpoint, ["set-option", "-p", "-t", pane["pane_id"],
                                                 "@localsetup_shell_generation", str(generation)], mutation=True)

    @staticmethod
    def _result(observed: Mapping[str, Any], *, state: str) -> dict[str, Any]:
        return {
            "state": state, "session": observed.get("session"),
            "options": observed.get("options"), "identity": observed.get("identity"),
            "inventory": observed.get("inventory"), "windows": observed.get("windows"),
            "geometry": observed.get("geometry"), "controls": observed.get("controls"),
            "desired_generation": observed.get("desired_generation"),
            "applied_generation": observed.get("applied_generation"),
            "history_retention": observed.get("history_retention"),
        }
