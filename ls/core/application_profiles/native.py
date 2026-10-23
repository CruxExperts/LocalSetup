"""Lazy adapters from application profiles to owned native application state."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .service import ApplicationProfileError, validate_profile


_TMUX_NATIVE_SETTINGS = ("mouse", "status", "status_position", "history_limit")


def _owner_binding(
    *,
    owner_pid: int | None,
    owner_starttime: int | None,
    agent_id: str | None,
) -> dict[str, Any]:
    from ..terminal_ops.command_state import ShellCommandError, make_owner_binding

    if (owner_pid is None) != (owner_starttime is None):
        raise ShellCommandError(
            "owner_identity_invalid",
            "--owner-pid and --owner-starttime must be supplied together.",
        )
    if agent_id is not None and owner_pid is None:
        raise ShellCommandError(
            "owner_identity_invalid",
            "--agent-id requires an explicit owner PID and start time.",
        )
    return make_owner_binding(
        owner_pid=owner_pid,
        owner_starttime=owner_starttime,
        agent_id=agent_id,
    )


def _failure(error: Exception) -> dict[str, Any]:
    code = getattr(error, "code", None)
    message = getattr(error, "message", str(error))
    effect = getattr(error, "effect", "none")
    result: dict[str, Any] = {"ok": False, "error": message}
    if isinstance(code, str):
        result["code"] = code
    if isinstance(effect, str):
        result["effect_certainty"] = effect
    return result


def _shell_service(state_root: str | None, source_root: Path) -> Any:
    from ..terminal_ops.command_service import ShellCommandService

    return ShellCommandService(state_root, source_root=source_root)


def _bound_profile(profile: Mapping[str, Any], *, source_root: Path) -> dict[str, Any]:
    application = profile.get("application")
    name = profile.get("name")
    base_profile = profile.get("base_profile")
    settings = profile.get("settings")
    if not isinstance(application, str) or not isinstance(name, str):
        raise ApplicationProfileError("resolved profile identity is missing")
    if not isinstance(base_profile, str) or not isinstance(settings, dict):
        raise ApplicationProfileError("resolved profile base or settings are missing")
    return validate_profile(
        application,
        name,
        settings,
        source_root=source_root,
        base_profile=base_profile,
    )


def apply_tmux_profile(
    profile: Mapping[str, Any],
    *,
    session_handle: str,
    source_root: Path,
    state_root: str | None,
    owner_pid: int | None,
    owner_starttime: int | None,
    agent_id: str | None,
) -> dict[str, Any]:
    """Apply one resolved profile only to its explicitly named owned session."""
    from ..terminal_ops.command_state import ShellCommandError

    try:
        bound = _bound_profile(profile, source_root=source_root)
        if bound["application"] != "tmux":
            return {
                "ok": False,
                "application": bound["application"],
                "name": bound["name"],
                "error": "native apply is registered only for tmux",
            }
        owner = _owner_binding(
            owner_pid=owner_pid,
            owner_starttime=owner_starttime,
            agent_id=agent_id,
        )
        service = _shell_service(state_root, source_root)
        result = service.apply_profile(
            session_handle,
            bound["settings"],
            str(profile.get("digest", "")),
            owner=owner,
            profile_name=bound["name"],
            profile_base=bound["base_profile"],
        )
        return {
            **result,
            "ok": result.get("ok") is True,
            "application": bound["application"],
            "name": bound["name"],
            "base_profile": bound["base_profile"],
        }
    except (ApplicationProfileError, ShellCommandError, OSError, ValueError, TypeError) as exc:
        return _failure(exc) | {
            "application": profile.get("application"),
            "name": profile.get("name"),
        }


def plan_tmux_profile(
    plan: Mapping[str, Any],
    *,
    session_handle: str,
    source_root: Path,
    state_root: str | None,
    owner_pid: int | None,
    owner_starttime: int | None,
    agent_id: str | None,
) -> dict[str, Any]:
    """Read an exact owned session and describe profile changes without applying them."""
    from ..terminal_ops.command_state import ShellCommandError

    if plan.get("application") != "tmux":
        return {
            "ok": False,
            "application": plan.get("application"),
            "name": plan.get("name"),
            "error": "native plan inspection is registered only for tmux",
        }
    try:
        owner = _owner_binding(
            owner_pid=owner_pid,
            owner_starttime=owner_starttime,
            agent_id=agent_id,
        )
        service = _shell_service(state_root, source_root)
        target = service.status(session_handle, owner=owner)
        if target.get("ok") is not True:
            return {
                **target,
                "application": plan.get("application"),
                "name": plan.get("name"),
                "plan_scope": "native-target",
            }
        if target.get("native_state") != "ready":
            return {
                "ok": False,
                "application": plan.get("application"),
                "name": plan.get("name"),
                "plan_scope": "native-target",
                "error": "the exact tmux target is not in a verified ready state",
                "target": {
                    "handle": session_handle,
                    "state": target.get("state"),
                    "native_state": target.get("native_state"),
                },
            }
        current_settings = target.get("profile_settings")
        readback = target.get("profile_readback")
        if not isinstance(current_settings, Mapping):
            current_settings = {}
        if not isinstance(readback, Mapping):
            readback = {}
        desired = plan.get("settings")
        if not isinstance(desired, Mapping):
            raise ApplicationProfileError("profile plan settings are missing")
        diff = {
            key: {
                "current": current_settings.get(key),
                "requested": desired.get(key),
                "changed": current_settings.get(key) != desired.get(key),
            }
            for key in sorted(desired)
        }
        native_diff: dict[str, dict[str, Any]] = {}
        readback_complete = all(
            key in readback and readback[key] is not None
            for key in _TMUX_NATIVE_SETTINGS
        ) and isinstance(readback.get("history_limit"), Mapping) and all(
            value is not None for value in readback["history_limit"].values()
        )
        for key in _TMUX_NATIVE_SETTINGS:
            current = readback.get(key)
            requested = desired.get(key)
            if key == "history_limit" and isinstance(current, Mapping):
                changed = not current or any(value != requested for value in current.values())
            else:
                changed = current != requested
            native_diff[key] = {
                "current": current,
                "requested": requested,
                "changed": changed,
            }
        return {
            **dict(plan),
            "native_adapter_registered": True,
            "plan_scope": "native-target",
            "target": {
                "handle": session_handle,
                "state": target.get("state"),
                "native_state": target.get("native_state"),
                "settings_digest": target.get("settings_digest"),
                "profile_name": target.get("profile_name"),
                "profile_base": target.get("profile_base"),
            },
            "target_readback": dict(readback),
            "target_readback_complete": readback_complete,
            "diff": diff,
            "native_diff": native_diff,
            "actions": [{"kind": "apply_profile_settings", "status": "planned"}],
        }
    except (ApplicationProfileError, ShellCommandError, OSError, ValueError, TypeError) as exc:
        return _failure(exc) | {
            "application": plan.get("application"),
            "name": plan.get("name"),
            "plan_scope": "native-target",
        }
