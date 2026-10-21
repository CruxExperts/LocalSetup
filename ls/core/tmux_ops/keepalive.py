from __future__ import annotations

import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from .constants import STATE_ROOT, WAIT_PREFIX
from .sanitize import _sanitize_session
from .state import (
    _acquire_pane_operation_lock,
    _attach_command,
    _is_managed,
    _json_load,
    _json_write,
    _live_active,
    _pane_operation_lock_path,
    _release_pane_operation_lock,
    _state_dir,
)
from .sudo_gate import ensure_ready_gate
from .tmux import _is_pane_idle, _pane_target, _session_exists, _targeted_tmux, _start_tmux_wait, _wait_proc

MAX_TTL_SECONDS = 7200
MAX_REFRESHES = 24
KEEPALIVE_REFRESH_COMMAND = "sudo -n -v"


def _keepalive_path(session: str) -> Path:
    return _state_dir(session) / "keepalive.json"


def _now() -> float:
    return time.time()


def _session_error(target: str, source: str) -> tuple[str | None, dict[str, Any] | None]:
    san = _sanitize_session(target)
    if san is None:
        return None, {"error": "invalid session name", "session": target, "source": source}
    if not _session_exists(san):
        return san, {"error": "managed session not found", "session": san, "source": source}
    if not _is_managed(san):
        return san, {"error": "session is not managed", "session": san, "source": source}
    return san, None


def _coerce_positive_capped(value: int, *, cap: int, name: str) -> tuple[int | None, str | None]:
    if value <= 0:
        return None, f"{name} must be positive"
    return min(value, cap), None


def _disable_marker(session: str, marker: dict[str, Any], reason: str) -> dict[str, Any]:
    marker.update(
        {
            "state": "disabled",
            "disabled_reason": reason,
            "disabled_at": _now(),
            "last_refresh_ok": False,
        }
    )
    _json_write(_keepalive_path(session), marker)
    return marker


def _malformed_marker(session: str) -> dict[str, Any]:
    marker = {
        "session": session,
        "state": "disabled",
        "disabled_reason": "keepalive marker is malformed",
        "disabled_at": _now(),
        "last_refresh_ok": False,
        "attach_command": _attach_command(session),
    }
    _json_write(_keepalive_path(session), marker)
    return marker


def _load_marker(session: str) -> tuple[dict[str, Any] | None, str | None]:
    marker = _json_load(_keepalive_path(session))
    if marker is None:
        return None, None
    if not isinstance(marker, dict):
        return _malformed_marker(session), "keepalive marker is malformed"
    return marker, None


def _status_entry(session: str, marker: dict[str, Any] | None = None) -> dict[str, Any]:
    if marker is None:
        marker, _ = _load_marker(session)
    marker = marker or {}
    expires_at = marker.get("expires_at")
    seconds_remaining = None
    if isinstance(expires_at, (int, float)):
        seconds_remaining = max(0, int(expires_at - _now()))
    return {
        "session": session,
        "state": marker.get("state", "missing"),
        "owner": marker.get("owner"),
        "reason": marker.get("reason"),
        "expires_at": expires_at,
        "seconds_remaining": seconds_remaining,
        "refresh_count": marker.get("refresh_count", 0),
        "max_refreshes": marker.get("max_refreshes"),
        "last_refresh_ok": marker.get("last_refresh_ok"),
        "disabled_reason": marker.get("disabled_reason"),
        "attach_command": marker.get("attach_command", _attach_command(session)),
    }


def _marker_invalid_reason(session: str, marker: dict[str, Any], *, require_active: bool) -> str | None:
    if marker.get("state") != "active":
        return "keepalive marker is not active" if require_active else None
    if not _session_exists(session) or not _is_managed(session):
        return "session is not managed"
    expires_at = marker.get("expires_at")
    if not isinstance(expires_at, (int, float)) or expires_at <= _now():
        return "keepalive marker expired"
    max_refreshes = marker.get("max_refreshes")
    refresh_count = marker.get("refresh_count", 0)
    if not isinstance(max_refreshes, int) or not isinstance(refresh_count, int):
        return "keepalive marker has invalid refresh counters"
    if refresh_count >= max_refreshes:
        return "keepalive marker reached max refreshes"
    recorded_pane = marker.get("pane_id")
    if isinstance(recorded_pane, str) and recorded_pane:
        current_pane, err = _pane_target(session)
        if current_pane is None:
            return err or "unable to resolve pane target"
        if current_pane != recorded_pane:
            return "keepalive pane identity changed"
    return None


