"""Qualify a native pane's live controller using kernel process ancestry."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .sudo import _proc_generation_is_live, _proc_identity


def process_identity(pid: int) -> dict[str, Any]:
    """Return a live same-user Linux process generation, not an authority grant."""
    observed = _proc_identity(pid)
    if observed is None or not _proc_generation_is_live(pid, observed[1]):
        raise ValueError("controller process generation is unavailable")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not boot_id:
        raise ValueError("controller boot identity is unavailable")
    return {
        "uid": os.geteuid(), "pid": pid, "start_token": str(observed[1]),
        "boot_id": boot_id,
    }


def owner_is_ancestor(owner: Mapping[str, Any]) -> bool:
    """Require the exact live generation in this process's native ancestry."""
    try:
        if process_identity(owner["pid"]) != dict(owner):
            return False
        pid = os.getpid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            if pid == owner["pid"]:
                return True
            observed = _proc_identity(pid)
            if observed is None:
                return False
            pid = observed[0]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return False


def caller_owner(observation: Mapping[str, Any], parent_id: str, *,
                 expected_owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Bind native foreground evidence to a proper live invoking ancestor."""
    body = observation.get("result", observation)
    if not isinstance(body, Mapping):
        raise ValueError("caller pane process identity is unavailable")
    observed = body.get("process_info", body)
    if not isinstance(observed, Mapping) or observed.get("pane_id") != parent_id:
        raise ValueError("caller pane process identity is unavailable")
    leader = observed.get("foreground_process_group_id")
    rows = observed.get("foreground_processes")
    if type(leader) is not int or not isinstance(rows, list) or not any(
        isinstance(row, Mapping) and row.get("pid") == leader for row in rows
    ):
        raise ValueError("caller pane has no exact foreground controller leader")
    if not owner_is_ancestor(process_identity(leader)):
        raise ValueError("caller pane foreground leader is not this command's ancestor")
    parent = _proc_identity(leader)
    if parent is None and (leader == os.getpid() or expected_owner is not None):
        raise ValueError("caller pane foreground parent is unavailable")
    caller_pid = parent[0] if leader == os.getpid() and parent is not None else leader
    if expected_owner is not None and parent is not None and parent[0] == expected_owner.get("pid"):
        if process_identity(expected_owner["pid"]) == dict(expected_owner):
            caller_pid = expected_owner["pid"]
    identity = process_identity(caller_pid)
    if not owner_is_ancestor(identity):
        raise ValueError("caller pane leader is not this command's live owner")
    return identity
