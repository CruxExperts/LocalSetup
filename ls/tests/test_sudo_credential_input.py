from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ls.core.terminal_ops.dialog_channel import ChannelClosed, send_frame
from ls.core.terminal_ops.command_state import process_info
from ls.core.terminal_ops import askpass as askpass_module
import ls.core.terminal_ops.sudo as sudo_module
from ls.core.terminal_ops.sudo import run_sudo
from ls.core.terminal_ops.sudo_channel import (
    CredentialReply,
    receive_reply,
    receive_request,
    send_reply,
    send_request,
)
from ls.core.terminal_ops.sudo_bridge import _private_input_reply
from ls.core.terminal_ops.sudo_bridge import _controller_current

from test_terminal_sudo import (
    GENERIC_SECRET,
    binding_for,
    command_writes_stdin,
    fake_sudo,
)


def test_private_channel_round_trip_keeps_secret_mutable_and_disposable():
    client, server = socket.socketpair()
    request_error: list[BaseException] = []

    def send_request_and_reply() -> None:
        try:
            send_request(client, "op-1", "manual", 2, "a" * 64, lambda: True)
            send_reply(client, "available", "manual", bytearray(b"private-key"), lambda: True)
        except BaseException as exc:
            request_error.append(exc)

    worker = threading.Thread(target=send_request_and_reply)
    worker.start()
    try:
        request = receive_request(server, lambda: True)
        reply = receive_reply(server, lambda: True)
    finally:
        worker.join(timeout=2)
        client.close()
        server.close()

    assert not worker.is_alive()
    assert request_error == []
    assert request == {
        "version": 1,
        "operation_id": "op-1",
        "kind": "manual",
        "ordinal": 2,
        "payload_digest": "a" * 64,
    }
    assert reply.status == "available" and reply.source == "manual"
    assert isinstance(reply.secret, bytearray) and reply.secret == b"private-key"
    assert "private-key" not in repr(reply)
    reply.dispose()
    assert reply.secret is None


@pytest.mark.parametrize("secret", [b"", b"a\0b", b"a\nb", b"a\rb", b"\xff"])
def test_private_channel_rejects_invalid_secret_values(secret: bytes):
    left, right = socket.socketpair()
    try:
        with pytest.raises(ChannelClosed):
            send_reply(left, "available", "manual", secret, lambda: True)
    finally:
        left.close()
        right.close()


def test_credential_reply_requires_mutable_secret_ownership():
    with pytest.raises(ChannelClosed):
        CredentialReply("available", "manual", b"immutable")


def test_askpass_read_polls_only_when_deadline_current_check_is_supplied():
    class TimeoutOnce:
        def __init__(self):
            self.calls = 0

        def recv(self, _size):
            self.calls += 1
            if self.calls == 1:
                raise socket.timeout()
            return b"x"

    legacy = TimeoutOnce()
    with pytest.raises(socket.timeout):
        askpass_module._read_exact(legacy, 1)

    deadline_bound = TimeoutOnce()
    assert askpass_module._read_exact(deadline_bound, 1, lambda: True) == b"x"


def test_private_request_rejects_extra_fields():
    sender, receiver = socket.socketpair()
    payload = json.dumps({
        "version": 1,
        "operation_id": "op-1",
        "kind": "automatic",
        "ordinal": 1,
        "payload_digest": "b" * 64,
        "secret": "must-not-be-present",
    }).encode()
    worker = threading.Thread(target=send_frame, args=(sender, payload, lambda: True))
    worker.start()
    try:
        with pytest.raises(ChannelClosed):
            receive_request(receiver, lambda: True)
    finally:
        worker.join(timeout=2)
        sender.close()
        receiver.close()


