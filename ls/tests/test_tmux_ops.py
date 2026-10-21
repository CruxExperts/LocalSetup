import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "ls" / "tools" / "tmux_ops.py"


@pytest.fixture
def tmux_env(tmp_path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    socket = f"localsetup-test-{uuid.uuid4().hex}"
    env = {name: value for name, value in os.environ.items() if not name.endswith("_SUDO_KEY")}
    env["HOME"] = str(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo_mode_file = tmp_path / "sudo-mode"
    sudo_mode_file.write_text("ready\n", encoding="utf-8")
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/usr/bin/env bash
mode="${FAKE_SUDO_MODE:-ready}"
if [ -n "${FAKE_SUDO_MODE_FILE:-}" ] && [ -f "$FAKE_SUDO_MODE_FILE" ]; then
  mode="$(cat "$FAKE_SUDO_MODE_FILE")"
fi
case "$mode:$1" in
  ready:-n) [ "${2:-}" = "-v" ] && exit 0 ;;
  password:-n) [ "${2:-}" = "-v" ] && echo "sudo: a password is required" >&2 && exit 1 ;;
  failed:-n) [ "${2:-}" = "-v" ] && echo "testuser is not in the sudoers file" >&2 && exit 1 ;;
  ready:-Nnv|ready:-vn) exit 0 ;;
  password:-Nnv|password:-vn) echo "sudo: a password is required" >&2; exit 1 ;;
  failed:-Nnv|failed:-vn) echo "testuser is not in the sudoers file" >&2; exit 1 ;;
  *) echo "unexpected sudo invocation: $*" >&2; exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_SUDO_MODE_FILE"] = str(sudo_mode_file)
    env["TMUX_OPS_TMUX"] = f"tmux -L {socket}"
    env["TMUX_OPS_STATE_ROOT"] = str(tmp_path / "state")
    try:
        yield env, socket
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, text=True)


def run_ops(env, *args, check=True):
    proc = subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"tmux_ops failed: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    payload = json.loads(proc.stdout)
    return proc, payload


def install_tmux_wrapper(tmp_path, env, socket, *, fail_run_send=False, fail_probe_send=False):
    log_path = tmp_path / f"tmux-{uuid.uuid4().hex}.log"
    wrapper = tmp_path / f"tmux-wrapper-{uuid.uuid4().hex}"
    fail_flag = "1" if fail_run_send else "0"
    fail_probe_flag = "1" if fail_probe_send else "0"
    wrapper.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
log={shlex.quote(str(log_path))}
fail_run_send={fail_flag}
fail_probe_send={fail_probe_flag}
printf '%q ' "$@" >> "$log"
printf '\\n' >> "$log"
if [ "${{1:-}}" = "send-keys" ]; then
  for arg in "$@"; do
    if [ "$fail_run_send" = "1" ]; then
      case "$arg" in TMUX_OPS_RUN_ID=*) printf 'not in a mode\\n' >&2; exit 1 ;; esac
    fi
    if [ "$fail_probe_send" = "1" ]; then
      case "$arg" in *probe-*.sh*) printf 'injected probe dispatch uncertainty\\n' >&2; exit 1 ;; esac
    fi
  done
fi
exec tmux -L {shlex.quote(socket)} "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    env["TMUX_OPS_TMUX"] = str(wrapper)
    return log_path


def tmux_calls(log_path):
    if not log_path.exists():
        return []
    return [shlex.split(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def pane_identity(socket: str, session: str = "ops") -> tuple[str, str]:
    pane_id = subprocess.check_output(
        ["tmux", "-L", socket, "list-panes", "-t", session, "-F", "#{pane_id}"],
        text=True,
    ).strip()
    pane_tty = subprocess.check_output(
        ["tmux", "-L", socket, "display-message", "-t", pane_id, "-p", "-F", "#{pane_tty}"],
        text=True,
    ).strip()
    return pane_id, pane_tty


def write_gate(env: dict[str, str], socket: str, *, state: str = "ready", checked_at: float | None = None) -> dict:
    pane_id, pane_tty = pane_identity(socket)
    payload = {
        "session": "ops",
        "sudo": state,
        "gate_state": state,
        "action_required": state != "ready",
        "user_command": "sudo -v" if state == "password_required" else None,
        "attach_command": "tmux new-session -A -s ops",
        "next_probe_command": "tmux_ops probe -t ops",
        "pane_id": pane_id,
        "pane_tty": pane_tty,
        "checked_at": time.time() if checked_at is None else checked_at,
        "ts": time.time() if checked_at is None else checked_at,
        "probe_command": "sudo -Nnv",
        "detail": "",
    }
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    state_dir.mkdir(parents=True, exist_ok=True)
    for name in ("sudo_gate.json", "probe.status.json"):
        (state_dir / name).write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def set_sudo_mode(env: dict[str, str], mode: str) -> None:
    mode_file = env.get("FAKE_SUDO_MODE_FILE")
    if mode_file:
        Path(mode_file).write_text(mode + "\n", encoding="utf-8")
    env["FAKE_SUDO_MODE"] = mode


def install_credential_sudo(tmp_path: Path, env: dict[str, str], *, hostname: str = "node-west.example") -> Path:
    fake_bin = tmp_path / f"credential-bin-{uuid.uuid4().hex}"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SUDO_ARG_LOG"
case "${1:-}" in
  -Nnv|-vn) echo "sudo: a password is required" >&2; exit 1 ;;
  -S)
    if [ "${FAKE_SUDO_AUTH_MODE:-}" = "timeout" ]; then sleep 8; exit 0; fi
    IFS= read -r supplied || supplied=""
    if [ "$supplied" = "$FAKE_SUDO_EXPECTED" ]; then exit 0; fi
    echo "Sorry, try again." >&2
    exit 1
    ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    hostname_cmd = fake_bin / "hostname"
    hostname_cmd.write_text(f"#!/bin/sh\necho {shlex.quote(hostname)}\n", encoding="utf-8")
    hostname_cmd.chmod(0o700)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_SUDO_ARG_LOG"] = str(tmp_path / f"sudo-args-{uuid.uuid4().hex}.log")
    env["FAKE_SUDO_EXPECTED"] = "synthetic-good-secret"
    return Path(env["FAKE_SUDO_ARG_LOG"])


