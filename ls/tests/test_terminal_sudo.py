from __future__ import annotations

import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import ls.core.terminal_ops.sudo as sudo_module
from ls.core.terminal_ops import runtime
from ls.core.terminal_ops import sudo_bridge
from ls.core.terminal_ops.credentials import (
    CredentialCandidate,
    candidate_names,
    child_environment,
    iter_candidates,
    normalize_candidate,
)
from ls.core.terminal_ops.models import PaneRequest
from ls.core.terminal_ops.sudo import (
    OperationBinding,
    payload_digest,
    run_sudo,
)


TARGET = "node-west.example"
TARGET_KEY = "NODE-WEST.EXAMPLE_SUDO_KEY"
TARGET_SECRET = "synthetic-target-secret"
GENERIC_SECRET = "synthetic-generic-secret"


def test_sudo_policy_uses_only_the_generic_variable():
    assert candidate_names(TARGET) == ("GENERIC_SUDO_KEY",)
    assert normalize_candidate(TARGET, TARGET_KEY, TARGET_SECRET) is None
    assert normalize_candidate(TARGET, "GENERIC_SUDO_KEY", GENERIC_SECRET) is not None


def test_manual_fallback_quotes_the_exact_authorized_argv():
    argv = ["/usr/bin/printf", "two words", "semi;colon", "quote' here"]
    fallback = sudo_bridge._manual_fallback(argv, "root", "wheel")
    assert shlex.split(fallback) == ["sudo", "-u", "root", "-g", "wheel", "--", *argv]


def binding_for(
    argv: list[str],
    *,
    runas_user: str | None = None,
    runas_group: str | None = None,
    validate_current=lambda: True,
    single_password_qualified: bool = False,
) -> OperationBinding:
    return OperationBinding(
        operation_id="op-7d4a",
        owner_id="owner-36ab",
        target=TARGET,
        endpoint_id="endpoint-3",
        live_owner_id="pane-owner-8",
        payload_digest=payload_digest(
            argv,
            runas_user=runas_user,
            runas_group=runas_group,
        ),
        validate_current=validate_current,
        single_password_qualified=single_password_qualified,
    )


