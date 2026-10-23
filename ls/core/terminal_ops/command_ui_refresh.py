"""Refresh only qualified native canvas renderers in an owned session."""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import time
from typing import Any, Mapping

from .command_state import ShellCommandError, atomic_json, process_info, read_json, session_lock


def _renderer_argv(session: Mapping[str, Any], role: str, token: str) -> list[str]:
    module = ("ls.core.terminal_ops.command_native_control" if role == "status"
              else "ls.core.terminal_ops.command_canvas")
    return [sys.executable, "-m", module, "--session-record",
            str(session["session_record_path"]),
            "--serve" if role == "status" else "--summaries",
            "--ui-generation", token]


def _process_matches(pid: object, starttime: object, argv: list[str], *, token: str | None = None) -> bool:
    if type(pid) is not int or type(starttime) is not int:
        return False
    info = process_info(pid)
    if info is None or info["starttime"] != starttime:
        return False
    try:
        actual = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    expected = [os.fsencode(part) for part in argv]
    # tmux starts commands via a shell, which may exec the fixed Python module.
    return (len(actual) >= len(expected) + 1 and actual[-1] == b""
            and Path(os.fsdecode(actual[0])).resolve() == Path(sys.executable).resolve()
            and actual[:-1] == expected
            and (token is None or os.fsencode(token) in actual))


def _pane(session: Mapping[str, Any], role: str) -> tuple[str, int, int]:
    prefix = "status" if role == "status" else "summary"
    pane_id, pid, starttime = (session.get(f"{prefix}_pane_id"),
                               session.get(f"{prefix}_pane_pid"),
                               session.get(f"{prefix}_pane_starttime"))
    if (not isinstance(pane_id, str) or not pane_id.startswith("%")
            or type(pid) is not int or type(starttime) is not int):
        raise ShellCommandError("ui_renderer_unknown", "The exact UI renderer identity is unavailable.", effect="unknown")
    return pane_id, pid, starttime


def _observed_pane(observed: Mapping[str, Any], pane_id: str, role: str) -> Mapping[str, Any]:
    panes = [pane for window in observed.get("windows", [])
             for pane in window.get("panes", [])
             if pane.get("pane_id") == pane_id]
    if len(panes) != 1 or panes[0].get("role") != role or panes[0].get("dead") is not False:
        raise ShellCommandError("ui_renderer_unknown", "The exact UI renderer pane changed.", effect="unknown")
    return panes[0]


def _readback(service: Any, session: Mapping[str, Any], role: str, token: str,
              previous: int) -> tuple[int, int]:
    pane_id, _old_pid, _old_start = _pane(session, role)
    endpoint = session["endpoint"]
    argv = _renderer_argv(session, role, token)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        output = service.backend._checked(
            endpoint, ["display-message", "-t", pane_id, "-p", "-F", "#{pane_id}\t#{pane_pid}\t#{pane_dead}"],
        ).strip().split("\t")
        if len(output) == 3 and output[0] == pane_id and output[1].isdigit() and output[2] == "0":
            pid = int(output[1])
            info = process_info(pid)
            if pid != previous and info is not None and _process_matches(pid, info["starttime"], argv, token=token):
                return pid, info["starttime"]
        time.sleep(.05)
    raise ShellCommandError("ui_refresh_unknown", "The renderer replacement could not be verified.", effect="unknown")


