from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from ls.core.terminal_ops import remote_diagnostic as diagnostic
from ls.core.terminal_ops.models import PaneBinding, PaneStatus
from ls.core.terminal_ops.remote_diagnostic_runtime import REMOTE_DIAGNOSTIC
from ls.core.terminal_ops.remote_diagnostic_transport import canonical
from ls.tests.test_terminal_sudo import fake_sudo


@pytest.fixture
def local_remote(tmp_path, monkeypatch):
    """Execute the real staged receiver and askpass protocol, without SSH/root."""
    environment = dict(os.environ)
    environment.pop("GENERIC_SUDO_KEY", None)
    environment["XDG_STATE_HOME"] = str(tmp_path / "remote-state")
    environment["FAKE_EXPECTED_SECRET"] = "private-diagnostic-fixture"
    fake_sudo(tmp_path, environment)
    owner = {"uid": os.getuid(), "pid": os.getpid(), "start_token": "fixture", "boot_id": "fixture"}
    monkeypatch.setattr(diagnostic, "_identity", lambda *args: dict(owner))

    class LocalTransport:
        def __init__(self, target):
            self.target = target
            self.settings = {"hostname": target}

        def command(self, target, argv):
            assert target == self.target
            return [sys.executable, *argv[1:]]

        def read(self, argv, *, stdin=None):
            result = subprocess.run(self.command(self.target, argv), input=stdin,
                capture_output=True, env=environment, timeout=20)
            if result.returncode:
                raise ValueError("receiver failed: " + result.stderr.decode())
            return json.loads(result.stdout)

        def endpoint_digest(self):
            return hashlib.sha256(canonical(self.settings)).hexdigest()

    class LocalPane:
        def __init__(self):
            self.process = None
            self.operation_id = None
            self.binding = None
            self.dispatches = 0

        def ensure(self, request):
            self.binding = PaneBinding(name=request.name, binding_key=request.binding_key,
                backend="herdr", endpoint=request.endpoint, parent_id=request.parent_id,
                pane_id="child", repository=request.repository, controller_id=request.controller_id,
                shell_pid=os.getpid(), workspace_id=request.workspace_id)
            return PaneStatus("ready", "herdr", request.binding_key, request.name, self.binding)

        def transition(self, request, transition):
            self.operation_id = transition.operation_id
            self.dispatches += 1
            self.process = subprocess.Popen(transition.argv, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert self.process.stdin is not None
            self.process.stdin.write(b"original workload input")
            self.process.stdin.close()
            return self.inspect(request)

        def inspect(self, request):
            return PaneStatus("busy", "herdr", request.binding_key, request.name,
                              self.binding, self.operation_id)

        def reconcile(self, request, operation_id):
            assert operation_id == self.operation_id
            assert self.process is not None
            self.process.wait(timeout=10)
            return self.inspect(request)

    pane = LocalPane()
    monkeypatch.setattr(diagnostic, "DiagnosticSSH", LocalTransport)
    monkeypatch.setattr(diagnostic, "_service", lambda root: pane)
    arguments = dict(endpoint="/fixture/socket", workspace="workspace", parent="parent",
        cwd=str(tmp_path), target="remote.example", operation_id=uuid.uuid4().hex,
        state_dir=str(tmp_path / "controller"), timeout=20,
        environment={"GENERIC_SUDO_KEY": environment["FAKE_EXPECTED_SECRET"]})
    yield arguments, pane, environment
    if pane.process is not None:
        if pane.process.poll() is None:
            pane.process.kill()
        pane.process.wait(timeout=10)
        if pane.process.stdout is not None:
            pane.process.stdout.close()
        if pane.process.stderr is not None:
            pane.process.stderr.close()


@pytest.mark.parametrize("returncode", [0, 7])
def test_exact_remote_payload_preserves_stdin_and_never_replays(local_remote, tmp_path, returncode):
    arguments, pane, environment = local_remote
    marker = tmp_path / "payload-executions"
    code = ("import os,sys;from pathlib import Path;"
            "p=Path(sys.argv[1]);p.write_text(p.read_text()+'x' if p.exists() else 'x');"
            "assert not any(k.endswith('_SUDO_KEY') for k in os.environ);"
            "print(sys.stdin.read());raise SystemExit(int(sys.argv[2]))")
    record = diagnostic.run_diagnostic(**arguments, argv=[sys.executable, "-c", code, str(marker), str(returncode)])
    result = diagnostic.public_result(record)
    assert result["state"] == "completed"
    assert result["exit_code"] == returncode
    assert result["stdout"] == "original workload input\n"
    assert result["sudo"]["credential_candidates"] == [{"variable": "GENERIC_SUDO_KEY", "status": "provided_to_askpass"}]
    assert result["sudo"]["status"] == ("completed" if returncode == 0 else "failed")
    assert marker.read_text() == "x"
    with pytest.raises(ValueError, match="already exists"):
        diagnostic.run_diagnostic(**arguments, argv=[sys.executable, "-c", code, str(marker), str(returncode)])
    assert pane.dispatches == 1
    assert marker.read_text() == "x"
    observed = diagnostic.status_diagnostic(state_dir=arguments["state_dir"],
        binding_key=record["binding_key"], operation_id=arguments["operation_id"])
    assert diagnostic.public_result(observed) == result
    secret = environment["FAKE_EXPECTED_SECRET"].encode()
    assert secret not in json.dumps(record).encode()
    for path in Path(record["remote_root"]).rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes()


def test_native_owner_refusal_precedes_any_remote_staging(tmp_path, monkeypatch):
    def refuse(*args):
        raise ValueError("native controller not current")
    monkeypatch.setattr(diagnostic, "_identity", refuse)
    with pytest.raises(ValueError, match="native controller"):
        diagnostic.run_diagnostic(endpoint="/socket", workspace="w", parent="p", cwd=str(tmp_path),
            target="remote.example", argv=["true"], operation_id=uuid.uuid4().hex,
            state_dir=str(tmp_path / "state"))
    assert not (tmp_path / "state").exists()


def test_receiver_rejects_changed_claim_without_running_payload(local_remote, tmp_path):
    arguments, pane, environment = local_remote
    marker = tmp_path / "marker"
    record = diagnostic.run_diagnostic(**arguments, argv=[sys.executable, "-c", "print('safe')"])
    claim_path = Path(record["remote_root"]) / "claim.json"
    claim = json.loads(claim_path.read_text())
    claim["argv"] = [sys.executable, "-c", "from pathlib import Path;Path(" + repr(str(marker)) + ").touch()"]
    claim_path.write_bytes(canonical(claim))
    result = subprocess.run([sys.executable, "-c", "exec(" + repr(REMOTE_DIAGNOSTIC) + ")",
        record["remote_root"], "run", record["claim_sha256"]],
        capture_output=True, env=environment, timeout=10)
    assert result.returncode != 0
    assert not marker.exists()
    assert pane.dispatches == 1


@pytest.mark.parametrize("change", ["owner", "deadline"])
def test_private_sender_rechecks_authority_after_transport_preparation(tmp_path, monkeypatch, change):
    from types import SimpleNamespace
    from ls.core.terminal_ops import remote_sudo_channel as channel

    now = [10.0]
    live = [True]
    marker = tmp_path / "must-not-execute"
    monkeypatch.setattr(channel, "time", SimpleNamespace(monotonic=lambda: now[0]))

    class Transport:
        def command(self, target, argv):
            if change == "owner":
                live[0] = False
            else:
                now[0] = 30.0
            return [sys.executable, "-c", "from pathlib import Path;Path(" + repr(str(marker)) + ").touch()"]

    delivered = channel.deliver_candidate(Transport(), "fixture", "a" * 32,
        "synthetic-secret", environment={}, deadline=20.0, validate_current=lambda: live[0])
    assert delivered is False
    assert not marker.exists()


def test_owner_loss_during_credential_resolution_never_delivers(local_remote, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ls.core.herdr_ops.errors import HerdrOpsError

    arguments, pane, _environment = local_remote
    original_owner = diagnostic._identity("fixture", "workspace", "parent", str(tmp_path))
    live = [True]

    def identity(*_args):
        if not live[0]:
            raise ValueError("native controller exited")
        return original_owner

    def candidates(*_args):
        live[0] = False
        yield SimpleNamespace(variable="GENERIC_SUDO_KEY", value="synthetic-secret")

    def transition(_request, value):
        pane.operation_id = value.operation_id
        pane.dispatches += 1

    monkeypatch.setattr(diagnostic, "_identity", identity)
    monkeypatch.setattr(diagnostic, "iter_candidates", candidates)
    monkeypatch.setattr(pane, "transition", transition)
    monkeypatch.setattr(diagnostic, "_observe", lambda _transport, record: {
        "challenge": "GENERIC_SUDO_KEY", "wrapper_current": True,
        "claim_sha256": record["claim_sha256"]})
    with pytest.raises(HerdrOpsError, match="do not replay"):
        diagnostic.run_diagnostic(**arguments, argv=["true"])
    assert pane.dispatches == 1
    assert not (tmp_path / "sudo-argv.json").exists()
    records = list(Path(arguments["state_dir"]).rglob("*.remote.json"))
    assert json.loads(records[0].read_text())["state"] == "unknown"


def test_recent_output_is_bounded_and_keeps_final_lines(local_remote):
    arguments, _pane, _environment = local_remote
    code = "import sys;sys.stdout.buffer.write(b'prefix\\n'*10000+b'last-line\\n')"
    result = diagnostic.public_result(diagnostic.run_diagnostic(**arguments,
        argv=[sys.executable, "-c", code]))
    assert result["stdout"].endswith("last-line\n")
    assert len(result["stdout"].splitlines()) == 100
    assert len(result["stdout"].encode()) <= 16384
    assert result["stdout_truncated"] is True
