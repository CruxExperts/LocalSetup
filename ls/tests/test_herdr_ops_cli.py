from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import types

from ls.core.herdr_ops import cli


def _install_router_stubs(monkeypatch):
    remote = types.ModuleType("ls.core.herdr_ops.remote")
    remote.add_parser = lambda sub: sub.add_parser("remote")
    remote.dispatch = lambda args, root: {"command": "remote", "root": str(root)}
    provenance = types.ModuleType("ls.core.herdr_ops.provenance")
    provenance.add_parser = lambda sub: sub.add_parser("provenance")
    provenance.dispatch = lambda args, root: {"command": "provenance", "root": str(root)}
    monkeypatch.setitem(sys.modules, remote.__name__, remote)
    monkeypatch.setitem(sys.modules, provenance.__name__, provenance)


def test_context_filters_sudo_and_uses_explicit_socket_and_workspace(monkeypatch, capsys):
    _install_router_stubs(monkeypatch)
    captured = []
    snapshot = {
        "version": "0.9.5",
        "protocol": 22,
        "focused_workspace_id": "w1",
        "focused_tab_id": "w1:t1",
        "focused_pane_id": "w1:p2",
        "workspaces": [{"workspace_id": "w1"}],
        "tabs": [{"workspace_id": "w1", "tab_id": "w1:t1"}],
        "panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "label": "work"},
            {"pane_id": "w1:p2", "workspace_id": "w1", "label": "sudo"},
        ],
        "layouts": [{"workspace_id": "w1", "focused_pane_id": "w1:p2", "panes": [{"pane_id": "w1:p1"}, {"pane_id": "w1:p2"}]}],
        "agents": [{"pane_id": "w1:p2", "agent": "controller"}, {"pane_id": "w1:p1", "agent": "worker"}],
    }

    def fake_request(endpoint, method, params=None, **kwargs):
        captured.append((endpoint, method, params, kwargs))
        return {"type": "session_snapshot", "snapshot": snapshot}

    monkeypatch.setattr(cli, "request", fake_request)
    code = cli.main(["context", "--socket", "/tmp/fixture.sock", "--workspace", "w1"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert captured == [("/tmp/fixture.sock", "session.snapshot", {}, {})]
    safe = output["result"]["snapshot"]
    assert [p["pane_id"] for p in safe["panes"]] == ["w1:p1"]
    assert safe["focused_pane_id"] is None
    assert safe["layouts"][0]["focused_pane_id"] is None
    assert all(agent["pane_id"] != "w1:p2" for agent in safe["agents"])


def test_task_dry_run_does_not_create_state_or_send_input(monkeypatch, tmp_path: Path, capsys):
    _install_router_stubs(monkeypatch)
    calls = []

    def fake_get_pane(endpoint, pane_id, workspace_id=None):
        calls.append((endpoint, pane_id, workspace_id))
        return {"pane_id": pane_id, "workspace_id": workspace_id, "terminal_id": "caller", "tab_id": "w1:t1", "label": "work"}

    monkeypatch.setattr(cli, "get_pane", fake_get_pane)
    state_dir = tmp_path / "unused-state"
    code = cli.main(
        [
            "task",
            "run",
            "--socket",
            "/tmp/fixture.sock",
            "--workspace",
            "w1",
            "--pane",
            "w1:p1",
            "--state-dir",
            str(state_dir),
            "--cwd",
            "/work/project",
            "--dry-run",
            "--",
            "python3",
            "-c",
            "print(1)",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["result"]["planned"] == "run-owned-task"
    assert calls == [("/tmp/fixture.sock", "w1:p1", "w1")]
    assert not state_dir.exists()


def test_remote_dispatch_receives_resolved_source_root(monkeypatch, tmp_path: Path, capsys):
    _install_router_stubs(monkeypatch)
    code = cli.main(["remote"], root=tmp_path)
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["result"] == {"command": "remote", "root": str(tmp_path.resolve())}


def test_error_is_structured_without_traceback(monkeypatch, capsys):
    _install_router_stubs(monkeypatch)

    def fail(*args, **kwargs):
        from ls.core.herdr_ops.errors import HerdrOpsError

        raise HerdrOpsError("protected_pane", "The sudo pane is protected.")

    monkeypatch.setattr(cli, "inspect_pane", fail)
    code = cli.main(["pane", "inspect", "--socket", "/tmp/fixture.sock", "--workspace", "w1", "--pane", "w1:p1"])
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert code == 2
    assert captured.out == ""
    assert error["error"]["code"] == "protected_pane"
    assert "Traceback" not in captured.err


def test_pane_read_maps_cli_hyphen_to_schema_wire_value(monkeypatch, capsys):
    _install_router_stubs(monkeypatch)
    calls = []
    monkeypatch.setattr(cli, "get_pane", lambda endpoint, pane_id, workspace_id=None: {"pane_id": pane_id, "workspace_id": workspace_id, "terminal_id": "term", "tab_id": "w1:t1"})
    monkeypatch.setattr(cli, "request", lambda endpoint, method, params=None, **kwargs: calls.append((method, params)) or {"read": {"text": "ok"}})
    code = cli.main(["pane", "read", "--socket", "/tmp/fixture.sock", "--workspace", "w1", "--pane", "w1:p1", "--source", "recent-unwrapped"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["result"]["read"]["text"] == "ok"
    assert calls == [("pane.read", {"pane_id": "w1:p1", "source": "recent_unwrapped", "lines": 80})]


def test_pane_ensure_inherits_only_matching_managed_local_caller(monkeypatch, capsys):
    _install_router_stubs(monkeypatch)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/local-herdr.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w-local")
    monkeypatch.setenv("HERDR_PANE_ID", "w-local:p-caller")
    calls = []

    def fake_get_pane(endpoint, pane_id, workspace_id=None):
        calls.append((endpoint, pane_id, workspace_id))
        return {"pane_id": pane_id, "workspace_id": workspace_id, "terminal_id": "caller", "tab_id": "w-local:t1", "label": "work"}

    monkeypatch.setattr(cli, "get_pane", fake_get_pane)
    code = cli.main(["pane", "ensure", "--socket", "/tmp/local-herdr.sock", "--cwd", "/work", "--dry-run"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["result"]["workspace_id"] == "w-local"
    assert output["result"]["caller_pane_id"] == "w-local:p-caller"
    assert calls == [("/tmp/local-herdr.sock", "w-local:p-caller", "w-local")]

    calls.clear()
    code = cli.main(["pane", "ensure", "--socket", "/tmp/remote-herdr.sock", "--cwd", "/work", "--dry-run"])
    error = json.loads(capsys.readouterr().err)
    assert code == 2
    assert error["error"]["code"] == "target_required"
    assert calls == []

    code = cli.main(["pane", "ensure", "--machine", "saved-host", "--cwd", "/work", "--dry-run"])
    error = json.loads(capsys.readouterr().err)
    assert code == 2
    assert error["error"]["code"] == "target_required"
    assert calls == []