def refresh_native_renderers(service: Any, handle: str, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Replace only the two fixed renderers; never replay an uncertain respawn."""
    directory, initial, operation_id = service._load(handle)
    if operation_id is not None:
        raise ShellCommandError("session_handle_required", "UI refresh requires a session handle.")
    with session_lock(directory):
        session = read_json(directory / "session.json")
        if session.get("handle") != handle or session.get("machine_generation") != initial.get("machine_generation"):
            raise ShellCommandError("session_identity_unknown", "The exact session identity changed.", effect="unknown")
        if session.get("session_record_path") != str(directory / "session.json"):
            raise ShellCommandError("session_identity_unknown", "The exact session record path changed.", effect="unknown")
        service._authorize(session, owner)
        if session.get("layout_mode") != "canvas-v3" or session.get("state") != "ready":
            raise ShellCommandError("native_layout_required", "UI refresh requires a ready native canvas session.")
        state = session.get("native_layout")
        if (not isinstance(state, Mapping) or state.get("overlay_lock") is not None
                or state.get("layout_state") != "applied" or state.get("pending")):
            raise ShellCommandError("ui_refresh_busy", "A native overlay or layout change is active.")
        if session.get("ui_refresh") is not None:
            raise ShellCommandError("ui_refresh_unknown", "A prior UI refresh needs read-only reconciliation.", effect="unknown")
        observed = service._verify(session)
        if observed.get("state") != "ready":
            raise ShellCommandError("native_inventory_unknown", "The native session is not qualified.", effect="unknown")
        operations = service._strict_ops(directory, session)
        service._native_inventory(session, observed, operations, allow_active=True)
        if observed.get("controls", {}).get("freeze_layout"):
            raise ShellCommandError("ui_refresh_busy", "A native viewer mode prevents UI refresh.")
        now = time.monotonic()
        if any(isinstance(lease := operation.get("input_lease"), Mapping)
               and not lease.get("revoked") and lease.get("machine_generation") == session.get("machine_generation")
               and type(lease.get("deadline_monotonic")) in {int, float}
               and now < lease["deadline_monotonic"] for operation in operations):
            raise ShellCommandError("ui_refresh_busy", "An active input lease prevents UI refresh.")
        pins, pin_unknown = service._pins(directory, remove_dead=False)
        if pin_unknown:
            raise ShellCommandError("ui_observer_unknown", "Observer identity is uncertain.", effect="unknown")
        pin_identities = {(pin.get("pin_id"), pin.get("pid"), pin.get("starttime")) for pin in pins}
        attached = observed.get("session", {}).get("attached")
        if type(attached) is not int or attached < 1:
            raise ShellCommandError("ui_observer_unknown", "No attached observer was verified.", effect="unknown")
        workload = {(pane["pane_id"], pane["pane_pid"], pane.get("pane_tty"), pane["role"])
                    for window in observed["windows"] for pane in window["panes"]
                    if pane.get("role") in {"operation", "placeholder"}}
        roles = ("status", "summary")
        for role in roles:
            pane_id, pid, starttime = _pane(session, role)
            pane = _observed_pane(observed, pane_id, role)
            if pane.get("pane_pid") != pid or pane.get("input_off") is not True:
                raise ShellCommandError("ui_renderer_unknown", "The UI renderer pane is not qualified.", effect="unknown")
            previous_token = session.get("ui_generation")
            if previous_token is not None and (not isinstance(previous_token, str)
                                               or re.fullmatch(r"[0-9a-f]{32}", previous_token) is None):
                raise ShellCommandError("ui_renderer_unknown", "The previous UI generation is invalid.", effect="unknown")
            expected = (_renderer_argv(session, role, previous_token)
                        if isinstance(previous_token, str) else _renderer_argv(session, role, "")[:-2])
            if not _process_matches(pid, starttime, expected):
                raise ShellCommandError("ui_renderer_unknown", "The UI renderer process is not qualified.", effect="unknown")
        token = secrets.token_hex(16)
        intent = {"token": token, "state": "prepared", "roles": {}, "started_at": time.time()}
        session["ui_refresh"] = intent
        atomic_json(directory / "session.json", session)
        try:
            for role in roles:
                pane_id, prior_pid, _prior_start = _pane(session, role)
                intent["state"] = "dispatching"
                intent["role"] = role
                atomic_json(directory / "session.json", session)
                command = ["env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[3]),
                           *_renderer_argv(session, role, token)]
                try:
                    service.backend._checked(session["endpoint"],
                                             ["respawn-pane", "-k", "-t", pane_id, shlex.join(command)],
                                             mutation=True)
                except ShellCommandError:
                    # A lost acknowledgement may follow a successful dispatch.
                    # Read back the exact new generation once; never resend.
                    pid, starttime = _readback(service, session, role, token, prior_pid)
                else:
                    pid, starttime = _readback(service, session, role, token, prior_pid)
                prefix = "status" if role == "status" else "summary"
                session[f"{prefix}_pane_pid"] = pid
                session[f"{prefix}_pane_starttime"] = starttime
                intent["roles"][role] = {"pane_id": pane_id, "pid": pid, "starttime": starttime}
                intent["state"] = "verified"
                atomic_json(directory / "session.json", session)
            service.backend.apply_native_control_theme(session)
            after = service._verify(session)
            if after.get("state") != "ready" or after.get("session", {}).get("attached") != attached:
                raise ShellCommandError("ui_refresh_unknown", "Native readback changed observer state.", effect="unknown")
            after_pins, after_unknown = service._pins(directory, remove_dead=False)
            if after_unknown or {(pin.get("pin_id"), pin.get("pid"), pin.get("starttime"))
                                 for pin in after_pins} != pin_identities:
                raise ShellCommandError("ui_refresh_unknown", "Observer identity changed during UI refresh.", effect="unknown")
            service._native_inventory(session, after, operations, allow_active=True)
            current_workload = {(pane["pane_id"], pane["pane_pid"], pane.get("pane_tty"), pane["role"])
                                for window in after["windows"] for pane in window["panes"]
                                if pane.get("role") in {"operation", "placeholder"}}
            if current_workload != workload:
                raise ShellCommandError("ui_refresh_unknown", "A workload pane changed during UI refresh.", effect="unknown")
            session.pop("ui_refresh", None)
            session["ui_generation"] = token
            atomic_json(directory / "session.json", session)
            return {"api_version": 1, "ok": True, "state": "refreshed", "handle": handle,
                    "status_pane_id": session["status_pane_id"], "summary_pane_id": session["summary_pane_id"]}
        except Exception as exc:
            # Once intent is durable, a mutation might have reached tmux. Keep
            # the prior on-disk intent when possible and never replay it.
            raise ShellCommandError("ui_refresh_unknown", "UI refresh needs read-only reconciliation.",
                                    effect="unknown") from exc