def delay_probe_completion_signal(tmp_path: Path, env: dict[str, str]) -> None:
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    fake_bin = tmp_path / "delayed-tmux-bin"
    fake_bin.mkdir()
    tmux = fake_bin / "tmux"
    tmux.write_text(
        f"#!/bin/sh\n{shlex.quote(real_tmux)} \"$@\"\n"
        'status=$?\nif [ "${1:-}" = "wait-for" ] && [ "${2:-}" = "-S" ]; then\n'
        '  case "${3:-}" in *-probe-*) sleep 0.3 ;; esac\nfi\n'
        'exit "$status"\n',
        encoding="utf-8",
    )
    tmux.chmod(0o700)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"


def send_targets(log_path, *, contains=None):
    targets = []
    for call in tmux_calls(log_path):
        if not call or call[0] != "send-keys":
            continue
        if contains is not None and not any(contains in part for part in call):
            continue
        targets.append(call[call.index("-t") + 1])
    return targets


def test_pick_creates_real_managed_session(tmux_env):
    env, socket = tmux_env

    _, payload = run_ops(env, "pick")

    assert payload["session"] == "ops"
    assert payload["reason"] == "created"
    assert payload["attach_command"] == "tmux new-session -A -s ops"
    has_session = subprocess.run(
        ["tmux", "-L", socket, "has-session", "-t", "ops"],
        capture_output=True,
        text=True,
    )
    assert has_session.returncode == 0


@pytest.mark.parametrize(
    ("command", "expected_exit_code"),
    [
        ("echo stdout; echo stderr >&2; exit 7", 7),
        ("echo stdout; echo stderr >&2; kill -TERM $$", 143),
        ("echo stdout; echo stderr >&2; kill -KILL $$", 137),
    ],
    ids=["exit-code", "sigterm", "sigkill"],
)
def test_run_captures_output_and_preserves_nonzero_exit(tmux_env, command, expected_exit_code):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket)
    log_path.write_text("", encoding="utf-8")

    _, payload = run_ops(
        env,
        "run",
        "-t",
        "ops",
        "--timeout",
        "5",
        "--tail",
        "20",
        "--",
        "bash",
        "-lc",
        command,
    )

    assert payload["status"] == "completed"
    assert payload["exit_code"] == expected_exit_code
    assert payload["credential_outcome"] == "cached"
    assert payload["credential_variable"] is None
    assert payload["credential_candidates_checked"] is False
    assert "stdout" in payload["tail"]
    assert "stderr" in payload["tail"]
    assert Path(payload["log_path"]).exists()
    assert send_targets(log_path, contains="TMUX_OPS_RUN_ID=")
    assert all(target.startswith("%") for target in send_targets(log_path, contains="TMUX_OPS_RUN_ID="))

    _, status = run_ops(env, "status", "-t", "ops", "--run-id", payload["run_id"], "--tail", "20")
    assert status["status"] == "completed"
    assert status["exit_code"] == expected_exit_code


def test_run_preserves_pane_tty_and_stdin(tmux_env):
    env, socket = tmux_env
    run_ops(env, "pick")
    write_gate(env, socket)

    _, first = run_ops(
        env,
        "run",
        "-t",
        "ops",
        "--timeout",
        "0.2",
        "--tail",
        "20",
        "--",
        "bash",
        "-c",
        "printf 'waiting-for-input\\n'; if [ -t 0 ]; then tty_state=yes; else tty_state=no; fi; IFS= read -r value; printf 'tty=%s input=%s\\n' \"$tty_state\" \"$value\"",
    )

    assert first["status"] == "running"
    tail = first["tail"]
    for _ in range(20):
        if "waiting-for-input" in tail or first["status"] != "running":
            break
        _, first = run_ops(env, "status", "-t", "ops", "--run-id", first["run_id"], "--tail", "20")
        tail = first["tail"]
        time.sleep(0.05)
    assert first["status"] == "running"
    assert "waiting-for-input" in tail

    subprocess.run(
        ["tmux", "-L", socket, "send-keys", "-t", "ops", "synthetic-input", "Enter"],
        check=True,
    )
    _, completed = run_ops(
        env,
        "status",
        "-t",
        "ops",
        "--run-id",
        first["run_id"],
        "--wait",
        "--timeout",
        "5",
        "--tail",
        "20",
    )

    assert completed["status"] == "completed"
    assert "tty=yes input=synthetic-input" in completed["tail"]


