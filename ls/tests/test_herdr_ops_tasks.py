from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

from ls.core.herdr_ops import tasks
from ls.core.herdr_ops.errors import HerdrOpsError


ENDPOINT = "/tmp/herdr-task-fixture.sock"
WORKSPACE = "w1"
CALLER = "w1:p1"


def _pane(pane_id: str):
    return {"pane_id": pane_id, "terminal_id": "task-terminal", "workspace_id": WORKSPACE, "tab_id": "w1:t1", "label": None}


def _fixture(monkeypatch, *, send_error: HerdrOpsError | None = None):
    state = {"calls": [], "send_count": 0, "ready": ""}

    def create(endpoint, workspace, caller, cwd, *, state_dir, owner_id, purpose):
        return {"owner_id": owner_id, "pane": _pane("w1:p8"), "state": "owned"}

    def owned(endpoint, identity, *, state_dir):
        return ({"owner_id": identity, "terminal_id": "task-terminal"}, _pane("w1:p8"))

    def request(endpoint, method, params=None, **kwargs):
        state["calls"].append((method, params, kwargs))
        if method == "pane.send_input":
            state["send_count"] += 1
            text = params["text"]
            ready = re.search(r"(__LSHERDR_READY_[a-f0-9]{32}__)", text)
            if ready:
                state["ready"] = ready.group(1)
            if send_error is not None and state["send_count"] == 3:
                raise send_error
            return {"type": "ok"}
        if method == "pane.process_info":
            return {"type": "pane_process_info", "process_info": {"pane_id": "w1:p8", "shell_pid": 123, "foreground_processes": [{"pid": 123, "name": "bash", "argv0": "bash"}]}}
        if method == "pane.read":
            return {"type": "pane_read", "read": {"text": state["ready"]}}
        raise AssertionError(method)

    monkeypatch.setattr(tasks, "create_owned_pane", create)
    monkeypatch.setattr(tasks, "owned_pane", owned)
    monkeypatch.setattr(tasks, "request", request)
    monkeypatch.setattr(tasks, "verify_local_herdr_endpoint", lambda _endpoint: None)
    monkeypatch.setattr(tasks, "cleanup_owned_pane", lambda endpoint, identity, **kwargs: {"owner_id": identity, "state": "closed", "pane_id": "w1:p8"})
    return state


def test_task_requires_atomic_request_specific_real_exit_receipt(tmp_path: Path, monkeypatch):
    fixture = _fixture(monkeypatch)
    state_dir = tmp_path / "state"
    payload = "safe; touch should-not-exist"
    first = tasks.run_task(
        ENDPOINT,
        WORKSPACE,
        CALLER,
        str(tmp_path),
        ["python3", "-c", "import sys; print(sys.argv[1]); raise SystemExit(17)", payload],
        state_dir=state_dir,
    )
    assert first["state"] == "running"
    assert first["completion"] == "pending"
    assert first["owner_id"] == first["task_id"]
    task_path = state_dir / "tasks" / f"{first['task_id']}.json"
    stored = json.loads(task_path.read_text())
    assert stored["argv_sha256"]
    assert "argv" not in stored
    assert fixture["send_count"] == 3
    sends = [params for method, params, _ in fixture["calls"] if method == "pane.send_input"]
    assert sends[0] == {"pane_id": "w1:p8", "text": "exec /bin/bash --noprofile --norc", "keys": ["enter"]}
    assert "--noprofile --norc" in sends[2]["text"]
    assert sends[2]["keys"] == ["enter"]

    runner = Path(stored["runner_path"])
    execution = subprocess.run(["/bin/bash", "--noprofile", "--norc", str(runner)], capture_output=True, text=True, check=False)
    assert execution.returncode == 17
    assert payload in execution.stdout
    assert not (tmp_path / "should-not-exist").exists()

    completed = tasks.task_status(ENDPOINT, first["task_id"], state_dir=state_dir)
    assert completed["state"] == "failed"
    assert completed["completion"] == "confirmed"
    assert completed["owner_id"] == first["task_id"]
    assert completed["exit_code"] == 17
    assert completed["receipt"]["kind"] == "atomic-exit"
    cleaned = tasks.cleanup_task(ENDPOINT, first["task_id"], state_dir=state_dir)
    assert cleaned["state"] == "failed"
    assert cleaned["exit_code"] == 17
    assert cleaned["pane_cleanup"]["state"] == "closed"


def test_lost_task_input_ack_marks_unknown_without_replay(tmp_path: Path, monkeypatch):
    fixture = _fixture(monkeypatch, send_error=HerdrOpsError("transport_error", "connection lost", "unknown"))
    result = tasks.run_task(ENDPOINT, WORKSPACE, CALLER, "/work", ["true"], state_dir=tmp_path / "state")
    assert result["state"] == "unknown"
    assert result["effect"] == "unknown"
    assert fixture["send_count"] == 3
    status = tasks.task_status(ENDPOINT, result["task_id"], state_dir=tmp_path / "state")
    assert status["state"] == "unknown"
    assert fixture["send_count"] == 3


