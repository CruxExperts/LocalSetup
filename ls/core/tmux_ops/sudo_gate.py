from __future__ import annotations

import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from .constants import WAIT_PREFIX
from .state import (
    _acquire_pane_operation_lock,
    _attach_command,
    _json_load,
    _json_write,
    _release_pane_operation_lock,
    _state_dir,
)
from .tmux import (
    _is_pane_idle,
    _is_pane_waiting_sudo,
    _pane_target,
    _pane_tty,
    _start_tmux_wait,
    _targeted_tmux,
    _wait_proc,
)

DEFAULT_GATE_FRESHNESS_SECONDS = 60.0


def _sudo_gate_path(session: str) -> Path:
    return _state_dir(session) / "sudo_gate.json"


def _probe_status_path(session: str) -> Path:
    return _state_dir(session) / "probe.status.json"


def _probe_pending_path(session: str) -> Path:
    return _state_dir(session) / "probe.pending.json"


def _probe_auth_blocked_path(session: str) -> Path:
    return _state_dir(session) / "probe.auth-blocked.json"


def _probe_result_path(session: str, operation_id: str) -> Path:
    return _state_dir(session) / f"probe-{operation_id}.status.json"


def _probe_script_path(session: str, operation_id: str) -> Path:
    return _state_dir(session) / f"probe-{operation_id}.sh"


def _pane_identity(session: str) -> tuple[dict[str, str | None], str | None]:
    pane_id, pane_err = _pane_target(session)
    if pane_id is None:
        return {"pane_id": None, "pane_tty": None}, pane_err
    pane_tty, tty_err = _pane_tty(session)
    if pane_tty is None:
        return {"pane_id": pane_id, "pane_tty": None}, tty_err
    return {"pane_id": pane_id, "pane_tty": pane_tty}, None


def _action_payload(
    session: str,
    *,
    sudo: str,
    detail: str = "",
    checked_at: float | None = None,
    probe_command: str = "sudo -Nnv",
    pane_id: str | None = None,
    pane_tty: str | None = None,
    operation_id: str | None = None,
    credential_outcome: str = "not_checked",
    credential_variable: str | None = None,
    credential_candidates: list[dict[str, str]] | None = None,
    credential_candidates_checked: bool = False,
) -> dict[str, Any]:
    checked = checked_at or time.time()
    return {
        "session": session,
        "sudo": sudo,
        "gate_state": sudo,
        "action_required": sudo != "ready",
        "user_command": "sudo -v" if sudo == "password_required" else None,
        "attach_command": _attach_command(session),
        "next_probe_command": f"tmux_ops probe -t {session}",
        "pane_id": pane_id,
        "pane_tty": pane_tty,
        "checked_at": checked,
        "ts": checked,
        "probe_command": probe_command,
        "detail": detail,
        "operation_id": operation_id,
        "credential_outcome": credential_outcome,
        "credential_variable": credential_variable,
        "credential_candidates": credential_candidates or [],
        "credential_candidates_checked": credential_candidates_checked,
    }


def _write_gate_payload(session: str, payload: dict[str, Any]) -> dict[str, Any]:
    _json_write(_sudo_gate_path(session), payload)
    _json_write(_probe_status_path(session), payload)
    return payload


def _write_probe_script(
    session: str,
    operation_id: str,
    identity: dict[str, str | None],
    *,
    allow_credentials: bool,
) -> Path:
    state = _state_dir(session)
    state.mkdir(parents=True, exist_ok=True)
    path = _probe_script_path(session, operation_id)
    result_path = _probe_result_path(session, operation_id)
    channel = f"{WAIT_PREFIX}-{session}-probe-{operation_id}"
    auth_helper = Path(__file__).with_name("sudo_auth.py").resolve()
    content = f"""#!/usr/bin/env bash
set +e
TMUX_PROBE_OPERATION_ID={shlex.quote(operation_id)} \\
TMUX_PROBE_SESSION={shlex.quote(session)} \\
TMUX_PROBE_PANE_ID={shlex.quote(str(identity.get("pane_id") or ""))} \\
TMUX_PROBE_PANE_TTY={shlex.quote(str(identity.get("pane_tty") or ""))} \\
TMUX_PROBE_RESULT={shlex.quote(str(result_path))} \\
TMUX_PROBE_CHANNEL={shlex.quote(channel)} \\
TMUX_PROBE_ALLOW_CREDENTIALS={"1" if allow_credentials else "0"} \\
TMUX_PROBE_HOSTNAME="$(hostname 2>/dev/null)" python3 {shlex.quote(str(auth_helper))} >/dev/null 2>&1
"""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)
    return path