def test_run_timeout_stays_active_and_blocks_second_run(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket)
    log_path.write_text("", encoding="utf-8")

    _, first = run_ops(
        env,
        "run",
        "-t",
        "ops",
        "--timeout",
        "0.2",
        "--tail",
        "20",
        "--",
        "bash",
        "-lc",
        "echo start; sleep 2; echo done",
    )

    assert first["status"] == "running"
    assert first["exit_code"] is None
    assert first["credential_outcome"] == "cached"
    assert first["credential_variable"] is None
    tail = first["tail"]
    for _ in range(20):
        if "start" in tail:
            break
        _, status_probe = run_ops(env, "status", "-t", "ops", "--run-id", first["run_id"], "--tail", "20")
        tail = status_probe["tail"]
        if status_probe["status"] != "running":
            break
        time.sleep(0.05)
    assert "start" in tail
    assert all(target.startswith("%") for target in send_targets(log_path, contains="TMUX_OPS_RUN_ID="))

    proc, blocked = run_ops(env, "run", "-t", "ops", "--", "echo", "blocked", check=False)
    assert proc.returncode == 1
    assert blocked["error"] == "run already active"
    assert blocked["run_id"] == first["run_id"]

    _, completed = run_ops(
        env,
        "status",
        "-t",
        "ops",
        "--run-id",
        first["run_id"],
        "--wait",
        "--timeout",
        "5",
        "--tail",
        "20",
    )
    assert completed["status"] == "completed"
    assert completed["exit_code"] == 0
    assert "done" in completed["tail"]


def test_probe_classifies_sudo_states(tmux_env, tmp_path):
    env, _socket = tmux_env
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/usr/bin/env bash
case "${FAKE_SUDO_MODE:-ready}:$1" in
  ready:-Nnv|ready:-vn) exit 0 ;;
  password:-Nnv|password:-vn) echo "sudo: a password is required" >&2; exit 1 ;;
  failed:-Nnv|failed:-vn) echo "testuser is not in the sudoers file" >&2; exit 1 ;;
  password:-v) echo "[sudo] password for testuser:" >&2; sleep 30 ;;
  *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    for mode, expected in [
        ("ready", "ready"),
        ("password", "password_required"),
        ("failed", "failed"),
    ]:
        env["FAKE_SUDO_MODE"] = mode
        socket = f"localsetup-test-{uuid.uuid4().hex}"
        env["TMUX_OPS_TMUX"] = f"tmux -L {socket}"
        env["TMUX_OPS_STATE_ROOT"] = str(tmp_path / f"state-{mode}")
        try:
            pick = run_ops(env, "pick")[1]
            log_path = install_tmux_wrapper(tmp_path, env, socket)
            probe = run_ops(env, "probe", "-t", pick["session"])[1]
            assert probe["sudo"] == expected
            assert probe["gate_state"] == expected
            assert probe["attach_command"] == "tmux new-session -A -s ops"
            assert send_targets(log_path, contains="probe-")
            assert all(target.startswith("%") for target in send_targets(log_path, contains="probe-"))
            assert not send_targets(log_path, contains="sudo -v")
            gate = json.loads((Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "sudo_gate.json").read_text())
            alias = json.loads((Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "probe.status.json").read_text())
            assert gate == alias
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, text=True)


def test_probe_falls_back_when_sudo_n_is_unsupported(tmux_env, tmp_path):
    env, _socket = tmux_env
    fake_bin = tmp_path / "fallback-bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/usr/bin/env bash
case "$1" in
  -Nnv) echo "sudo: invalid option -- N" >&2; exit 1 ;;
  -vn) exit 0 ;;
  *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    run_ops(env, "pick")
    _, probe = run_ops(env, "probe", "-t", "ops")

    assert probe["sudo"] == "ready"
    assert probe["probe_command"] == "sudo -vn"