def test_cancel_closes_only_the_recorded_task_owner(tmp_path: Path, monkeypatch):
    fixture = _fixture(monkeypatch)
    run = tasks.run_task(ENDPOINT, WORKSPACE, CALLER, "/work", ["sleep", "10"], state_dir=tmp_path / "state")
    result = tasks.cancel_task(ENDPOINT, run["task_id"], state_dir=tmp_path / "state")
    assert result["state"] == "unknown"
    assert result["cancellation"].startswith("owned-pane-close-confirmed")
    assert fixture["send_count"] == 3


def test_runner_uses_atomic_json_receipt_and_no_interactive_user_shell():
    script = tasks._runner_script(["printf", "%s", "x; touch /tmp/no"], "/tmp/work", "a" * 32, "b" * 32, "/tmp/state/receipts/a.json", "__LSHERDR_EXIT_token__").decode()
    assert "umask 077" in script
    assert "mv -f \"$__lsherdr_tmp\"" in script
    assert "__LSHERDR_EXIT_token__" in script
    assert "set +e" in script
    assert "'x; touch /tmp/no'" in script
    assert "-i" not in script
    with pytest.raises(HerdrOpsError) as caught:
        tasks._validate_argv(["echo", "first\nsecond"])
    assert caught.value.code == "invalid_command"


def test_remote_receipts_require_explicit_target_and_absolute_remote_state():
    with pytest.raises(HerdrOpsError) as caught:
        tasks._remote_receipt_store("host", None)
    assert caught.value.code == "remote_state_dir_required"
    with pytest.raises(HerdrOpsError) as caught:
        tasks._remote_receipt_store("host", "../state")
    assert caught.value.code == "invalid_remote_state_dir"
    assert tasks._remote_receipt_store("ops@box", "/var/tmp/localsetup-herdr") == {
        "kind": "ssh",
        "target": "ops@box",
        "state_dir": "/var/tmp/localsetup-herdr",
    }


def test_remote_staging_shell_executes_runner_and_writes_real_exit_receipt(tmp_path: Path, monkeypatch):
    task_id, nonce = "a" * 32, "b" * 32
    store = {"target": "fixture", "state_dir": str(tmp_path / "remote-state")}
    expected_receipt = str(Path(store["state_dir"]) / task_id / "receipt.json")
    runner = tasks._runner_script(["bash", "-c", "exit 17"], str(tmp_path), task_id, nonce,
                                  expected_receipt, "__LSHERDR_EXIT_fixture__")

    def local_ssh(_target, argv, *, stdin=None, timeout=30):
        return subprocess.run(argv, input=stdin, capture_output=True, check=False, timeout=timeout)

    monkeypatch.setattr(tasks, "_ssh_run", local_ssh)
    runner_path, receipt_path = tasks._store_remote_runner(store, task_id, runner)
    assert Path(runner_path).read_bytes() == runner
    completed = subprocess.run(["bash", runner_path], capture_output=True, check=False)
    assert completed.returncode == 17
    assert json.loads(Path(receipt_path).read_text()) == {
        "schema_version": 1, "task_id": task_id, "nonce": nonce, "exit_code": 17,
    }


def test_remote_receipt_target_mismatch_fails_before_pane_creation(tmp_path: Path, monkeypatch):
    fixture = _fixture(monkeypatch)
    state = tmp_path / "state"
    tunnels = state / "tunnels"
    tunnels.mkdir(parents=True)
    (tunnels / f"{'a' * 32}.json").write_text(json.dumps({
        "owner_id": "a" * 32, "kind": "unix_socket", "target": "host-a",
        "local_endpoint": str(Path(ENDPOINT).absolute()), "remote_endpoint": "/remote/herdr.sock",
        "process": {"pid": 99999},
    }))
    staged: list[bool] = []
    monkeypatch.setattr(tasks, "_ssh_run", lambda *_args, **_kw: staged.append(True))
    result = tasks.run_task(ENDPOINT, WORKSPACE, CALLER, "/remote/work", ["true"],
                            state_dir=state, ssh_target="host-b", remote_state_dir="/tmp/receipts")
    assert result["state"] == "failed"
    assert result["error"]["code"] == "receipt_route_mismatch"
    assert staged == []
    assert fixture["send_count"] == 0