def _busy_keepalive_payload(session: str, detail: str, source: str) -> dict[str, Any]:
    marker, _ = _load_marker(session)
    marker = marker or {}
    return {
        "error": "keepalive pane operation blocked",
        "action_required": True,
        "detail": detail,
        **_status_entry(session, marker),
        "source": source,
    }


def _refresh_not_safe_reason(session: str) -> str | None:
    active = _live_active(session)
    if active:
        run_id = active.get("run_id")
        return f"run already active: {run_id}" if run_id else "run already active"
    if not _is_pane_idle(session):
        return "pane is not idle"
    return None


def _write_refresh_script(session: str, refresh_id: str) -> tuple[Path, Path, str]:
    state = _state_dir(session)
    state.mkdir(parents=True, exist_ok=True)
    script_path = state / f"keepalive-refresh-{refresh_id}.sh"
    result_path = state / f"keepalive-refresh-{refresh_id}.status.json"
    channel = f"{WAIT_PREFIX}-{session}-keepalive-refresh-{refresh_id}"
    content = f"""#!/usr/bin/env bash
set +e
{KEEPALIVE_REFRESH_COMMAND}
rc=$?
TMUX_OPS_KEEPALIVE_RESULT={shlex.quote(str(result_path))} \\
TMUX_OPS_KEEPALIVE_RC="$rc" \\
python3 - <<'PY'
import json, os, time
payload = {{
    "ok": os.environ["TMUX_OPS_KEEPALIVE_RC"] == "0",
    "exit_code": int(os.environ["TMUX_OPS_KEEPALIVE_RC"]),
    "checked_at": time.time(),
}}
with open(os.environ["TMUX_OPS_KEEPALIVE_RESULT"], "w", encoding="utf-8") as f:
    json.dump(payload, f, sort_keys=True)
    f.write("\\n")
PY
tmux wait-for -S {shlex.quote(channel)}
"""
    script_path.write_text(content, encoding="utf-8")
    script_path.chmod(0o700)
    return script_path, result_path, channel


def cmd_keepalive_request(target: str, owner: str, ttl_seconds: int, max_refreshes: int, reason: str) -> dict[str, Any]:
    san, err = _session_error(target, "keepalive request")
    if err:
        return err
    assert san is not None
    ttl, ttl_err = _coerce_positive_capped(ttl_seconds, cap=MAX_TTL_SECONDS, name="ttl_seconds")
    if ttl_err:
        return {"error": "invalid ttl_seconds", "detail": ttl_err, "session": san, "source": "keepalive request"}
    refreshes, refresh_err = _coerce_positive_capped(max_refreshes, cap=MAX_REFRESHES, name="max_refreshes")
    if refresh_err:
        return {
            "error": "invalid max_refreshes",
            "detail": refresh_err,
            "session": san,
            "source": "keepalive request",
        }

    busy = _refresh_not_safe_reason(san)
    if busy:
        return _busy_keepalive_payload(san, busy, "keepalive request")
    if _pane_operation_lock_path(san).exists():
        return _busy_keepalive_payload(san, "tmux pane operation already in progress", "keepalive request")

    gate = ensure_ready_gate(san, allow_credentials=False)
    if gate.get("sudo") != "ready" or gate.get("gate_state") != "ready":
        return {"error": "sudo gate not ready", **gate, "source": "keepalive request"}

    requested_at = _now()
    marker = {
        "session": san,
        "owner": owner,
        "reason": reason,
        "requested_at": requested_at,
        "expires_at": requested_at + int(ttl or MAX_TTL_SECONDS),
        "ttl_seconds": int(ttl or MAX_TTL_SECONDS),
        "max_refreshes": int(refreshes or MAX_REFRESHES),
        "refresh_count": 0,
        "state": "active",
        "attach_command": _attach_command(san),
        "pane_id": gate.get("pane_id"),
        "pane_tty": gate.get("pane_tty"),
        "last_refresh_ok": None,
    }
    _json_write(_keepalive_path(san), marker)
    return {"ok": True, **_status_entry(san, marker)}