def test_probe_uses_exact_hostname_key_then_generic_and_run_reports_provenance(tmux_env, tmp_path):
    env, socket = tmux_env
    calls = install_credential_sudo(tmp_path, env, hostname="node-west.example")
    env["NODE-WEST.EXAMPLE_SUDO_KEY"] = "synthetic-wrong-secret"
    env["GENERIC_SUDO_KEY"] = "synthetic-good-secret"
    tmux_log = install_tmux_wrapper(tmp_path, env, socket)
    run_ops(env, "pick")
    tmux_log.write_text("", encoding="utf-8")

    _, probe = run_ops(env, "probe", "-t", "ops")

    assert probe["sudo"] == "ready"
    assert probe["credential_outcome"] == "automatic"
    assert probe["credential_variable"] == "GENERIC_SUDO_KEY"
    assert probe["credential_candidates"] == [
        {"variable": "NODE-WEST.EXAMPLE_SUDO_KEY", "status": "rejected"},
        {"variable": "GENERIC_SUDO_KEY", "status": "used"},
    ]
    sudo_args = calls.read_text(encoding="utf-8").splitlines()
    assert sudo_args == ["-Nnv", "-S -v -p ", "-S -v -p "]

    _, run = run_ops(
        env,
        "run",
        "-t",
        "ops",
        "--",
        "python3",
        "-c",
        "import os; print('sudo_key_present=' + str(any(k.endswith('_SUDO_KEY') for k in os.environ)))",
    )

    assert run["status"] == "completed"
    assert run["credential_outcome"] == "automatic"
    assert run["credential_variable"] == "GENERIC_SUDO_KEY"
    assert run["credential_candidates"] == probe["credential_candidates"]
    assert "sudo_key_present=False" in run["tail"]
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in state_dir.rglob("*")
        if path.is_file()
    )
    public_output = json.dumps(probe) + json.dumps(run) + tmux_log.read_text(encoding="utf-8")
    for secret in ("synthetic-wrong-secret", "synthetic-good-secret"):
        assert secret not in persisted
        assert secret not in public_output
        assert secret not in calls.read_text(encoding="utf-8")


def test_probe_preserves_underscore_in_hostname_credential_name(tmux_env, tmp_path):
    env, _socket = tmux_env
    calls = install_credential_sudo(tmp_path, env, hostname="node_west")
    env["NODE_WEST_SUDO_KEY"] = "synthetic-good-secret"
    run_ops(env, "pick")

    _, probe = run_ops(env, "probe", "-t", "ops")

    assert probe["sudo"] == "ready"
    assert probe["credential_variable"] == "NODE_WEST_SUDO_KEY"
    assert probe["credential_candidates"] == [
        {"variable": "NODE_WEST_SUDO_KEY", "status": "used"},
    ]
    assert calls.read_text(encoding="utf-8").splitlines() == ["-Nnv", "-S -v -p "]


def test_probe_reports_no_env_and_requires_manual_sudo(tmux_env, tmp_path):
    env, _socket = tmux_env
    install_credential_sudo(tmp_path, env)
    run_ops(env, "pick")

    _, probe = run_ops(env, "probe", "-t", "ops")

    assert probe["sudo"] == "password_required"
    assert probe["credential_outcome"] == "no_env"
    assert probe["credential_variable"] is None
    assert probe["credential_candidates_checked"] is True
    assert probe["credential_candidates"] == [
        {"variable": "NODE-WEST.EXAMPLE_SUDO_KEY", "status": "unavailable"},
        {"variable": "GENERIC_SUDO_KEY", "status": "unavailable"},
    ]
    assert probe["user_command"] == "sudo -v"


def test_probe_rejects_newline_key_without_sending_it(tmux_env, tmp_path):
    env, _socket = tmux_env
    calls = install_credential_sudo(tmp_path, env)
    env["NODE-WEST.EXAMPLE_SUDO_KEY"] = "synthetic-line1\nsynthetic-line2"
    env["GENERIC_SUDO_KEY"] = "synthetic-good-secret"
    run_ops(env, "pick")

    _, probe = run_ops(env, "probe", "-t", "ops")

    assert probe["sudo"] == "ready"
    assert probe["credential_variable"] == "GENERIC_SUDO_KEY"
    assert probe["credential_candidates"] == [
        {"variable": "NODE-WEST.EXAMPLE_SUDO_KEY", "status": "rejected"},
        {"variable": "GENERIC_SUDO_KEY", "status": "used"},
    ]
    assert calls.read_text(encoding="utf-8").splitlines() == ["-Nnv", "-S -v -p "]
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in state_dir.rglob("*")
        if path.is_file()
    )
    assert "synthetic-line1" not in persisted
    assert "synthetic-line2" not in persisted


def test_probe_timeout_blocks_generic_and_future_secret_retry(tmux_env, tmp_path):
    env, _socket = tmux_env
    calls = install_credential_sudo(tmp_path, env)
    env["NODE-WEST.EXAMPLE_SUDO_KEY"] = "synthetic-timeout-secret"
    env["GENERIC_SUDO_KEY"] = "synthetic-good-secret"
    env["FAKE_SUDO_AUTH_MODE"] = "timeout"
    run_ops(env, "pick")

    _, first = run_ops(env, "probe", "-t", "ops")

    assert first["sudo"] == "failed"
    assert first["credential_outcome"] == "blocked"
    assert first["credential_candidates"] == [
        {"variable": "NODE-WEST.EXAMPLE_SUDO_KEY", "status": "ambiguous"}
    ]
    assert "timed out" in first["detail"]
    assert Path(env["TMUX_OPS_STATE_ROOT"], "ops", "probe.auth-blocked.json").exists()

    _, followup = run_ops(env, "probe", "-t", "ops")

    assert followup["sudo"] == "password_required"
    assert followup["credential_outcome"] == "manual"
    assert calls.read_text(encoding="utf-8").splitlines() == ["-Nnv", "-S -v -p ", "-Nnv"]