def fake_sudo(tmp_path: Path, environment: dict[str, str]) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    executable = fake_bin / "sudo"
    executable.write_text(
        """#!""" + sys.executable + """\n
import json
import os
import subprocess
import sys
import time

args = sys.argv[1:]
with open(os.environ["FAKE_SUDO_ARGS_FILE"], "w", encoding="utf-8") as stream:
    json.dump(args, stream)
if "-A" not in args or "--" not in args:
    raise SystemExit(90)
if any(key.endswith("_SUDO_KEY") for key in os.environ):
    with open(os.environ["FAKE_SUDO_CHILD_ENV_FILE"], "w", encoding="utf-8") as stream:
        stream.write("credential-key-inherited")
command = args[args.index("--") + 1:]
time.sleep(float(os.environ.get("FAKE_SUDO_START_DELAY", "0")))
if os.environ.get("FAKE_SKIP_ASKPASS") == "1":
    os.execvpe(command[0], command, os.environ)
expected = os.environ["FAKE_EXPECTED_SECRET"].encode("utf-8")
count = int(os.environ.get("FAKE_ASKPASS_COUNT", "1"))
for attempt in range(count):
    result = subprocess.run(
        [os.environ["SUDO_ASKPASS"], "[sudo] password for test:"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=os.environ,
    )
    if result.returncode != 0:
        raise SystemExit(1)
    if result.stdout[:-1] == expected and result.stdout.endswith(b"\\n"):
        os.execvpe(command[0], command, os.environ)
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    environment["PATH"] = str(fake_bin) + os.pathsep + os.environ.get("PATH", os.defpath)
    environment["FAKE_SUDO_ARGS_FILE"] = str(tmp_path / "sudo-argv.json")
    environment["FAKE_SUDO_CHILD_ENV_FILE"] = str(tmp_path / "sudo-child-env")
    return executable


def command_writes_stdin(tmp_path: Path) -> list[str]:
    marker = tmp_path / "workload-stdin.bin"
    code = (
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
    )
    return [sys.executable, "-c", code, str(marker)]


def test_generic_candidate_ignores_target_key_and_preserves_argv_and_workload_stdin(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    incoming = tmp_path / "incoming"
    incoming.write_bytes(b"stdin remains the workload stream\n")
    output = tmp_path / "stdout"
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        TARGET_KEY: TARGET_SECRET,
        "GENERIC_SUDO_KEY": GENERIC_SECRET,
        "FAKE_EXPECTED_SECRET": GENERIC_SECRET,
    }
    runas_user = "root"
    runas_group = "wheel"
    fake_sudo(tmp_path, env)
    looked_up: list[str] = []

    def candidates(target: str, _environment):
        assert target == TARGET
        looked_up.append("generic")
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET

    events: list[dict[str, object]] = []
    with incoming.open("rb") as source, output.open("wb") as sink:
        result = run_sudo(
            binding_for(argv, runas_user=runas_user, runas_group=runas_group),
            argv,
            environment=env,
            resolver=candidates,
            runas_user=runas_user,
            runas_group=runas_group,
            stdin=source,
            stdout=sink,
            state_callback=lambda state: events.append(dict(state)),
        )

    assert result.status == "completed"
    assert result.returncode == 0
    assert looked_up == ["generic"]
    assert (tmp_path / "workload-stdin.bin").read_bytes() == b"stdin remains the workload stream\n"
    invoked = json.loads(Path(env["FAKE_SUDO_ARGS_FILE"]).read_text(encoding="utf-8"))
    assert invoked == ["-A", "-u", runas_user, "-g", runas_group, "--", *argv]
    assert not Path(env["FAKE_SUDO_CHILD_ENV_FILE"]).exists()
    assert all(TARGET_SECRET not in json.dumps(event) for event in events)
    safe = result.safe_state()
    assert TARGET_SECRET not in json.dumps(safe)
    assert safe["credential_candidates"] == [
        {"variable": "GENERIC_SUDO_KEY", "status": "provided_to_askpass"},
    ]


def test_target_key_is_ignored_and_generic_environment_value_is_authoritative():
    statuses: dict[str, str] = {}
    candidates = list(
        iter_candidates(
            TARGET,
            {
                TARGET_KEY: "bad\nvalue",
                "GENERIC_SUDO_KEY": GENERIC_SECRET,
            },
            statuses=statuses,
        )
    )
    assert [(item.variable, item.value) for item in candidates] == [
        ("GENERIC_SUDO_KEY", GENERIC_SECRET)
    ]
    assert statuses == {"GENERIC_SUDO_KEY": "available"}
    assert child_environment({TARGET_KEY: TARGET_SECRET, "OTHER_SUDO_KEY": "x", "PATH": "/bin"}) == {
        "PATH": "/bin"
    }


@pytest.mark.parametrize(("value", "status"), [("", "empty"), ("bad\nvalue", "invalid")])
def test_explicit_bad_generic_value_blocks_envman_fallback(value: str, status: str):
    calls: list[list[str]] = []

    def runner(args, **_kwargs):
        calls.append(args)
        raise AssertionError("an explicit generic value must remain authoritative")

    statuses: dict[str, str] = {}
    assert list(iter_candidates(TARGET, {"GENERIC_SUDO_KEY": value, "PATH": "/bin"},
                                envman_runner=runner, statuses=statuses)) == []
    assert statuses == {"GENERIC_SUDO_KEY": status}
    assert calls == []


def test_qualified_repeated_prompt_does_not_repeat_generic_credential(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": "synthetic-different-secret",
        "FAKE_ASKPASS_COUNT": "2",
        "FAKE_SUDO_START_DELAY": "0.1",
    }
    fake_sudo(tmp_path, env)

    def candidates(_target: str, _environment):
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET

    result = run_sudo(
        binding_for(argv, single_password_qualified=True),
        argv,
        environment=env,
        resolver=candidates,
    )

    assert result.status in {"action_required", "unknown"}
    assert [item.status for item in result.credential_attempts] == ["retry_requested"]
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_repeated_prompt_without_qualification_stops_before_generic_lookup(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": "synthetic-different-secret",
        "FAKE_ASKPASS_COUNT": "2",
        "FAKE_SUDO_START_DELAY": "0.1",
    }
    fake_sudo(tmp_path, env)
    requested: list[str] = []

    def candidates(_target: str, _environment):
        requested.append("generic")
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET

    result = run_sudo(binding_for(argv), argv, environment=env, resolver=candidates)

    assert result.status in {"unsupported_authentication", "unknown"}
    assert result.action_required is True
    assert requested == ["generic"]
    assert {item.variable: item.status for item in result.credential_attempts} == {
        "GENERIC_SUDO_KEY": "provided_to_askpass",
    }
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_empty_candidates_return_action_required_without_workload_start(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": "synthetic-different-secret",
    }
    fake_sudo(tmp_path, env)
    result = run_sudo(binding_for(argv), argv, environment=env, resolver=lambda *_: ())

    assert result.status == "action_required"
    assert result.action_required is True
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_cached_or_nopasswd_path_does_not_consult_candidates(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_SKIP_ASKPASS": "1",
        "FAKE_EXPECTED_SECRET": GENERIC_SECRET,
    }
    fake_sudo(tmp_path, env)
    looked_up: list[str] = []

    def candidates(*_args):
        looked_up.append("called")
        return ()

    result = run_sudo(binding_for(argv), argv, environment=env, resolver=candidates)

    assert result.status == "completed"
    assert looked_up == []
    assert all(item.status == "not_checked" for item in result.credential_attempts)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_nopasswd_child_exits_before_registration_without_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int,
):
    marker = tmp_path / "starts"
    argv = [
        sys.executable, "-c",
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "p.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x'); "
        "sys.exit(int(sys.argv[2]))",
        str(marker), str(exit_code),
    ]
    env = {"PATH": os.environ.get("PATH", os.defpath), "FAKE_SKIP_ASKPASS": "1"}
    fake_sudo(tmp_path, env)
    original_register = sudo_module._AskpassBroker.register_sudo
    observed_zombie = False

    def register_after_exit(broker, process):
        nonlocal observed_zombie
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                stat_line = Path(f"/proc/{process.pid}/stat").read_text()
            except FileNotFoundError:
                break
            if stat_line.rsplit(") ", 1)[1].startswith("Z "):
                observed_zombie = True
                break
            time.sleep(0.005)
        assert observed_zombie, "exact sudo child did not reach exited zombie state"
        assert original_register(broker, process) is False
        return False

    monkeypatch.setattr(sudo_module._AskpassBroker, "register_sudo", register_after_exit)
    looked_up: list[str] = []

    def candidates(*_args):
        looked_up.append("called")
        return ()

    result = run_sudo(binding_for(argv), argv, environment=env, resolver=candidates)

    assert observed_zombie
    assert result.status == ("completed" if exit_code == 0 else "failed")
    assert result.returncode == exit_code
    assert marker.read_bytes() == b"x"
    assert looked_up == []
    assert all(item.status == "not_checked" for item in result.credential_attempts)


def test_failed_registration_of_running_child_remains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    argv = [sys.executable, "-c", "import time; time.sleep(10)"]
    env = {"PATH": os.environ.get("PATH", os.defpath), "FAKE_SKIP_ASKPASS": "1"}
    fake_sudo(tmp_path, env)
    monkeypatch.setattr(sudo_module._AskpassBroker, "register_sudo", lambda *_: False)

    result = run_sudo(binding_for(argv), argv, environment=env)

    assert result.status == "unknown"
    assert result.returncode is None
    assert result.process is not None
    try:
        assert result.process.poll() is None
    finally:
        result.process.terminate()
        result.process.wait(timeout=5)


def test_payload_failure_that_looks_like_authentication_is_never_replayed(tmp_path: Path):
    marker = tmp_path / "payload-starts"
    argv = [
        sys.executable, "-c",
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "p.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x'); "
        "print('Sorry, try again.', file=sys.stderr); sys.exit(1)",
        str(marker),
    ]
    env = {"PATH": os.environ.get("PATH", os.defpath), "FAKE_EXPECTED_SECRET": GENERIC_SECRET}
    fake_sudo(tmp_path, env)
    lookups: list[str] = []

    def candidates(*_):
        lookups.append("generic")
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET

    result = run_sudo(binding_for(argv), argv, environment=env, resolver=candidates)

    assert result.returncode == 1
    assert result.status == "failed"
    assert marker.read_bytes() == b"x"
    assert lookups == ["generic"]


def test_attempt_limit_does_not_start_another_sudo_process(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": "synthetic-different-secret",
        "FAKE_ASKPASS_COUNT": "2",
        "FAKE_SUDO_START_DELAY": "0.1",
    }
    fake_sudo(tmp_path, env)
    lookups: list[str] = []

    def candidates(*_):
        lookups.append("generic")
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET
        lookups.append("target-ignored")
        yield TARGET_KEY, TARGET_SECRET

    result = run_sudo(binding_for(argv, single_password_qualified=True), argv,
                      environment=env, resolver=candidates)

    assert result.status == "blocked"
    assert result.returncode == 1
    assert lookups == ["generic", "target-ignored"]
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_command_uses_requested_directory_without_changing_controller_cwd(tmp_path: Path):
    working = tmp_path / "working"
    working.mkdir()
    argv = [sys.executable, "-c",
            "from pathlib import Path; Path('cwd-proof').write_text(str(Path.cwd()))"]
    env = {"PATH": os.environ.get("PATH", os.defpath), "FAKE_SKIP_ASKPASS": "1"}
    fake_sudo(tmp_path, env)
    original = Path.cwd()

    result = run_sudo(binding_for(argv), argv, environment=env, cwd=working)

    assert result.status == "completed"
    assert (working / "cwd-proof").read_text() == str(working)
    assert Path.cwd() == original


def test_current_owner_is_rechecked_at_askpass_before_candidate_delivery(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": GENERIC_SECRET,
    }
    fake_sudo(tmp_path, env)
    checks = 0

    def current_owner() -> bool:
        nonlocal checks
        checks += 1
        return checks < 3

    result = run_sudo(
        binding_for(argv, validate_current=current_owner),
        argv,
        environment=env,
        resolver=lambda *_: (("GENERIC_SUDO_KEY", GENERIC_SECRET),),
    )

    # Once sudo was launched, owner loss is uncertain even when the broker
    # withheld the credential. It must not be advertised as safe to retry.
    assert result.status == "unknown"
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_digest_mismatch_is_rejected_before_sudo_launch(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    fake_sudo(tmp_path, env)
    binding = binding_for([sys.executable, "-c", "pass"])

    with pytest.raises(ValueError, match="payload digest"):
        run_sudo(binding, argv, environment=env)
    assert not Path(env["FAKE_SUDO_ARGS_FILE"]).exists()


@pytest.mark.parametrize("runas", ["root;id", "../root", "user name", ""])
def test_invalid_runas_values_are_rejected(runas: str):
    with pytest.raises(ValueError, match="runas_user"):
        payload_digest(["id"], runas_user=runas)


def test_envman_lookup_is_lazy_and_receives_scrubbed_environment(tmp_path: Path):
    calls: list[list[str]] = []
    envman = tmp_path / "envman"
    envman.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    envman.chmod(0o700)

    def runner(args, **kwargs):
        calls.append(args)
        assert not any(name.endswith("_SUDO_KEY") for name in kwargs["env"])
        name = args[2]
        value = GENERIC_SECRET
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps({"variable": {"name": name, "value": value}}),
            stderr="",
        )

    env = {"PATH": str(tmp_path), TARGET_KEY: TARGET_SECRET}
    statuses: dict[str, str] = {}
    candidates = iter_candidates(TARGET, env, envman_runner=runner, statuses=statuses)
    assert next(candidates).variable == "GENERIC_SUDO_KEY"
    assert calls == [[str(envman), "get", "GENERIC_SUDO_KEY", "--json", "--reveal"]]


def test_envman_failure_has_safe_loading_error_status(tmp_path: Path):
    envman = tmp_path / "envman"
    envman.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    envman.chmod(0o700)
    statuses: dict[str, str] = {}

    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="private error")

    assert list(iter_candidates(TARGET, {"PATH": str(tmp_path)}, envman_runner=runner,
                                statuses=statuses)) == []
    assert statuses == {"GENERIC_SUDO_KEY": "loading_error"}


def test_envman_exact_missing_variable_result_is_unavailable(tmp_path: Path):
    envman = tmp_path / "envman"
    envman.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    envman.chmod(0o700)
    statuses: dict[str, str] = {}

    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(
            args, 3, stdout="", stderr="envman: GENERIC_SUDO_KEY is not managed.\n",
        )

    assert list(iter_candidates(TARGET, {"PATH": str(tmp_path)}, envman_runner=runner,
                                statuses=statuses)) == []
    assert statuses == {"GENERIC_SUDO_KEY": "unavailable"}


@pytest.mark.parametrize(
    "returncode,stderr",
    [
        (1, "envman: storage is locked.\n"),
        (3, "envman: the environment store is unhealthy.\n"),
    ],
)
def test_envman_non_missing_failures_remain_loading_errors(tmp_path: Path, returncode: int, stderr: str):
    envman = tmp_path / "envman"
    envman.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    envman.chmod(0o700)
    statuses: dict[str, str] = {}

    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout="", stderr=stderr)

    assert list(iter_candidates(TARGET, {"PATH": str(tmp_path)}, envman_runner=runner,
                                statuses=statuses)) == []
    assert statuses == {"GENERIC_SUDO_KEY": "loading_error"}


def test_managed_broker_rechecks_owner_after_slow_credential_resolution(tmp_path: Path, monkeypatch):
    request = PaneRequest("tmux", str(tmp_path / "tmux.sock"), "project", "controller",
                          "%0", str(tmp_path), session_id="$1")
    operation_id = "owner-change-after-lookup"
    ops_root = tmp_path / "terminal_ops"
    ops_root.mkdir(mode=0o700)
    ops_root.chmod(0o700)
    directory = tmp_path / "terminal_ops" / request.binding_key
    directory.mkdir(mode=0o700)
    operation_path = directory / f"{operation_id}.json"
    shell_pid = os.getppid()
    monkeypatch.setattr(runtime, "_read_operation", lambda *_: (
        operation_path,
        {"binding_key": request.binding_key, "operation_id": operation_id,
         "state": "running", "shell_pid": shell_pid},
    ))
    lookup_started = threading.Event()
    release_lookup = threading.Event()
    authentication_requested = threading.Event()
    owner_current = [True]
    statuses: dict[str, str] = {}

    def candidates():
        lookup_started.set()
        assert release_lookup.wait(timeout=3)
        yield CredentialCandidate("GENERIC_SUDO_KEY", GENERIC_SECRET)

    with runtime._credential_server(
        tmp_path, request, operation_id, shell_pid, candidates,
        lambda: owner_current[0], time.monotonic() + 5, statuses, authentication_requested,
    ) as socket_path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(socket_path)
            encoded_id = operation_id.encode("ascii")
            connection.sendall(struct.pack("!I", len(encoded_id)) + encoded_id)
            assert lookup_started.wait(timeout=3)
            owner_current[0] = False
            release_lookup.set()
            assert connection.recv(4) == b""

    assert not (directory / f"{operation_id}.sudo-lookup.json").exists()


def test_stopped_broker_does_not_validate_owner_after_credential_resolution(tmp_path: Path, monkeypatch):
    request = PaneRequest("tmux", str(tmp_path / "tmux.sock"), "project", "controller",
                          "%0", str(tmp_path), session_id="$1")
    operation_id = "stop-during-lookup"
    directory = tmp_path / "terminal_ops" / request.binding_key
    directory.mkdir(parents=True, mode=0o700)
    operation_path = directory / f"{operation_id}.json"
    shell_pid = os.getppid()
    monkeypatch.setattr(runtime, "_read_operation", lambda *_: (
        operation_path,
        {"binding_key": request.binding_key, "operation_id": operation_id,
         "state": "running", "shell_pid": shell_pid},
    ))
    lookup_started = threading.Event()
    release_lookup = threading.Event()
    authentication_requested = threading.Event()
    validation_calls = []

    def validate_current() -> bool:
        validation_calls.append(1)
        return True

    def candidates():
        lookup_started.set()
        assert release_lookup.wait(timeout=3)
        yield CredentialCandidate("GENERIC_SUDO_KEY", GENERIC_SECRET)

    existing_threads = set(threading.enumerate())
    with runtime._credential_server(
        tmp_path, request, operation_id, shell_pid, candidates,
        validate_current, time.monotonic() + 5, {}, authentication_requested,
    ) as socket_path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(socket_path)
            encoded_id = operation_id.encode("ascii")
            connection.sendall(struct.pack("!I", len(encoded_id)) + encoded_id)
            assert lookup_started.wait(timeout=3)
            threading.Timer(0.1, release_lookup.set).start()

    assert len(validation_calls) == 2
    assert not any(thread.name == "managed-sudo-candidates" and thread.is_alive()
                   for thread in set(threading.enumerate()) - existing_threads)
    assert not (directory / f"{operation_id}.sudo-lookup.json").exists()


def test_managed_broker_rechecks_deadline_after_slow_owner_validation(tmp_path: Path):
    tmp_path.chmod(0o700)
    request = PaneRequest("tmux", str(tmp_path / "tmux.sock"), "project", "controller",
                          "%0", str(tmp_path), session_id="$1")
    operation_id = "expiry-during-owner-check"
    ops_root = tmp_path / "terminal_ops"
    ops_root.mkdir(mode=0o700)
    ops_root.chmod(0o700)
    directory = ops_root / request.binding_key
    directory.mkdir(mode=0o700)
    workload = command_writes_stdin(tmp_path)
    operation_path = directory / f"{operation_id}.json"
    operation_path.write_text(json.dumps({
        "binding_key": request.binding_key,
        "operation_id": operation_id,
        "state": "running",
        "shell_pid": os.getpid(),
        "target": TARGET,
        "endpoint": request.endpoint,
        "controller_id": request.controller_id,
        "working_directory": str(tmp_path),
    }), encoding="utf-8")
    operation_path.chmod(0o600)

    env = {"PATH": os.environ.get("PATH", os.defpath), "FAKE_EXPECTED_SECRET": GENERIC_SECRET}
    fake_sudo(tmp_path, env)
    owner_validation_started = threading.Event()
    release_owner_validation = threading.Event()
    authentication_requested = threading.Event()
    validation_calls = 0

    def validate_current() -> bool:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 3:
            owner_validation_started.set()
            assert release_owner_validation.wait(timeout=4)
        return True

    def candidates():
        yield CredentialCandidate("GENERIC_SUDO_KEY", GENERIC_SECRET)

    deadline = time.monotonic() + 1.0
    with runtime._credential_server(
        tmp_path, request, operation_id, os.getpid(), candidates,
        validate_current, deadline, {}, authentication_requested,
    ) as socket_path:
        bridge_env = dict(env)
        bridge_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        bridge = subprocess.Popen(
            [sys.executable, "-m", "ls.core.terminal_ops.sudo_bridge",
             "--state-dir", str(tmp_path), "--binding-key", request.binding_key,
             "--operation-id", operation_id, "--socket", socket_path,
             "--", *workload],
            cwd=Path(__file__).resolve().parents[2], env=bridge_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert owner_validation_started.wait(timeout=5)
        while time.monotonic() < deadline:
            time.sleep(0.005)
        release_owner_validation.set()
        stdout, stderr = bridge.communicate(timeout=8)

    assert bridge.returncode != 0, (stdout, stderr)
    assert authentication_requested.is_set()
    assert not (tmp_path / "workload-stdin.bin").exists()
    lookup = json.loads((directory / f"{operation_id}.sudo-lookup.json").read_text(encoding="utf-8"))
    assert lookup == {
        "binding_key": request.binding_key,
        "operation_id": operation_id,
        "credential_lookup_status": "expired",
    }
    assert GENERIC_SECRET not in json.dumps(lookup)


def test_credential_response_reader_accepts_fragmented_frames(tmp_path: Path):
    tmp_path.chmod(0o700)
    socket_path = str(tmp_path / "credential.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    os.chmod(socket_path, 0o600)
    server.listen(1)
    errors: list[BaseException] = []

    def respond():
        try:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(2)
                header = bytearray()
                while len(header) < 4:
                    header.extend(connection.recv(4 - len(header)))
                size = struct.unpack("!I", header)[0]
                request = bytearray()
                while len(request) < size:
                    request.extend(connection.recv(size - len(request)))
                assert request == b"operation-1"
                payload = b"GENERIC_SUDO_KEY\0fragmented-secret"
                framed = struct.pack("!I", len(payload)) + payload
                for byte in framed:
                    connection.sendall(bytes([byte]))
                    time.sleep(0.001)
        except BaseException as exc:
            errors.append(exc)
        finally:
            server.close()

    thread = threading.Thread(target=respond)
    thread.start()
    try:
        candidate = sudo_bridge._receive_candidate(socket_path, "operation-1")
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert errors == []
    assert candidate == ("GENERIC_SUDO_KEY", "fragmented-secret")