def cmd_keepalive_refresh(target: str) -> dict[str, Any]:
    san, err = _session_error(target, "keepalive refresh")
    if err:
        return err
    assert san is not None
    lock_fd = _acquire_pane_operation_lock(san)
    if lock_fd is None:
        return _busy_keepalive_payload(san, "tmux pane operation already in progress", "keepalive refresh")
    try:
        marker, malformed = _load_marker(san)
        if not marker:
            return {"error": "keepalive marker not found", "session": san, "source": "keepalive refresh"}
        if malformed:
            return {
                "error": "keepalive marker disabled",
                "action_required": True,
                **_status_entry(san, marker),
                "source": "keepalive refresh",
            }
        invalid = _marker_invalid_reason(san, marker, require_active=True)
        if invalid:
            _disable_marker(san, marker, invalid)
            return {
                "error": "keepalive marker disabled",
                "action_required": True,
                **_status_entry(san, marker),
                "source": "keepalive refresh",
            }
        busy = _refresh_not_safe_reason(san)
        if busy:
            return _busy_keepalive_payload(san, busy, "keepalive refresh")
        refresh_count = int(marker.get("refresh_count", 0))
        marker["refresh_count"] = refresh_count + 1
        marker["refresh_reserved_at"] = _now()
        marker["last_refresh_ok"] = None
        _json_write(_keepalive_path(san), marker)
        refresh_id = f"{int(time.time())}-{uuid.uuid4().hex[:10]}"

        script_path, result_path, channel = _write_refresh_script(san, refresh_id)
        wait = _start_tmux_wait(channel)
        result_path.unlink(missing_ok=True)
        r = _targeted_tmux(san, ["send-keys", "-t", "{target}", f"bash {shlex.quote(str(script_path))}", "Enter"])
        if r.returncode != 0:
            marker = _load_marker(san)[0] or marker
            _disable_marker(san, marker, r.stderr or "tmux send-keys failed")
            return {
                "error": "keepalive refresh failed",
                "action_required": True,
                **_status_entry(san, marker),
                "source": "keepalive refresh",
            }
        if not _wait_proc(wait, 10.0):
            marker = _load_marker(san)[0] or marker
            _disable_marker(san, marker, "keepalive refresh timed out")
            return {
                "error": "keepalive refresh failed",
                "action_required": True,
                **_status_entry(san, marker),
                "source": "keepalive refresh",
            }
        result = _json_load(result_path) or {}
        if not result.get("ok"):
            marker = _load_marker(san)[0] or marker
            _disable_marker(san, marker, "sudo keepalive refresh failed")
            return {
                "error": "keepalive refresh failed",
                "action_required": True,
                "exit_code": result.get("exit_code"),
                **_status_entry(san, marker),
                "source": "keepalive refresh",
            }

        marker = _load_marker(san)[0] or marker
        marker["refreshed_at"] = _now()
        marker["last_refresh_ok"] = True
        marker.pop("refresh_reserved_at", None)
        _json_write(_keepalive_path(san), marker)
        return {"ok": True, **_status_entry(san, marker)}
    finally:
        _release_pane_operation_lock(san, lock_fd)


def cmd_keepalive_sweep() -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    if STATE_ROOT.exists():
        for path in sorted(STATE_ROOT.iterdir()):
            if not path.is_dir():
                continue
            san = _sanitize_session(path.name)
            if san is None:
                continue
            marker, _ = _load_marker(san)
            if not marker:
                continue
            reason = _marker_invalid_reason(san, marker, require_active=False)
            if reason:
                _disable_marker(san, marker, reason)
            sessions.append(_status_entry(san, marker))
    return {"ok": True, "sessions": sessions}


def cmd_keepalive_status(target: str | None) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    if target:
        san = _sanitize_session(target)
        if san is None:
            return {"error": "invalid session name", "session": target, "source": "keepalive status"}
        marker, _ = _load_marker(san)
        if marker:
            reason = _marker_invalid_reason(san, marker, require_active=False)
            if reason:
                _disable_marker(san, marker, reason)
        sessions.append(_status_entry(san, marker))
        return {"ok": True, "sessions": sessions}
    if STATE_ROOT.exists():
        for path in sorted(STATE_ROOT.iterdir()):
            if path.is_dir() and _sanitize_session(path.name):
                marker, _ = _load_marker(path.name)
                if marker:
                    reason = _marker_invalid_reason(path.name, marker, require_active=False)
                    if reason:
                        _disable_marker(path.name, marker, reason)
                    sessions.append(_status_entry(path.name, marker))
    return {"ok": True, "sessions": sessions}


def cmd_keepalive_clear(target: str, owner: str) -> dict[str, Any]:
    san, err = _session_error(target, "keepalive clear")
    if err:
        return err
    assert san is not None
    marker, _ = _load_marker(san)
    if not marker:
        return {"error": "keepalive marker not found", "session": san, "source": "keepalive clear"}
    if marker.get("owner") != owner:
        return {"error": "keepalive owner mismatch", "session": san, "source": "keepalive clear"}
    marker.update(
        {
            "state": "disabled",
            "disabled_reason": "cleared",
            "disabled_at": _now(),
        }
    )
    _json_write(_keepalive_path(san), marker)
    return {"ok": True, **_status_entry(san, marker)}