def test_probe_pending_operation_reconciles_only_its_matching_result(tmux_env):
    env, socket = tmux_env
    run_ops(env, "pick")
    pane_id, pane_tty = pane_identity(socket)
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    operation_id = "a" * 32
    pending = {
        "operation_id": operation_id,
        "pane_id": pane_id,
        "pane_tty": pane_tty,
        "allow_credentials": False,
        "created_at": time.time(),
    }
    (state_dir / "probe.pending.json").write_text(json.dumps(pending), encoding="utf-8")
    result = {
        "operation_id": operation_id,
        "session": "ops",
        "pane_id": pane_id,
        "pane_tty": pane_tty,
        "sudo": "ready",
        "detail": "",
        "probe_command": "sudo -Nnv",
        "checked_at": time.time(),
        "credential_outcome": "cached",
        "credential_variable": None,
        "credential_candidates": [],
        "credential_candidates_checked": False,
    }
    (state_dir / f"probe-{operation_id}.status.json").write_text(json.dumps(result), encoding="utf-8")

    _, probe = run_ops(env, "probe", "-t", "ops")

    assert probe["sudo"] == "ready"
    assert probe["operation_id"] == operation_id
    assert probe["credential_outcome"] == "cached"
    assert not (state_dir / "probe.pending.json").exists()


def test_probe_rejects_stale_ready_and_mismatched_pending_result(tmux_env):
    env, socket = tmux_env
    run_ops(env, "pick")
    write_gate(env, socket, state="ready")
    pane_id, pane_tty = pane_identity(socket)
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    operation_id = "b" * 32
    pending = {
        "operation_id": operation_id,
        "pane_id": pane_id,
        "pane_tty": pane_tty,
        "allow_credentials": False,
        "created_at": time.time(),
    }
    (state_dir / "probe.pending.json").write_text(json.dumps(pending), encoding="utf-8")
    result = {
        "operation_id": operation_id,
        "session": "ops",
        "pane_id": "%wrong-pane",
        "pane_tty": pane_tty,
        "sudo": "ready",
        "detail": "",
        "probe_command": "sudo -Nnv",
        "checked_at": time.time(),
        "credential_outcome": "cached",
        "credential_variable": None,
        "credential_candidates": [],
        "credential_candidates_checked": False,
    }
    (state_dir / f"probe-{operation_id}.status.json").write_text(json.dumps(result), encoding="utf-8")

    _, probe = run_ops(env, "probe", "-t", "ops")

    assert probe["sudo"] == "failed"
    assert "did not match" in probe["detail"]
    assert (state_dir / "probe.pending.json").exists()
    proc, blocked = run_ops(env, "run", "-t", "ops", "--", "echo", "must-not-run", check=False)
    assert proc.returncode == 1
    assert blocked["error"] == "sudo gate not ready"
    assert blocked["sudo"] == "failed"


def test_probe_dispatch_uncertainty_keeps_fence_without_replaying(tmux_env, tmp_path):
    env, socket = tmux_env
    calls = install_credential_sudo(tmp_path, env)
    env["NODE-WEST.EXAMPLE_SUDO_KEY"] = "synthetic-do-not-replay"
    env["GENERIC_SUDO_KEY"] = "synthetic-do-not-fallback"
    tmux_log = install_tmux_wrapper(tmp_path, env, socket, fail_probe_send=True)
    run_ops(env, "pick")
    write_gate(env, socket, state="ready")
    tmux_log.write_text("", encoding="utf-8")

    _, first = run_ops(env, "probe", "-t", "ops")
    _, followup = run_ops(env, "probe", "-t", "ops")

    assert first["sudo"] == followup["sudo"] == "failed"
    assert "same operation" in first["detail"]
    assert followup["operation_id"] == first["operation_id"]
    assert (Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "probe.pending.json").exists()
    assert len(send_targets(tmux_log, contains="probe-")) == 1
    assert not calls.exists()
    proc, blocked = run_ops(env, "run", "-t", "ops", "--", "echo", "must-not-run", check=False)
    assert proc.returncode == 1
    assert blocked["error"] == "sudo gate not ready"


def test_run_refuses_unready_gates_without_creating_run_state(tmux_env):
    env, socket = tmux_env
    run_ops(env, "pick")
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"

    cases = [
        (None, "password"),
        ({"state": "ready", "checked_at": time.time() - 120}, "password"),
        ({"state": "failed"}, "failed"),
        ({"state": "password_required"}, "password"),
    ]
    for case, sudo_mode in cases:
        set_sudo_mode(env, sudo_mode)
        for child in ("active.json", "runs", "logs", "scripts", "sudo_gate.json", "probe.status.json"):
            path = state_dir / child
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        if case:
            write_gate(env, socket, **case)
        proc, payload = run_ops(env, "run", "-t", "ops", "--", "echo", "blocked", check=False)
        assert proc.returncode == 1
        assert payload["action_required"] is True
        assert "run_id" not in payload
        assert not (state_dir / "active.json").exists()
        assert not (state_dir / "runs").exists()
        assert not (state_dir / "logs").exists()
        assert not (state_dir / "scripts").exists()