def _payload_from_result(session: str, result: dict[str, Any]) -> dict[str, Any] | None:
    sudo = result.get("sudo")
    if not isinstance(sudo, str) or sudo not in {"ready", "password_required", "failed"}:
        return None
    candidates = result.get("credential_candidates", [])
    if not isinstance(candidates, list):
        return None
    safe_candidates: list[dict[str, str]] = []
    for item in candidates:
        if not isinstance(item, dict):
            return None
        variable = item.get("variable")
        status = item.get("status")
        if not isinstance(variable, str) or not isinstance(status, str):
            return None
        valid_name = variable == "GENERIC_SUDO_KEY" or (
            variable.endswith("_SUDO_KEY")
            and len(variable) > len("_SUDO_KEY")
            and all(char.isupper() or char.isdigit() or char in "._-" for char in variable[:-9])
        )
        if not valid_name or status not in {"unavailable", "empty", "rejected", "used", "ambiguous"}:
            return None
        safe_candidates.append({"variable": variable, "status": status})
    outcome = result.get("credential_outcome")
    if not isinstance(outcome, str) or outcome not in {
        "cached",
        "automatic",
        "manual",
        "no_env",
        "blocked",
        "not_checked",
    }:
        outcome = "not_checked"
    variable = result.get("credential_variable")
    if not isinstance(variable, str) or not any(
        item["variable"] == variable and item["status"] == "used" for item in safe_candidates
    ):
        variable = None
    detail = result.get("detail")
    probe_command = result.get("probe_command")
    if not isinstance(probe_command, str) or probe_command not in {"sudo -Nnv", "sudo -vn"}:
        probe_command = "sudo -Nnv"
    allowed_details = {
        "",
        "sudo probe failed safely; refusing credential fallback",
        "sudo probe timed out; refusing credential fallback",
        "sudo probe could not be reconciled; refusing credential fallback",
        "sudo is unavailable, denied, or blocked by policy",
        "manual sudo validation is required; automatic credentials are disabled for this operation",
        "sudo credential check timed out; refusing fallback",
        "sudo credential check could not be reconciled; refusing fallback",
        "sudo credential result is ambiguous; refusing fallback",
        "sudo credentials were rejected or unavailable; manual sudo validation is required",
        "no non-empty sudo credential variables were available; manual sudo validation is required",
    }
    return _action_payload(
        session,
        sudo=sudo,
        detail=detail if isinstance(detail, str) and detail in allowed_details else "sudo probe result detail was invalid",
        checked_at=result.get("checked_at") if isinstance(result.get("checked_at"), (int, float)) else None,
        probe_command=probe_command,
        pane_id=result.get("pane_id") if isinstance(result.get("pane_id"), str) else None,
        pane_tty=result.get("pane_tty") if isinstance(result.get("pane_tty"), str) else None,
        operation_id=result.get("operation_id") if isinstance(result.get("operation_id"), str) else None,
        credential_outcome=outcome,
        credential_variable=variable,
        credential_candidates=safe_candidates,
        credential_candidates_checked=result.get("credential_candidates_checked") is True,
    )