def test_bridge_private_request_checks_controller_generation_and_receives_secret(tmp_path):
    tmp_path.chmod(0o700)
    socket_path = str(tmp_path / "controller.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(socket_path)
    os.chmod(socket_path, 0o600)
    listener.listen(1)
    requests: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(0.2)
                requests.append(receive_request(connection, lambda: True))
                send_reply(connection, "available", "generic", bytearray(b"generic-secret"), lambda: True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    identity = process_info(os.getpid())
    assert identity is not None
    reply = _private_input_reply(
        socket_path, "op-bridge", "automatic", 1, "c" * 64,
        lambda: True, time.monotonic() + 3,
        os.getpid(), identity["starttime"],
    )
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert errors == []
    assert requests == [{
        "version": 1,
        "operation_id": "op-bridge",
        "kind": "automatic",
        "ordinal": 1,
        "payload_digest": "c" * 64,
    }]
    assert reply.status == "available" and reply.source == "generic"
    assert reply.secret == b"generic-secret"
    reply.dispose()


def test_askpass_discards_received_secret_if_controller_exits_before_stdout(monkeypatch):
    controller = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    identity = process_info(controller.pid)
    assert identity is not None
    payload = b"controller-exit-canary"
    response = [b"O", len(payload).to_bytes(4, "big"), payload]
    captured_buffers: list[bytearray] = []
    writes: list[bytes] = []

    class TrackedBytearray(bytearray):
        def __new__(cls, *args, **kwargs):
            value = super().__new__(cls)
            captured_buffers.append(value)
            return value

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

    class BrokerSocket:
        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def send(self, value):
            return len(value)

        def recv(self, size):
            value = response.pop(0)
            assert len(value) <= size
            if not response:
                controller.terminate()
                controller.wait(timeout=3)
            return value

        def close(self):
            pass

    env = {
        "LS_TERMINAL_SUDO_SOCKET": "/tmp/fake-private-askpass.sock",
        "LS_TERMINAL_SUDO_NONCE": "a" * 32,
        "LS_TERMINAL_SUDO_OPERATION_ID": "op-askpass-final-hop",
        "LS_TERMINAL_SUDO_OWNER_ID": "owner",
        "LS_TERMINAL_SUDO_TARGET": "GENERIC",
        "LS_TERMINAL_SUDO_ENDPOINT_ID": "endpoint",
        "LS_TERMINAL_SUDO_LIVE_OWNER_ID": "live-owner",
        "LS_TERMINAL_SUDO_PAYLOAD_DIGEST": "b" * 64,
        "LS_TERMINAL_SUDO_CONTROLLER_PID": str(controller.pid),
        "LS_TERMINAL_SUDO_CONTROLLER_STARTTIME": str(identity["starttime"]),
    }
    try:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(askpass_module, "bytearray", TrackedBytearray, raising=False)
        monkeypatch.setattr(askpass_module.socket, "socket", lambda *_args: BrokerSocket())
        monkeypatch.setattr(
            askpass_module, "_write_secret",
            lambda value, _current=None: writes.append(bytes(value)),
        )

        assert askpass_module.main() == 1
        assert writes == []
        secret_buffers = [value for value in captured_buffers if len(value) == len(payload)]
        assert len(secret_buffers) >= 2
        assert all(not any(value) for value in secret_buffers)
    finally:
        if controller.poll() is None:
            controller.terminate()
        controller.wait(timeout=3)


def test_sudo_registration_retries_transient_missing_proc_sample(monkeypatch):
    binding = binding_for(["true"])
    broker = sudo_module._AskpassBroker(
        binding,
        {},
        resolver=None,
        envman_runner=None,
        state_callback=None,
        manual_provider=None,
        authentication_deadline=None,
        validate_input_current=None,
        controller_pid=None,
        controller_starttime=None,
    )
    parent_pid = os.getpid()
    identity_reads = 0
    command_reads = 0

    def read_identity(_pid):
        nonlocal identity_reads
        identity_reads += 1
        return None if identity_reads == 1 else (parent_pid, 12345)

    def read_command(_pid):
        nonlocal command_reads
        command_reads += 1
        return None if command_reads == 1 else (b"/usr/bin/sudo", b"--", b"true")

    monkeypatch.setattr(sudo_module, "_proc_identity", read_identity)
    monkeypatch.setattr(sudo_module, "_proc_cmdline", read_command)
    process = SimpleNamespace(pid=987654, poll=lambda: None)

    assert broker.register_sudo(process) is True
    assert broker.sudo_pid == process.pid
    assert broker.sudo_start_time == 12345
    assert identity_reads == 3 and command_reads == 2


def _sudo_env(tmp_path, expected: str, *, count: int = 1) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": expected,
        "FAKE_ASKPASS_COUNT": str(count),
        "FAKE_SUDO_START_DELAY": "0.1",
    }
    fake_sudo(tmp_path, env)
    return env


def test_missing_automatic_candidate_uses_one_manual_value_in_same_sudo_run(tmp_path):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, "manual-value")
    entered = bytearray(b"manual-value")
    returned = CredentialReply("available", "manual", entered)
    attempts: list[int] = []

    def manual(attempt, current, deadline):
        attempts.append(attempt)
        assert current() is True
        assert deadline > time.monotonic()
        return returned

    controller = process_info(os.getpid())
    assert controller is not None

    result = run_sudo(
        binding_for(argv), argv, environment=env, resolver=lambda *_: (),
        manual_provider=manual, authentication_deadline=time.monotonic() + 3,
        controller_pid=os.getpid(), controller_starttime=controller["starttime"],
    )

    assert result.status == "completed"
    assert attempts == [1]
    assert returned.secret is None
    assert result.safe_state()["manual_authentication"] == "provided_to_askpass"
    assert "manual-value" not in json.dumps(result.safe_state())
    assert (tmp_path / "workload-stdin.bin").exists()