def test_cancel_uses_resolved_pane_target(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket)
    _, run_payload = run_ops(
        env,
        "run",
        "-t",
        "ops",
        "--timeout",
        "0.2",
        "--tail",
        "5",
        "--",
        "bash",
        "-lc",
        "sleep 5",
    )
    assert run_payload["status"] == "running"
    log_path.write_text("", encoding="utf-8")

    _, cancel = run_ops(env, "cancel", "-t", "ops", "--run-id", run_payload["run_id"])

    assert cancel["cancel_sent"] is True
    assert send_targets(log_path, contains="C-c")
    assert all(target.startswith("%") for target in send_targets(log_path, contains="C-c"))


def test_legacy_send_uses_resolved_pane_target(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    log_path.write_text("", encoding="utf-8")

    _, payload = run_ops(env, "send", "-t", "ops", "--wait", "--wait-timeout", "3", "echo legacy-send-ok")

    assert payload["sent"] is True
    assert send_targets(log_path, contains="echo legacy-send-ok")
    assert all(target.startswith("%") for target in send_targets(log_path, contains="echo legacy-send-ok"))


def test_run_send_keys_failure_reports_resolved_pane_target(tmux_env):
    env, socket = tmux_env
    tmp_path = Path(env["TMUX_OPS_STATE_ROOT"]).parent
    install_tmux_wrapper(tmp_path, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket)
    install_tmux_wrapper(tmp_path, env, socket, fail_run_send=True)

    proc, payload = run_ops(env, "run", "-t", "ops", "--", "echo", "blocked", check=False)

    assert proc.returncode == 1
    assert payload["error"] == "tmux send-keys failed"
    assert "not in a mode" in payload["detail"]
    assert "resolved_target=%" in payload["detail"]


def test_keepalive_request_status_and_caps(tmux_env):
    env, socket = tmux_env
    run_ops(env, "pick")
    write_gate(env, socket)

    _, payload = run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "999999",
        "--max-refreshes",
        "999",
        "--reason",
        "bounded sudo maintenance",
    )

    assert payload["ok"] is True
    assert payload["session"] == "ops"
    assert payload["state"] == "active"
    marker = json.loads((Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "keepalive.json").read_text())
    assert marker["ttl_seconds"] == 7200
    assert marker["max_refreshes"] == 24
    assert marker["refresh_count"] == 0
    assert marker["owner"] == "agent-1"
    assert marker["reason"] == "bounded sudo maintenance"
    assert marker["pane_id"].startswith("%")

    _, status = run_ops(env, "keepalive", "status", "-t", "ops")

    assert status["ok"] is True
    assert len(status["sessions"]) == 1
    assert status["sessions"][0]["state"] == "active"
    assert status["sessions"][0]["owner"] == "agent-1"


def test_keepalive_refresh_uses_hard_coded_sudo_in_resolved_pane(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket)
    run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "test refresh",
    )
    log_path.write_text("", encoding="utf-8")

    _, payload = run_ops(env, "keepalive", "refresh", "-t", "ops")

    assert payload["ok"] is True
    assert payload["refresh_count"] == 1
    assert payload["last_refresh_ok"] is True
    assert send_targets(log_path, contains="keepalive-refresh-")
    assert all(target.startswith("%") for target in send_targets(log_path, contains="keepalive-refresh-"))
    scripts = sorted((Path(env["TMUX_OPS_STATE_ROOT"]) / "ops").glob("keepalive-refresh-*.sh"))
    assert len(scripts) == 1
    script = scripts[0].read_text(encoding="utf-8")
    assert "sudo -n -v" in script
    assert "sudo -v" not in script.replace("sudo -n -v", "")


def test_keepalive_refresh_failure_disables_marker(tmux_env):
    env, socket = tmux_env
    run_ops(env, "pick")
    write_gate(env, socket)
    run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "test refresh failure",
    )
    set_sudo_mode(env, "failed")

    proc, payload = run_ops(env, "keepalive", "refresh", "-t", "ops", check=False)

    assert proc.returncode == 1
    assert payload["action_required"] is True
    assert payload["state"] == "disabled"
    assert payload["last_refresh_ok"] is False
    assert payload["disabled_reason"] == "sudo keepalive refresh failed"


