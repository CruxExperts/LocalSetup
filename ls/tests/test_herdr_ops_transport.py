from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from ls.core.herdr_ops.errors import HerdrOpsError
from ls.core.herdr_ops.transport import machine_snapshot, request, resolve_endpoint


class FakeSocketServer:
    def __init__(self, path: Path, reply):
        self.path = path
        self.reply = reply
        self.requests = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        connection, _ = self.listener.accept()
        with connection:
            raw = bytearray()
            while b"\n" not in raw:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                raw.extend(chunk)
            value = json.loads(bytes(raw).split(b"\n", 1)[0])
            self.requests.append(value)
            response = self.reply(value)
            if response is not None:
                connection.sendall(json.dumps(response).encode("utf-8") + b"\n")

    def close(self):
        self.listener.close()
        self.thread.join(timeout=1)


def test_request_uses_explicit_endpoint_and_round_trips_request_id(tmp_path: Path):
    endpoint = tmp_path / "herdr.sock"
    server = FakeSocketServer(endpoint, lambda req: {"id": req["id"], "result": {"type": "pong"}})
    try:
        assert request(endpoint, "ping", {}) == {"type": "pong"}
        assert server.requests[0]["method"] == "ping"
        assert server.requests[0]["params"] == {}
        assert server.requests[0]["id"].startswith("ls_")
    finally:
        server.close()


def test_lost_mutation_response_is_unknown_and_never_replayed(tmp_path: Path):
    endpoint = tmp_path / "herdr.sock"
    server = FakeSocketServer(endpoint, lambda _req: None)
    try:
        with pytest.raises(HerdrOpsError) as caught:
            request(endpoint, "pane.send_input", {"pane_id": "w1:p4", "text": "echo hi"}, mutation=True, timeout=0.2)
        assert caught.value.effect == "unknown"
        assert len(server.requests) == 1
    finally:
        server.close()


def test_endpoint_precedence_uses_named_session_before_environment_socket(tmp_path: Path):
    endpoint = resolve_endpoint(
        session="work",
        environ={"HERDR_SOCKET_PATH": "/ignored/custom.sock", "HERDR_CONFIG_PATH": str(tmp_path / "config.toml")},
    )
    assert endpoint == str(tmp_path / "sessions" / "work" / "herdr.sock")
    assert resolve_endpoint(environ={"HERDR_SOCKET_PATH": "/tmp/explicit.sock"}) == "/tmp/explicit.sock"


def test_implicit_endpoint_requires_herdr_context(tmp_path: Path):
    with pytest.raises(HerdrOpsError) as caught:
        resolve_endpoint(environ={}, home=tmp_path)
    assert caught.value.code == "endpoint_required"
    assert resolve_endpoint(environ={"HERDR_ENV": "1"}, home=tmp_path) == str(tmp_path / ".config" / "herdr" / "herdr.sock")


def test_default_session_environment_requires_context_marker(tmp_path: Path):
    with pytest.raises(HerdrOpsError) as caught:
        resolve_endpoint(environ={"HERDR_SESSION": "work", "HERDR_CONFIG_PATH": str(tmp_path / "config.toml")})
    assert caught.value.code == "endpoint_required"
    assert resolve_endpoint(environ={"HERDR_ENV": "1", "HERDR_SESSION": "work", "HERDR_CONFIG_PATH": str(tmp_path / "config.toml")}) == str(tmp_path / "sessions" / "work" / "herdr.sock")


def test_endpoint_rejects_session_traversal():
    with pytest.raises(HerdrOpsError) as caught:
        resolve_endpoint(session="../other")
    assert caught.value.code == "invalid_session"


def test_saved_machine_snapshot_uses_herdr_cli_without_shell(monkeypatch):
    seen = {}
    monkeypatch.setenv("HERDR_BIN_PATH", "herdr")

    class Completed:
        returncode = 0
        stdout = '{"type":"session_snapshot","snapshot":{"protocol":22}}'

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("ls.core.herdr_ops.transport.subprocess.run", run)
    assert machine_snapshot("box-east") == {"type": "session_snapshot", "snapshot": {"protocol": 22}}
    assert seen["argv"] == ["herdr", "--machine", "box-east", "api", "snapshot"]
    assert seen["kwargs"]["shell"] is False