@pytest.mark.parametrize("source", ["generic", "manual"])
def test_controller_exit_after_reply_prevents_forwarding_input(tmp_path, source):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, "late-private-input")
    controller = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    identity = process_info(controller.pid)
    assert identity is not None
    entered = bytearray(b"late-private-input")
    requests: list[str] = []

    def received_reply(*_args):
        requests.append(source)
        reply = CredentialReply("available", source, entered)
        controller.terminate()
        controller.wait(timeout=3)
        return reply

    try:
        result = run_sudo(
            binding_for(argv), argv, environment=env,
            resolver=(lambda *_: iter((received_reply(),))) if source == "generic" else lambda *_: (),
            manual_provider=received_reply if source == "manual" else None,
            validate_input_current=lambda: _controller_current(controller.pid, identity["starttime"]),
        )
        assert requests == [source], "controller exit happened before the target credential callback"
        assert result.status == "blocked"
        assert not (tmp_path / "workload-stdin.bin").exists()
        assert not entered or not any(entered)
    finally:
        if controller.poll() is None:
            controller.terminate()
        controller.wait(timeout=3)


def test_input_owner_exit_keeps_monitoring_started_workload(tmp_path):
    marker = tmp_path / "started"
    argv = [sys.executable, "-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(.3)", str(marker)]
    env = _sudo_env(tmp_path, GENERIC_SECRET)
    controller = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    identity = process_info(controller.pid)
    assert identity is not None

    def end_owner_after_start():
        end = time.monotonic() + 4
        while not marker.exists() and time.monotonic() < end:
            time.sleep(.01)
        controller.terminate()
        controller.wait(timeout=3)

    watcher = threading.Thread(target=end_owner_after_start)
    watcher.start()
    try:
        result = run_sudo(binding_for(argv), argv, environment=env,
                          resolver=lambda *_: (("GENERIC_SUDO_KEY", GENERIC_SECRET),),
                          validate_input_current=lambda: _controller_current(controller.pid, identity["starttime"]))
        watcher.join(timeout=5)
        assert marker.exists() and not watcher.is_alive()
        assert controller.poll() is not None
        assert result.status == "completed" and result.returncode == 0
    finally:
        if controller.poll() is None:
            controller.terminate()
        watcher.join(timeout=5)
        controller.wait(timeout=3)


def test_qualified_rejected_automatic_candidate_allows_one_manual_retry(tmp_path):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, "manual-value", count=2)
    attempts: list[int] = []

    def manual(attempt, current, _deadline):
        attempts.append(attempt)
        assert current() is True
        return CredentialReply("available", "manual", bytearray(b"manual-value"))

    result = run_sudo(
        binding_for(argv, single_password_qualified=True), argv,
        environment=env,
        resolver=lambda *_: [("GENERIC_SUDO_KEY", "rejected-auto")],
        manual_provider=manual,
        authentication_deadline=time.monotonic() + 3,
    )

    assert result.status == "completed"
    assert attempts == [2]
    assert [item.status for item in result.credential_attempts] == ["retry_requested"]
    assert result.safe_state()["manual_authentication"] == "provided_to_askpass"


