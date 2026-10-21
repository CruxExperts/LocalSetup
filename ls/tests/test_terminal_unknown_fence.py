"""Unknown privileged starts retain their pane lease and cannot be replayed."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time

from ls.core.terminal_ops import sudo_bridge
from ls.core.terminal_ops import sudo
from ls.core.terminal_ops.models import PaneBinding, PaneRequest, PaneStatus, PaneTransition
from ls.core.terminal_ops.pane import ManagedPaneService
from ls.core.terminal_ops.shell import finish_transition


def test_live_sudo_registration_failure_keeps_unknown_lease(tmp_path: Path, monkeypatch) -> None:
    request = PaneRequest("tmux", str(tmp_path / "tmux.sock"), "localsetup", "controller-1",
                          "%0", str(tmp_path), session_id="$1")
    operation_id = "live-register-failure"
    directory = tmp_path / "terminal_ops" / request.binding_key
    directory.mkdir(parents=True, mode=0o700)
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
