from __future__ import annotations

import json
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from .sanitize import _sanitize_command, _sanitize_session
from .session import _ensure_managed_session
from .state import (
    _active_path,
    _acquire_pane_operation_lock,
    _attach_command,
    _json_write,
    _live_active,
    _release_pane_operation_lock,
    _run_channel,
    _state_dir,
    _status_path,
    _status_payload,
)
from .sudo_gate import DEFAULT_GATE_FRESHNESS_SECONDS, ensure_ready_gate, sudo_blocked_payload
from .tmux import _start_tmux_wait, _targeted_tmux, _wait_proc


_RUN_COMMAND_LAUNCHER = """import os, subprocess, sys
command = os.environ.pop("TMUX_OPS_COMMAND")
env = os.environ.copy()
for name in tuple(env):
    if name.endswith("_SUDO_KEY"):
        env.pop(name, None)
try:
    result = subprocess.run(["bash", "-c", command], env=env, check=False)
except OSError:
    print("managed command could not be started", file=sys.stderr)
    sys.exit(127)
exit_code = 128 - result.returncode if result.returncode < 0 else result.returncode
sys.exit(exit_code)
"""


def _command_from_remainder(parts: list[str]) -> tuple[str | None, str | None]:
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        return None, "missing command after --"
    command = parts[0] if len(parts) == 1 else shlex.join(parts)
    return _sanitize_command(command)


def _credential_provenance(gate: dict[str, Any]) -> dict[str, Any]:
    outcome = gate.get("credential_outcome")
    if not isinstance(outcome, str):
        outcome = "cached" if gate.get("sudo") == "ready" else "not_checked"
    variable = gate.get("credential_variable")
    if not isinstance(variable, str):
        variable = None
    candidates = gate.get("credential_candidates")
    if not isinstance(candidates, list):
        candidates = []
    safe_candidates = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = item.get("variable")
        status = item.get("status")
        valid_name = isinstance(name, str) and (
            name == "GENERIC_SUDO_KEY"
            or (
                name.endswith("_SUDO_KEY")
                and len(name) > len("_SUDO_KEY")
                and all(char.isupper() or char.isdigit() or char in "._-" for char in name[:-9])
            )
        )
        if valid_name and isinstance(status, str) and status in {
            "unavailable",
            "empty",
            "rejected",
            "used",
            "ambiguous",
        }:
            safe_candidates.append({"variable": name, "status": status})
    if outcome not in {"cached", "automatic", "manual", "no_env", "blocked", "not_checked"}:
        outcome = "not_checked"
    if not isinstance(variable, str) or not any(
        item["variable"] == variable and item["status"] == "used" for item in safe_candidates
    ):
        variable = None
    return {
        "credential_outcome": outcome,
        "credential_variable": variable,
        "credential_candidates": safe_candidates,
        "credential_candidates_checked": gate.get("credential_candidates_checked") is True,
    }


def _write_run_script(
    session: str,
    run_id: str,
    command: str,
    started_at: float,
    credential_provenance: dict[str, Any],
) -> tuple[Path, Path]:
    state = _state_dir(session)
    runs = state / "runs"
    logs = state / "logs"
    scripts = state / "scripts"
    for path in (runs, logs, scripts):
        path.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{run_id}.log"
    script_path = scripts / f"{run_id}.sh"
    status_path = _status_path(session, run_id)
    active_path = _active_path(session)
    content = f"""#!/usr/bin/env bash
set +e
__tmux_ops_code=0
{{
  TMUX_OPS_COMMAND={shlex.quote(command)} python3 -c {shlex.quote(_RUN_COMMAND_LAUNCHER)}
  __tmux_ops_code=$?
}} > >(tee -a {shlex.quote(str(log_path))}) 2> >(tee -a {shlex.quote(str(log_path))} >&2)
TMUX_OPS_STATUS_PATH={shlex.quote(str(status_path))} \\
TMUX_OPS_SESSION={shlex.quote(session)} \\
TMUX_OPS_RUN_ID={shlex.quote(run_id)} \\
TMUX_OPS_EXIT_CODE="$__tmux_ops_code" \\
TMUX_OPS_STARTED_AT={started_at!r} \\
TMUX_OPS_LOG_PATH={shlex.quote(str(log_path))} \\
TMUX_OPS_SCRIPT_PATH={shlex.quote(str(script_path))} \\
TMUX_OPS_ATTACH={shlex.quote(_attach_command(session))} \\
TMUX_OPS_SUDO_PROVENANCE={shlex.quote(json.dumps(credential_provenance, sort_keys=True))} \\
python3 - <<'PY'
import json, os, time
finished = time.time()
started = float(os.environ["TMUX_OPS_STARTED_AT"])
payload = {{
    "run_id": os.environ["TMUX_OPS_RUN_ID"],
    "session": os.environ["TMUX_OPS_SESSION"],
    "status": "completed",
    "exit_code": int(os.environ["TMUX_OPS_EXIT_CODE"]),
    "started_at": started,
    "finished_at": finished,
    "elapsed_s": round(finished - started, 3),
    "log_path": os.environ["TMUX_OPS_LOG_PATH"],
    "script_path": os.environ["TMUX_OPS_SCRIPT_PATH"],
    "attach_command": os.environ["TMUX_OPS_ATTACH"],
    **json.loads(os.environ["TMUX_OPS_SUDO_PROVENANCE"]),
}}
with open(os.environ["TMUX_OPS_STATUS_PATH"], "w", encoding="utf-8") as f:
    json.dump(payload, f, sort_keys=True)
    f.write("\\n")
PY
rm -f {shlex.quote(str(active_path))}
printf '%s\n' {shlex.quote(run_id)} > {shlex.quote(str(_state_dir(session) / "last_run_id"))}
exit "$__tmux_ops_code"
"""
    script_path.write_text(content, encoding="utf-8")
    script_path.chmod(0o700)
    return script_path, log_path