def test_keepalive_refresh_blocks_when_run_active_without_sending(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket)
    run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "active run",
    )
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    (state_dir / "active.json").write_text(
        json.dumps({"session": "ops", "run_id": "running-1", "status": "running"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    log_path.write_text("", encoding="utf-8")

    proc, payload = run_ops(env, "keepalive", "refresh", "-t", "ops", check=False)

    assert proc.returncode == 1
    assert payload["error"] == "keepalive pane operation blocked"
    assert "run already active" in payload["detail"]
    assert not send_targets(log_path, contains="keepalive-refresh")


def test_keepalive_refresh_blocks_when_refresh_lock_exists(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket)
    run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "1",
        "--reason",
        "locked refresh",
    )
    lock_path = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "pane-operation.lock"
    lock_path.write_text("locked\n", encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    proc, payload = run_ops(env, "keepalive", "refresh", "-t", "ops", check=False)

    assert proc.returncode == 1
    assert payload["error"] == "keepalive pane operation blocked"
    assert payload["detail"] == "tmux pane operation already in progress"
    assert not send_targets(log_path, contains="keepalive-refresh")


def test_keepalive_request_blocks_when_run_active_without_sending_probe(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket, checked_at=time.time() - 120)
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    (state_dir / "active.json").write_text(
        json.dumps({"session": "ops", "run_id": "running-1", "status": "running"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    log_path.write_text("", encoding="utf-8")

    proc, payload = run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "active request",
        check=False,
    )

    assert proc.returncode == 1
    assert payload["error"] == "keepalive pane operation blocked"
    assert "run already active" in payload["detail"]
    assert not send_targets(log_path, contains="probe.sh")
    assert not (state_dir / "keepalive.json").exists()


def test_keepalive_request_blocks_when_pane_operation_lock_exists_without_sending_probe(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket, checked_at=time.time() - 120)
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    (state_dir / "pane-operation.lock").write_text("locked\n", encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    proc, payload = run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "locked request",
        check=False,
    )

    assert proc.returncode == 1
    assert payload["error"] == "keepalive pane operation blocked"
    assert payload["detail"] == "tmux pane operation already in progress"
    assert not send_targets(log_path, contains="probe.sh")
    assert not (state_dir / "keepalive.json").exists()


@pytest.mark.parametrize("delay_completion", [False, True])
def test_keepalive_request_never_starts_password_authentication(tmux_env, tmp_path, delay_completion):
    env, socket = tmux_env
    calls = install_credential_sudo(tmp_path, env)
    if delay_completion:
        delay_probe_completion_signal(tmp_path, env)
    env["NODE-WEST.EXAMPLE_SUDO_KEY"] = "synthetic-must-not-be-read"
    env["GENERIC_SUDO_KEY"] = "synthetic-must-not-be-read-either"
    run_ops(env, "pick")

    proc, payload = run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "requires manual sudo",
        check=False,
    )

    assert proc.returncode == 1
    assert payload["error"] == "sudo gate not ready", payload
    if delay_completion:
        assert payload["credential_outcome"] == "blocked", payload
    if payload["credential_outcome"] == "blocked":
        transient_detail = "sudo probe result is present but the pane has not returned to idle"
        assert payload["detail"] == transient_detail, payload
        state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
        pending_path = state_dir / "probe.pending.json"
        assert pending_path.is_file(), payload
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        operation_id = pending["operation_id"]
        assert pending["allow_credentials"] is False
        assert operation_id == payload["operation_id"]
        assert pending["pane_id"] == payload["pane_id"]
        assert pending["pane_tty"] == payload["pane_tty"]
        result_path = state_dir / f"probe-{operation_id}.status.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["operation_id"] == operation_id
        assert result["session"] == "ops"
        assert result["pane_id"] == pending["pane_id"]
        assert result["pane_tty"] == pending["pane_tty"]
        deadline = time.monotonic() + 3
        while payload["credential_outcome"] == "blocked" and time.monotonic() < deadline:
            assert json.loads(pending_path.read_text(encoding="utf-8")) == pending
            time.sleep(0.02)
            _, payload = run_ops(env, "probe", "-t", "ops", check=False)
            assert payload["operation_id"] == operation_id, payload
            if payload["credential_outcome"] == "blocked":
                assert payload["detail"] == transient_detail, payload
        assert not pending_path.exists(), payload
    assert payload["credential_outcome"] == "manual", payload
    assert payload["credential_candidates_checked"] is False, payload
    assert calls.read_text(encoding="utf-8").splitlines() == ["-Nnv"]


def test_keepalive_request_blocks_when_pane_not_idle_without_sending_probe(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket, checked_at=time.time() - 120)
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    subprocess.run(
        ["tmux", "-L", socket, "send-keys", "-t", "ops", "sleep 5", "Enter"],
        check=True,
    )
    time.sleep(0.2)
    log_path.write_text("", encoding="utf-8")

    proc, payload = run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "busy request",
        check=False,
    )

    assert proc.returncode == 1
    assert payload["error"] == "keepalive pane operation blocked"
    assert payload["detail"] == "pane is not idle"
    assert not send_targets(log_path, contains="probe.sh")
    assert not (state_dir / "keepalive.json").exists()
    subprocess.run(["tmux", "-L", socket, "send-keys", "-t", "ops", "C-c"], check=False)


def test_probe_blocks_when_pane_operation_lock_exists_without_sending(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    (state_dir / "pane-operation.lock").write_text("locked\n", encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    _, payload = run_ops(env, "probe", "-t", "ops")

    assert payload["sudo"] == "failed"
    assert payload["detail"] == "tmux pane operation already in progress"
    assert not send_targets(log_path, contains="probe.sh")
    assert not list(state_dir.glob("probe-*.sh"))


def test_run_blocks_when_pane_operation_lock_exists_without_sending(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    write_gate(env, socket)
    state_dir = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops"
    (state_dir / "pane-operation.lock").write_text("locked\n", encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    proc, payload = run_ops(env, "run", "-t", "ops", "--", "echo", "blocked", check=False)

    assert proc.returncode == 1
    assert payload["error"] == "tmux pane operation already active"
    assert not send_targets(log_path, contains="TMUX_OPS_RUN_ID=")
    assert not (state_dir / "active.json").exists()


def test_keepalive_status_disables_expired_marker(tmux_env):
    env, socket = tmux_env
    run_ops(env, "pick")
    write_gate(env, socket)
    run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "expires",
    )
    marker_path = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "keepalive.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["expires_at"] = time.time() - 1
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")

    _, status = run_ops(env, "keepalive", "status", "-t", "ops")

    row = status["sessions"][0]
    assert row["state"] == "disabled"
    assert row["disabled_reason"] == "keepalive marker expired"


def test_keepalive_status_disables_malformed_marker(tmux_env):
    env, _socket = tmux_env
    run_ops(env, "pick")
    marker_path = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "keepalive.json"
    marker_path.write_text("[]\n", encoding="utf-8")

    _, status = run_ops(env, "keepalive", "status", "-t", "ops")

    row = status["sessions"][0]
    assert row["state"] == "disabled"
    assert row["disabled_reason"] == "keepalive marker is malformed"
    stored = json.loads(marker_path.read_text(encoding="utf-8"))
    assert stored["state"] == "disabled"
    assert stored["disabled_reason"] == "keepalive marker is malformed"


def test_keepalive_sweep_disables_malformed_marker(tmux_env):
    env, _socket = tmux_env
    run_ops(env, "pick")
    marker_path = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "keepalive.json"
    marker_path.write_text('"bad"\n', encoding="utf-8")

    _, sweep = run_ops(env, "keepalive", "sweep")

    row = sweep["sessions"][0]
    assert row["state"] == "disabled"
    assert row["disabled_reason"] == "keepalive marker is malformed"


def test_keepalive_refresh_disables_malformed_marker_without_sending(tmux_env):
    env, socket = tmux_env
    log_path = install_tmux_wrapper(Path(env["TMUX_OPS_STATE_ROOT"]).parent, env, socket)
    run_ops(env, "pick")
    marker_path = Path(env["TMUX_OPS_STATE_ROOT"]) / "ops" / "keepalive.json"
    marker_path.write_text("1\n", encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    proc, payload = run_ops(env, "keepalive", "refresh", "-t", "ops", check=False)

    assert proc.returncode == 1
    assert payload["error"] == "keepalive marker disabled"
    assert payload["state"] == "disabled"
    assert payload["disabled_reason"] == "keepalive marker is malformed"
    assert not send_targets(log_path, contains="keepalive-refresh")


def test_keepalive_clear_requires_matching_owner(tmux_env):
    env, socket = tmux_env
    run_ops(env, "pick")
    write_gate(env, socket)
    run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "test clear",
    )

    proc, mismatch = run_ops(env, "keepalive", "clear", "-t", "ops", "--owner", "other", check=False)
    assert proc.returncode == 1
    assert mismatch["error"] == "keepalive owner mismatch"

    _, cleared = run_ops(env, "keepalive", "clear", "-t", "ops", "--owner", "agent-1")
    assert cleared["ok"] is True
    assert cleared["state"] == "disabled"
    assert cleared["disabled_reason"] == "cleared"


def test_keepalive_rejects_invalid_and_unmanaged_sessions(tmux_env):
    env, socket = tmux_env

    proc, invalid = run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "not-ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "invalid",
        check=False,
    )
    assert proc.returncode == 1
    assert invalid["error"] == "invalid session name"

    subprocess.run(["tmux", "-L", socket, "new-session", "-d", "-s", "ops"], check=True)
    proc, unmanaged = run_ops(
        env,
        "keepalive",
        "request",
        "-t",
        "ops",
        "--owner",
        "agent-1",
        "--ttl-seconds",
        "60",
        "--max-refreshes",
        "2",
        "--reason",
        "unmanaged",
        check=False,
    )
    assert proc.returncode == 1
    assert unmanaged["error"] == "session is not managed"


def test_skill_documents_managed_run_path():
    skill = (ROOT / "ls" / "workflows" / "ls-wf-sudo-tmux-session" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "tmux send-keys" not in skill
    assert "tmux_ops run" in skill
    sequence = skill.split("## Sequence", 1)[1]
    assert "pick" in sequence
    assert "probe" in sequence
    assert "run" in sequence
    assert "send -t" not in sequence
