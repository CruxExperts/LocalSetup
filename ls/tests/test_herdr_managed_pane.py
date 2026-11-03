"""Focused regressions for managed Herdr pane ownership and observation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ls.core.terminal_ops import backends
from ls.core.terminal_ops.models import PaneBinding, PaneRequest, PaneStatus, PaneTransition
from ls.core.terminal_ops.pane import ManagedPaneService


@pytest.fixture
def herdr_request(tmp_path: Path) -> PaneRequest:
    return PaneRequest(
        "herdr", str(tmp_path / "herdr.sock"), "project", "controller-current",
        "parent-pane", str(tmp_path), workspace_id="workspace", machine_id="machine",
    )


def _owner_record(state_dir: Path, endpoint: str, pane_id: str, *, controller: str,
                  parent: str, workspace: str = "workspace") -> None:
    record = {
        "schema_version": 1,
        "owner_id": pane_id,
        "purpose": "managed_ops",
        "endpoint": str(Path(endpoint).absolute()),
        "workspace_id": workspace,
        "caller_pane_id": parent,
        "pane_id": pane_id,
        "terminal_id": f"terminal-{pane_id}",
        "tab_id": "tab-1",
        "cwd": "/tmp",
        "ratio": 1 / 3,
        "state": "owned",
        "owner_metadata": {
            "controller_id": controller,
            "parent_id": parent,
        },
    }
    owners = state_dir / "panes"
    owners.mkdir(parents=True, exist_ok=True, mode=0o700)
    (owners / f"{pane_id}.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize(
    ("owned", "other_controller", "expected"),
    [
        (True, True, "missing"),  # same label/parent, but an exact other controller owns it
        (False, False, "unknown"),  # an unowned label collision cannot be adopted
        (True, False, "unknown"),  # a same-controller collision is not an exact binding
    ],
)
def test_same_label_inventory_isolated_only_for_foreign_owned_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, herdr_request: PaneRequest,
    owned: bool, other_controller: bool, expected: str,
) -> None:
    request = herdr_request
    adapter = backends.HerdrAdapter(state_dir=tmp_path / "state")
    monkeypatch.setattr(adapter, "_endpoint", lambda _request: request.endpoint)
    pane = {
        "pane_id": "other-managed-pane", "workspace_id": request.workspace_id,
        "tab_id": "tab-1", "label": request.name,
    }
    inventory = [
        {"pane_id": request.parent_id, "workspace_id": request.workspace_id, "tab_id": "tab-1"},
        pane,
    ]
    monkeypatch.setattr(adapter, "_inventory", lambda _request: inventory)
    if owned:
        _owner_record(
            tmp_path / "state", request.endpoint, pane["pane_id"],
            controller="controller-other" if other_controller else request.controller_id,
            parent=request.parent_id,
        )

    status = adapter.inspect(request)

    assert status.state == expected
    assert status.binding is None
    assert status.binding_key == request.binding_key


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux process environment required")
def test_startup_home_and_nonempty_sudo_key_names_come_from_real_server_process(
    tmp_path: Path, herdr_request: PaneRequest,
) -> None:
    home = tmp_path / "server-home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "PATH": os.defpath,
        "GENERIC_SUDO_KEY": "dummy-generic-secret",
        "ALIEN_SUDO_KEY": "dummy-host-secret",
    }
    server = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        adapter = backends.HerdrAdapter(state_dir=tmp_path / "state")

        class EndpointClient:
            @staticmethod
            def endpoint_identity() -> dict[str, int]:
                return {"pid": server.pid}

        adapter.client = EndpointClient()
        names, startup_home = adapter._server_startup_environment(herdr_request)

        assert startup_home == str(home)
        assert names == {"GENERIC_SUDO_KEY", "ALIEN_SUDO_KEY"}
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_busy_original_shell_binding_is_observable_but_never_dispatchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = PaneRequest("herdr", str(tmp_path / "herdr.sock"), "project", "controller",
                          "parent", str(tmp_path), workspace_id="workspace")
    observed_binding = PaneBinding(
        name=request.name, binding_key=request.binding_key, backend="herdr",
        endpoint=request.endpoint, parent_id=request.parent_id, pane_id="child",
        repository=request.repository, controller_id=request.controller_id,
        workspace_id=request.workspace_id, tab_id="tab", terminal_id="terminal",
        pane_tty="/dev/pts/7", shell_pid=421,
        shell_path="/bin/bash", machine_generation="generation", cwd=str(tmp_path),
        state="busy", metadata={"prompt_nonce": "busy-nonce", "phase": "busy"},
    )
    monkeypatch.setattr(backends, "read_prompt_state", lambda *_args: {
        "pane_id": observed_binding.pane_id,
        "shell_pid": observed_binding.shell_pid,
        "pane_tty": observed_binding.pane_tty,
        "shell_path": observed_binding.shell_path,
        "cwd": observed_binding.cwd,
        "machine_generation": observed_binding.machine_generation,
        "phase": "busy",
        "keymap_ready": True,
        "prompt_nonce": "busy-nonce",
    })
    monkeypatch.setattr(backends, "machine_generation", lambda: "generation")
    monkeypatch.setattr(backends, "_process_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(backends, "_process_tty_matches", lambda _pid, _tty: True)
    monkeypatch.setattr(backends, "_same_path", lambda left, right: left == right)
    monkeypatch.setattr(backends, "_credential_names_in_process", lambda _pid: set())
    monkeypatch.setattr(backends, "_shell_owns_foreground", lambda _pid, _tty: False)
    native = backends.HerdrAdapter(state_dir=tmp_path)
    native.client = type("NativeClient", (), {
        "process_info": lambda self, pane: {
            "process_info": {"shell_pid": 421, "foreground_processes": [{"pid": 422}]},
        },
    })()
    monkeypatch.setattr(native, "_endpoint", lambda _: request.endpoint)
    monkeypatch.setattr(native, "_owned", lambda _: (
        {"caller_pane_id": request.parent_id, "workspace_id": request.workspace_id,
         "tab_id": "tab", "cwd": str(tmp_path)},
        {"pane_id": "child", "workspace_id": request.workspace_id, "tab_id": "tab",
         "terminal_id": "terminal", "label": request.name},
    ))
    monkeypatch.setattr(native, "_inventory", lambda _: [
        {"pane_id": request.parent_id, "tab_id": "tab"},
    ])
    readlink = os.readlink
    monkeypatch.setattr(os, "readlink", lambda path: (
        "/dev/pts/7" if str(path) == "/proc/421/fd/0" else readlink(path)
    ))
    status = native.inspect(request)
    assert status.state == "busy"
    assert status.binding == observed_binding

    class BusyAdapter:
        triggered = False

        def inspect(self, _request: PaneRequest) -> PaneStatus:
            return status

        def send_trigger(self, _request: PaneRequest, _binding: PaneBinding) -> None:
            self.triggered = True

    adapter = BusyAdapter()
    service = ManagedPaneService({}, {"herdr": adapter}, state_dir=tmp_path)
    result = service.transition(request, PaneTransition("must-not-dispatch", ("true",)))

    assert result.state == "busy"
    assert result.binding == observed_binding
    assert result.operation_id is None
    assert not adapter.triggered
    assert not (tmp_path / "terminal_ops" / request.binding_key / "must-not-dispatch.json").exists()