def cmd_run(target: str, command: str, timeout: float, tail_lines: int) -> dict[str, Any]:
    san = _sanitize_session(target)
    if san is None:
        return {"error": "invalid session name", "session": target, "source": "run"}
    ok, err = _ensure_managed_session(san)
    if not ok:
        return {"error": "session is not ready for run", "detail": err or "", "session": san, "source": "run"}
    active = _live_active(san)
    if active:
        return {
            "error": "run already active",
            "detail": "use status or cancel before starting another run",
            **_status_payload(san, str(active.get("run_id", "")), tail_lines),
            "source": "run",
        }

    gate = ensure_ready_gate(san, freshness=DEFAULT_GATE_FRESHNESS_SECONDS)
    if gate.get("sudo") != "ready" or gate.get("gate_state") != "ready":
        return {
            "error": "sudo gate not ready",
            **gate,
            "source": "run",
        }

    lock_fd = _acquire_pane_operation_lock(san)
    if lock_fd is None:
        return {
            "error": "tmux pane operation already active",
            "detail": "use status or retry after the current pane operation finishes",
            "session": san,
            "source": "run",
        }
    try:
        active = _live_active(san)
        if active:
            return {
                "error": "run already active",
                "detail": "use status or cancel before starting another run",
                **_status_payload(san, str(active.get("run_id", "")), tail_lines),
                "source": "run",
            }
        run_id = f"{int(time.time())}-{uuid.uuid4().hex[:10]}"
        started_at = time.time()
        provenance = _credential_provenance(gate)
        script_path, log_path = _write_run_script(san, run_id, command, started_at, provenance)
        status = {
            "run_id": run_id,
            "session": san,
            "status": "running",
            "exit_code": None,
            "started_at": started_at,
            "elapsed_s": 0.0,
            "log_path": str(log_path),
            "script_path": str(script_path),
            "attach_command": _attach_command(san),
            **provenance,
        }
        _json_write(_status_path(san, run_id), status)
        _json_write(_active_path(san), status)

        channel = _run_channel(san, run_id)
        wait = _start_tmux_wait(channel)
        r = _targeted_tmux(
            san,
            [
                "send-keys",
                "-t",
                "{target}",
                f"TMUX_OPS_RUN_ID={shlex.quote(run_id)} bash {shlex.quote(str(script_path))}",
                "Enter",
            ],
        )
    finally:
        _release_pane_operation_lock(san, lock_fd)
    if r.returncode != 0:
        status.update({"status": "failed", "detail": r.stderr})
        _json_write(_status_path(san, run_id), status)
        try:
            _active_path(san).unlink()
        except OSError:
            pass
        return {
            "error": "tmux send-keys failed",
            "detail": r.stderr,
            **_status_payload(san, run_id, tail_lines),
            "source": "run",
        }

    completed = _wait_proc(wait, timeout)
    if completed:
        out = _status_payload(san, run_id, tail_lines)
        if out.get("status") == "running":
            out["elapsed_s"] = round(time.time() - started_at, 3)
        return out

    out = _status_payload(san, run_id, tail_lines)
    if out.get("status") == "running":
        out["elapsed_s"] = round(time.time() - started_at, 3)
    return out


def cmd_status(target: str, run_id: str | None, wait: bool, timeout: float, tail_lines: int) -> dict[str, Any]:
    san = _sanitize_session(target)
    if san is None:
        return {"error": "invalid session name", "session": target, "source": "status"}
    active = _live_active(san)
    rid = run_id or (str(active.get("run_id")) if active else None)
    if rid is None:
        status = {"session": san, "status": "idle", "attach_command": _attach_command(san), "tail": ""}
        blocked = sudo_blocked_payload(san)
        if blocked:
            status.update(blocked)
            status["status"] = "idle"
        return status
    status = _status_payload(san, rid, tail_lines)
    if wait and status.get("status") == "running":
        proc = _start_tmux_wait(_run_channel(san, rid))
        _wait_proc(proc, timeout)
        status = _status_payload(san, rid, tail_lines)
        if status.get("status") == "running":
            status["elapsed_s"] = round(time.time() - float(status.get("started_at", time.time())), 3)
    blocked = sudo_blocked_payload(san)
    if blocked:
        status.update(
            {
                "action_required": True,
                "sudo": blocked.get("sudo"),
                "gate_state": blocked.get("gate_state"),
                "user_command": blocked.get("user_command"),
                "attach_command": blocked.get("attach_command", status.get("attach_command")),
                "next_probe_command": blocked.get("next_probe_command"),
                "pane_id": blocked.get("pane_id"),
                "pane_tty": blocked.get("pane_tty"),
                "checked_at": blocked.get("checked_at"),
                "probe_command": blocked.get("probe_command"),
                "detail": blocked.get("detail", status.get("detail", "")),
            }
        )
    return status


def cmd_cancel(target: str, run_id: str) -> dict[str, Any]:
    san = _sanitize_session(target)
    if san is None:
        return {"error": "invalid session name", "session": target, "source": "cancel"}
    active = _live_active(san)
    if not active:
        return {"error": "no active run", "session": san, "source": "cancel"}
    if active.get("run_id") != run_id:
        return {"error": "run_id does not match active run", "session": san, "run_id": run_id, "source": "cancel"}
    r = _targeted_tmux(san, ["send-keys", "-t", "{target}", "C-c"])
    if r.returncode != 0:
        return {"error": "tmux cancel failed", "detail": r.stderr, "session": san, "run_id": run_id, "source": "cancel"}
    status = _status_payload(san, run_id)
    status["cancel_sent"] = True
    status["status"] = "running"
    return status
