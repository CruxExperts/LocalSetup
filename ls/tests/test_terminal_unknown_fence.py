"""Unknown privileged starts retain their pane lease and cannot be replayed."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from ls.core.terminal_ops import sudo_bridge
from ls.core.terminal_ops import sudo
from ls.core.terminal_ops.models import PaneBinding, PaneRequest, PaneStatus, PaneTransition
from ls.core.terminal_ops.pane import ManagedPaneService
from ls.core.terminal_ops.shell import finish_transition, reconcile_transition


def test_live_sudo_registration_failure_keeps_unknown_lease(tmp_path: Path, monkeypatch) -> None:
    request = PaneRequest("tmux", str(tmp_path / "tmux.sock"), "localsetup", "controller-1",
                          "%0", str(tmp_path), session_id="$1")
    operation_id = "live-register-failure"
    ops_root = tmp_path / "terminal_ops"
    ops_root.mkdir(mode=0o700)
    ops_root.chmod(0o700)
    directory = tmp_path / "terminal_ops" / request.binding_key
    directory.mkdir(mode=0o700)
    lease = directory / "writer-lease"
    lease.mkdir(mode=0o700)
    (lease / "lease.json").write_text(json.dumps({"binding_key": request.binding_key,
                                                   "operation_id": operation_id, "token": "exact-token"}))
    record = {
        "binding_key": request.binding_key, "operation_id": operation_id, "state": "running",
        "shell_pid": os.getppid(), "target": "localsetup", "endpoint": request.endpoint,
        "controller_id": request.controller_id, "working_directory": str(tmp_path),
        "lease_token": "exact-token", "transition": {"requires_sudo_receipt": True},
    }
    (directory / f"{operation_id}.json").write_text(json.dumps(record))
    socket_dir = tmp_path / "private"
    socket_dir.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "started"
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        f"#!{sys.executable}\n"
        "import os,pathlib,time\n"
        "pathlib.Path(os.environ['FAKE_STARTED']).open('a').write('once\\n')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STARTED", str(marker))
    monkeypatch.setattr(sudo._AskpassBroker, "register_sudo", lambda self, process: False)
    monkeypatch.setattr(sys, "argv", ["terminal-sudo-bridge", "--state-dir", str(tmp_path),
                                     "--binding-key", request.binding_key, "--operation-id", operation_id,
                                     "--socket", str(socket_dir / "unused.sock"), "--", "/usr/bin/id"])
    assert sudo_bridge.main() == 2
    safe = json.loads((directory / f"{operation_id}.sudo.json").read_text())
    assert safe["status"] == "unknown"
    pid = safe["sudo_pid"]
    assert isinstance(pid, int) and pid > 0
    try:
        os.kill(pid, 0)
        finished = finish_transition(tmp_path, binding_key=request.binding_key,
                                     operation_id=operation_id, exit_code=2)
        assert finished["state"] == "unknown"
        assert lease.is_dir()
        binding = PaneBinding(request.name, request.binding_key, "tmux", request.endpoint,
                              request.parent_id, "%1", request.repository, request.controller_id,
                              session_id=request.session_id, shell_pid=record["shell_pid"], cwd=str(tmp_path),
                              metadata={"prompt_nonce": "fresh"})
        ready = PaneStatus("ready", "tmux", request.binding_key, request.name, binding)

        class Native:
            def inspect(self, _request):
                return ready

            def send_trigger(self, _request, _binding):
                raise AssertionError("unknown operation must never redispatch")

        store = {request.binding_key: {"binding_key": request.binding_key,
                                       "request": request.to_dict(), "status": ready.to_dict(),
                                       "last_binding": binding.to_dict(), "operation": finished}}
        service = ManagedPaneService(store, {"tmux": Native()}, state_dir=tmp_path)
        assert service.inspect(request).state == "unknown"
        assert service.transition(request, PaneTransition("next", ("/usr/bin/id",))).state == "unknown"
    finally:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)


def test_concurrent_completion_and_reconciliation_never_demote_completed_receipt(tmp_path: Path) -> None:
    request = PaneRequest("tmux", str(tmp_path / "tmux.sock"), "project", "controller-2",
                          "%1", str(tmp_path), session_id="$2")
    operation_id = "serialized-completion"
    ops_root = tmp_path / "terminal_ops"
    ops_root.mkdir(mode=0o700)
    ops_root.chmod(0o700)
    directory = tmp_path / "terminal_ops" / request.binding_key
    directory.mkdir(mode=0o700)
    lease = directory / "writer-lease"
    lease.mkdir(mode=0o700)
    (lease / "lease.json").write_text(json.dumps({
        "binding_key": request.binding_key, "operation_id": operation_id, "token": "lease-token",
    }))
    operation = {
        "binding_key": request.binding_key, "operation_id": operation_id, "state": "running",
        "lease_token": "lease-token", "transition": {"requires_sudo_receipt": False},
    }
    (directory / f"{operation_id}.json").write_text(json.dumps(operation))
    workers = 24
    barrier = threading.Barrier(workers)
    finished: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            if index % 2:
                value = finish_transition(tmp_path, binding_key=request.binding_key,
                                          operation_id=operation_id, exit_code=index % 4)
                with lock:
                    finished.append(value)
            else:
                reconcile_transition(tmp_path, binding_key=request.binding_key,
                                     operation_id=operation_id)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    receipt = json.loads((directory / f"{operation_id}.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "completed"
    assert receipt["exit_code"] in {1, 3}
    assert finished and all(value["state"] == "completed" for value in finished)
    assert not lease.exists()


def test_managed_inspection_fences_staged_receipt_after_prompt_generation_changes(tmp_path: Path) -> None:
    request = PaneRequest("tmux", str(tmp_path / "tmux.sock"), "project", "controller-3",
                          "%3", str(tmp_path), session_id="$3")
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    ops_root = state_dir / "terminal_ops"
    ops_root.mkdir(mode=0o700)
    ops_root.chmod(0o700)
    directory = ops_root / request.binding_key
    directory.mkdir(mode=0o700)
    operation_id = "staged-old-prompt"
    transition = PaneTransition(operation_id, ("/usr/bin/true",))
    lease_token = "staged-lease-token"
    lease = directory / "writer-lease"
    lease.mkdir(mode=0o700)
    lease_record = {"binding_key": request.binding_key, "operation_id": operation_id,
                    "token": lease_token}
    (lease / "lease.json").write_text(json.dumps(lease_record), encoding="utf-8")
    (lease / "lease.json").chmod(0o600)
    operation = {
        "schema_version": 1,
        "binding_key": request.binding_key,
        "operation_id": operation_id,
        "state": "staged",
        "transition": transition.to_dict(),
        "pane_id": "%3",
        "shell_pid": os.getpid(),
        "pane_tty": "/dev/pts/3",
        "shell_path": "/bin/bash",
        "machine_generation": "generation-3",
        "prompt_nonce": "old-prompt",
        "lease_token": lease_token,
    }
    receipt_path = directory / f"{operation_id}.json"
    receipt_path.write_text(json.dumps(operation), encoding="utf-8")
    receipt_path.chmod(0o600)
    (directory / "shell.json").write_text(json.dumps({
        "binding_key": request.binding_key,
        "phase": "primary",
        "keymap_ready": True,
        "prompt_nonce": "new-prompt",
        "pane_id": "%3",
        "shell_pid": os.getpid(),
        "pane_tty": "/dev/pts/3",
        "shell_path": "/bin/bash",
        "machine_generation": "generation-3",
    }), encoding="utf-8")
    (directory / "shell.json").chmod(0o600)
    binding = PaneBinding(
        request.name, request.binding_key, "tmux", request.endpoint, request.parent_id,
        "%3", request.repository, request.controller_id, session_id=request.session_id,
        pane_tty="/dev/pts/3", shell_pid=os.getpid(), shell_path="/bin/bash",
        machine_generation="generation-3", cwd=request.cwd,
    )
    ready = PaneStatus("ready", "tmux", request.binding_key, request.name, binding)

    class Native:
        def inspect(self, _request):
            return ready

    store = {request.binding_key: {
        "binding_key": request.binding_key,
        "request": request.to_dict(),
        "status": ready.to_dict(),
        "last_binding": binding.to_dict(),
        "operation": operation,
    }}
    service = ManagedPaneService(store, {"tmux": Native()}, state_dir=state_dir)

    observed = service.inspect(request)

    assert observed.state == "unknown"
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["state"] == "unknown"
    assert lease.is_dir(), "a stale staged lease stays fenced for safe reconciliation"