def _reconcile_pending(session: str, identity: dict[str, str | None]) -> dict[str, Any]:
    pending_path = _probe_pending_path(session)
    pending = _json_load(pending_path)
    if not isinstance(pending, dict):
        return _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="sudo probe pending record is malformed; refusing another probe",
                pane_id=identity.get("pane_id"),
                pane_tty=identity.get("pane_tty"),
            ),
        )
    operation_id = pending.get("operation_id")
    if not isinstance(operation_id, str) or len(operation_id) != 32 or any(ch not in "0123456789abcdef" for ch in operation_id):
        return _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="sudo probe pending record is invalid; refusing another probe",
                pane_id=identity.get("pane_id"),
                pane_tty=identity.get("pane_tty"),
            ),
        )
    result = _json_load(_probe_result_path(session, operation_id))
    if not isinstance(result, dict):
        return _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="sudo probe outcome is uncertain; awaiting the same operation result",
                pane_id=identity.get("pane_id"),
                pane_tty=identity.get("pane_tty"),
                operation_id=operation_id,
                credential_outcome="blocked",
            ),
        )
    if (
        result.get("operation_id") != operation_id
        or result.get("session") != session
        or result.get("pane_id") != pending.get("pane_id")
        or result.get("pane_tty") != pending.get("pane_tty")
    ):
        return _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="sudo probe result did not match its pending operation; refusing another probe",
                pane_id=identity.get("pane_id"),
                pane_tty=identity.get("pane_tty"),
                operation_id=operation_id,
                credential_outcome="blocked",
            ),
        )
    if not _is_pane_idle(session):
        return _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="sudo probe result is present but the pane has not returned to idle",
                pane_id=identity.get("pane_id"),
                pane_tty=identity.get("pane_tty"),
                operation_id=operation_id,
                credential_outcome="blocked",
            ),
        )
    payload = _payload_from_result(session, result)
    if payload is None:
        return _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="sudo probe result was invalid; refusing another probe",
                pane_id=identity.get("pane_id"),
                pane_tty=identity.get("pane_tty"),
                operation_id=operation_id,
                credential_outcome="blocked",
            ),
        )
    if identity != {"pane_id": pending.get("pane_id"), "pane_tty": pending.get("pane_tty")}:
        return _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="tmux pane identity changed while sudo probe was running",
                pane_id=identity.get("pane_id"),
                pane_tty=identity.get("pane_tty"),
                operation_id=operation_id,
                credential_outcome="blocked",
            ),
        )
    if payload.get("sudo") == "ready" and payload.get("credential_outcome") == "cached":
        try:
            _probe_auth_blocked_path(session).unlink(missing_ok=True)
        except OSError:
            # A stale auth fence only disables future automatic attempts; it is safe to retain.
            pass
    elif payload.get("sudo") != "ready" and (
        payload.get("credential_outcome") == "blocked"
        or (payload.get("credential_outcome") == "manual" and pending.get("allow_credentials") is True)
    ):
        _json_write(
            _probe_auth_blocked_path(session),
            {"operation_id": operation_id, "created_at": time.time(), "reason": "manual or ambiguous sudo outcome"},
        )
    _write_gate_payload(session, payload)
    try:
        pending_path.unlink()
    except OSError:
        payload = _action_payload(
            session,
            sudo="failed",
            detail="sudo probe result was reconciled but its pending fence could not be cleared",
            pane_id=identity.get("pane_id"),
            pane_tty=identity.get("pane_tty"),
            operation_id=operation_id,
            credential_outcome="blocked",
        )
        return _write_gate_payload(session, payload)
    return payload