def test_remote_runner_is_staged_and_receipt_is_read_on_the_same_explicit_route(tmp_path: Path, monkeypatch):
    fixture = _fixture(monkeypatch)
    from ls.core.herdr_ops import remote
    monkeypatch.setattr(remote, "verify_receipt_tunnel", lambda *_args, **_kwargs: {
        "target": "ops@remote-box", "remote_socket": "/remote/herdr.sock",
        "tunnel_owner": "a" * 32, "ssh_settings": {"hostname": "remote-box"},
    })
    monkeypatch.setattr(remote, "_qualify_ssh_target", lambda *_args, **_kwargs: {"hostname": "remote-box"})
    calls = []
    receipt = {}

    def ssh_run(target, argv, *, stdin=None, timeout=30):
        calls.append((target, list(argv), stdin, timeout))
        if argv[0] == "bash":
            assert isinstance(stdin, bytes) and b"atomic-exit" not in stdin
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert argv[0] == "cat"
        return subprocess.CompletedProcess(argv, 0, json.dumps(receipt).encode(), b"")

    monkeypatch.setattr(tasks, "_ssh_run", ssh_run)
    result = tasks.run_task(
        ENDPOINT,
        WORKSPACE,
        CALLER,
        "/remote/work",
        ["true"],
        state_dir=tmp_path / "state",
        ssh_target="ops@remote-box",
        remote_state_dir="/var/tmp/herdr-receipts",
    )
    assert result["state"] == "running"
    stored = json.loads((tmp_path / "state" / "tasks" / f"{result['task_id']}.json").read_text())
    receipt.update({"schema_version": 1, "task_id": result["task_id"], "nonce": stored["nonce"], "exit_code": 0})
    done = tasks.task_status(ENDPOINT, result["task_id"], state_dir=tmp_path / "state")
    assert done["state"] == "completed"
    assert calls[0][0] == calls[1][0] == "ops@remote-box"
    assert calls[0][1][:2] == ["bash", "-c"]
    assert calls[1][1] == ["cat", f"/var/tmp/herdr-receipts/{result['task_id']}/receipt.json"]
    assert fixture["send_count"] == 3


def test_remote_wait_uses_pane_marker_before_receipt_readback(tmp_path: Path, monkeypatch):
    _fixture(monkeypatch)
    from ls.core.herdr_ops import remote
    monkeypatch.setattr(remote, "verify_receipt_tunnel", lambda *_args, **_kwargs: {
        "target": "ops@remote-box", "remote_socket": "/remote/herdr.sock",
        "tunnel_owner": "a" * 32, "ssh_settings": {"hostname": "remote-box"},
    })
    monkeypatch.setattr(remote, "_qualify_ssh_target", lambda *_args, **_kwargs: {"hostname": "remote-box"})
    ssh_calls = []
    receipt = {}
    allow_receipt = {"value": False}

    def ssh_run(target, argv, *, stdin=None, timeout=30):
        ssh_calls.append((target, list(argv), timeout))
        if argv[0] == "bash":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if allow_receipt["value"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(receipt).encode(), b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(tasks, "_ssh_run", ssh_run)
    run = tasks.run_task(
        ENDPOINT,
        WORKSPACE,
        CALLER,
        "/remote/work",
        ["true"],
        state_dir=tmp_path / "state",
        ssh_target="ops@remote-box",
        remote_state_dir="/var/tmp/herdr-receipts",
    )
    stored = json.loads((tmp_path / "state" / "tasks" / f"{run['task_id']}.json").read_text())
    receipt.update({"schema_version": 1, "task_id": run["task_id"], "nonce": stored["nonce"], "exit_code": 0})
    wait_calls = []

    def wait_request(endpoint, method, params=None, **kwargs):
        wait_calls.append((method, params, kwargs))
        assert method == "pane.wait_for_output"
        assert {key: value for key, value in params.items() if key != "timeout_ms"} == {
            "pane_id": "w1:p8",
            "source": "recent",
            "match": {"type": "substring", "value": stored["marker"]},
            "lines": tasks.MAX_READ_LINES,
        }
        assert 0 < params["timeout_ms"] <= 5000
        allow_receipt["value"] = True
        return {"type": "output_matched", "pane_id": "w1:p8", "matched_line": f"{stored['marker']}:0"}

    monkeypatch.setattr(tasks, "request", wait_request)
    done = tasks.wait_task(ENDPOINT, run["task_id"], timeout=5, state_dir=tmp_path / "state")
    assert done["state"] == "completed"
    assert done["exit_code"] == 0
    assert len(ssh_calls) == 3
    assert ssh_calls[0][0] == ssh_calls[1][0] == ssh_calls[2][0] == "ops@remote-box"
    assert ssh_calls[0][1][:2] == ["bash", "-c"]
    assert ssh_calls[1][1] == ssh_calls[2][1] == ["cat", f"/var/tmp/herdr-receipts/{run['task_id']}/receipt.json"]
    assert len(wait_calls) == 1


def test_wait_duration_is_bounded_and_negative_timeout_rejected():
    assert tasks.bounded_wait(100000) == 600
    with pytest.raises(HerdrOpsError) as caught:
        tasks.bounded_wait(-1)
    assert caught.value.code == "invalid_timeout"