def test_unqualified_rejected_automatic_candidate_does_not_open_manual_input(tmp_path):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, "manual-value", count=2)
    calls: list[int] = []

    def manual(attempt, _current, _deadline):
        calls.append(attempt)
        return CredentialReply("available", "manual", bytearray(b"manual-value"))

    result = run_sudo(
        binding_for(argv), argv, environment=env,
        resolver=lambda *_: [("GENERIC_SUDO_KEY", "rejected-auto")],
        manual_provider=manual, authentication_deadline=time.monotonic() + 3,
    )

    assert result.status in {"unsupported_authentication", "unknown"}
    assert calls == []
    assert "manual_authentication" not in result.safe_state()
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_loading_error_blocks_before_manual_provider_and_keeps_receipt_secret_free(tmp_path):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, GENERIC_SECRET)
    (tmp_path / "bin" / "envman").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (tmp_path / "bin" / "envman").chmod(0o700)
    calls: list[int] = []

    def manual(attempt, _current, _deadline):
        calls.append(attempt)
        return CredentialReply("available", "manual", bytearray(b"should-not-appear"))

    result = run_sudo(
        binding_for(argv), argv, environment=env,
        envman_runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "private diagnostic"),
        manual_provider=manual, authentication_deadline=time.monotonic() + 3,
    )

    safe = result.safe_state()
    assert result.status == "blocked"
    assert calls == []
    assert safe["credential_candidates"] == [
        {"variable": "GENERIC_SUDO_KEY", "status": "loading_error"},
    ]
    assert "should-not-appear" not in json.dumps(safe)
    assert "private diagnostic" not in json.dumps(safe)
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_manual_cancellation_has_no_retry_instruction_or_workload_start(tmp_path):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, "not-used")

    result = run_sudo(
        binding_for(argv), argv, environment=env, resolver=lambda *_: (),
        manual_provider=lambda *_: CredentialReply("cancelled", "none"),
        authentication_deadline=time.monotonic() + 3,
    )

    safe = result.safe_state()
    assert result.status == "blocked"
    assert safe["manual_authentication"] == "cancelled"
    assert "manual_fallback" not in safe
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_manual_expiry_is_recorded_without_retry_advice(tmp_path):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, "not-used")

    result = run_sudo(
        binding_for(argv), argv, environment=env, resolver=lambda *_: (),
        manual_provider=lambda *_: CredentialReply("expired", "none"),
        authentication_deadline=time.monotonic() + 3,
    )

    safe = result.safe_state()
    assert result.status == "blocked"
    assert safe["manual_authentication"] == "expired"
    assert "manual_fallback" not in safe
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_owner_loss_during_manual_wait_releases_broker_lock_and_stops_cleanly(tmp_path, monkeypatch):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, "not-used")
    owner_live = threading.Event()
    owner_live.set()
    prompt_started = threading.Event()
    brokers = []
    original_init = sudo_module._AskpassBroker.__init__

    def capture_broker(broker, *args, **kwargs):
        original_init(broker, *args, **kwargs)
        brokers.append(broker)

    monkeypatch.setattr(sudo_module._AskpassBroker, "__init__", capture_broker)

    def manual(_attempt, current, _deadline):
        prompt_started.set()
        while current():
            time.sleep(0.01)
        return CredentialReply("cancelled", "none")

    results = []
    worker = threading.Thread(target=lambda: results.append(run_sudo(
        binding_for(argv, validate_current=owner_live.is_set), argv,
        environment=env, resolver=lambda *_: (), manual_provider=manual,
        authentication_deadline=time.monotonic() + 4,
    )))
    worker.start()
    assert prompt_started.wait(timeout=3)
    assert brokers
    broker = brokers[0]
    assert broker.lock.acquire(timeout=0.5), "broker lock stayed held during manual wait"
    broker.lock.release()
    owner_live.clear()
    worker.join(timeout=4)

    assert not worker.is_alive(), "owner loss left the manual prompt holding the operation"
    assert len(results) == 1 and results[0].status == "unknown"
    assert "not-used" not in json.dumps(results[0].safe_state())
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_deadline_aware_manual_wait_can_exceed_short_socket_poll_window(tmp_path):
    argv = command_writes_stdin(tmp_path)
    env = _sudo_env(tmp_path, "delayed-manual")
    started = time.monotonic()

    def manual(attempt, current, deadline):
        assert attempt == 1 and deadline > time.monotonic()
        time.sleep(0.35)
        assert current() is True
        return CredentialReply("available", "manual", bytearray(b"delayed-manual"))

    result = run_sudo(
        binding_for(argv), argv, environment=env, resolver=lambda *_: (),
        manual_provider=manual, authentication_deadline=time.monotonic() + 3,
    )

    assert time.monotonic() - started > 0.3
    assert result.status == "completed"
    assert result.safe_state()["manual_authentication"] == "provided_to_askpass"