def probe_sudo_gate(session: str, *, allow_credentials: bool = True) -> dict[str, Any]:
    identity, err = _pane_identity(session)
    if err:
        return _action_payload(
            session,
            sudo="failed",
            detail=err,
            pane_id=identity.get("pane_id"),
            pane_tty=identity.get("pane_tty"),
        )
    lock_fd = _acquire_pane_operation_lock(session)
    if lock_fd is None:
        return _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="tmux pane operation already in progress",
                pane_id=identity.get("pane_id"),
                pane_tty=identity.get("pane_tty"),
                credential_outcome="blocked",
            ),
        )
    try:
        current_identity, identity_error = _pane_identity(session)
        if identity_error or current_identity != identity:
            return _write_gate_payload(
                session,
                _action_payload(
                    session,
                    sudo="failed",
                    detail="tmux pane identity changed before sudo probe",
                    pane_id=current_identity.get("pane_id"),
                    pane_tty=current_identity.get("pane_tty"),
                    credential_outcome="blocked",
                ),
            )
        if _probe_pending_path(session).exists():
            return _reconcile_pending(session, current_identity)
        if not _is_pane_idle(session):
            return _write_gate_payload(
                session,
                _action_payload(
                    session,
                    sudo="failed",
                    detail="tmux pane is not idle; refusing sudo probe",
                    pane_id=current_identity.get("pane_id"),
                    pane_tty=current_identity.get("pane_tty"),
                    credential_outcome="blocked",
                ),
            )
        final_identity, identity_error = _pane_identity(session)
        if identity_error or final_identity != current_identity:
            return _write_gate_payload(
                session,
                _action_payload(
                    session,
                    sudo="failed",
                    detail="tmux pane identity changed before sudo probe dispatch",
                    pane_id=final_identity.get("pane_id"),
                    pane_tty=final_identity.get("pane_tty"),
                    credential_outcome="blocked",
                ),
            )

        operation_id = uuid.uuid4().hex
        automatic_allowed = allow_credentials and not _probe_auth_blocked_path(session).exists()
        script = _write_probe_script(
            session,
            operation_id,
            final_identity,
            allow_credentials=automatic_allowed,
        )
        pending = {
            "operation_id": operation_id,
            "pane_id": final_identity.get("pane_id"),
            "pane_tty": final_identity.get("pane_tty"),
            "created_at": time.time(),
            "allow_credentials": automatic_allowed,
        }
        _json_write(_probe_pending_path(session), pending)
        _write_gate_payload(
            session,
            _action_payload(
                session,
                sudo="failed",
                detail="sudo probe is pending",
                pane_id=final_identity.get("pane_id"),
                pane_tty=final_identity.get("pane_tty"),
                operation_id=operation_id,
                credential_outcome="blocked",
            ),
        )
        dispatch_identity, dispatch_identity_error = _pane_identity(session)
        if dispatch_identity_error or dispatch_identity != final_identity:
            try:
                _probe_pending_path(session).unlink(missing_ok=True)
            except OSError:
                pass
            try:
                script.unlink()
            except OSError:
                pass
            return _write_gate_payload(
                session,
                _action_payload(
                    session,
                    sudo="failed",
                    detail="tmux pane identity changed before sudo probe dispatch",
                    pane_id=dispatch_identity.get("pane_id"),
                    pane_tty=dispatch_identity.get("pane_tty"),
                    operation_id=operation_id,
                    credential_outcome="blocked",
                ),
            )
        channel = f"{WAIT_PREFIX}-{session}-probe-{operation_id}"
        try:
            wait = _start_tmux_wait(channel)
        except OSError:
            _probe_pending_path(session).unlink(missing_ok=True)
            return _write_gate_payload(
                session,
                _action_payload(
                    session,
                    sudo="failed",
                    detail="unable to start sudo probe completion wait",
                    pane_id=final_identity.get("pane_id"),
                    pane_tty=final_identity.get("pane_tty"),
                    operation_id=operation_id,
                    credential_outcome="blocked",
                ),
            )
        dispatched = _targeted_tmux(
            session,
            ["send-keys", "-t", "{target}", f"bash {shlex.quote(str(script))}", "Enter"],
        )
        if dispatched.returncode == 0:
            _wait_proc(wait, 10.0)
        else:
            # A failed acknowledgement may follow partial dispatch; keep the fence.
            try:
                wait.kill()
                wait.wait(timeout=1)
            except Exception:
                pass
        return _reconcile_pending(session, final_identity)
    finally:
        _release_pane_operation_lock(session, lock_fd)


def current_sudo_gate(session: str) -> dict[str, Any] | None:
    return _json_load(_sudo_gate_path(session)) or _json_load(_probe_status_path(session))


def gate_is_fresh_ready(session: str, *, freshness: float = DEFAULT_GATE_FRESHNESS_SECONDS) -> bool:
    if _probe_pending_path(session).exists():
        return False
    gate = current_sudo_gate(session)
    if not gate or gate.get("sudo") != "ready" or gate.get("gate_state") != "ready":
        return False
    checked_at = gate.get("checked_at", gate.get("ts"))
    if not isinstance(checked_at, (int, float)) or time.time() - float(checked_at) > freshness:
        return False
    identity, err = _pane_identity(session)
    if err:
        return False
    return gate.get("pane_id") == identity.get("pane_id") and gate.get("pane_tty") == identity.get("pane_tty")


def ensure_ready_gate(
    session: str,
    *,
    freshness: float = DEFAULT_GATE_FRESHNESS_SECONDS,
    allow_credentials: bool = True,
) -> dict[str, Any]:
    if _is_pane_waiting_sudo(session):
        identity, _ = _pane_identity(session)
        return _action_payload(
            session,
            sudo="password_required",
            detail="pane is waiting at a sudo/password prompt",
            pane_id=identity.get("pane_id"),
            pane_tty=identity.get("pane_tty"),
            credential_outcome="manual",
        )
    if gate_is_fresh_ready(session, freshness=freshness):
        return current_sudo_gate(session) or _action_payload(session, sudo="failed", detail="sudo gate missing")
    return probe_sudo_gate(session, allow_credentials=allow_credentials)


def sudo_blocked_payload(session: str) -> dict[str, Any] | None:
    if _is_pane_waiting_sudo(session):
        identity, _ = _pane_identity(session)
        return _action_payload(
            session,
            sudo="password_required",
            detail="pane is waiting at a sudo/password prompt",
            pane_id=identity.get("pane_id"),
            pane_tty=identity.get("pane_tty"),
            credential_outcome="manual",
        )
    gate = current_sudo_gate(session)
    if gate and gate.get("sudo") == "password_required":
        return gate
    return None
