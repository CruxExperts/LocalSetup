from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ls.core.terminal_ops.sudo as sudo_module
from ls.core.terminal_ops.credentials import child_environment, iter_candidates
from ls.core.terminal_ops.sudo import (
    OperationBinding,
    payload_digest,
    run_sudo,
)


TARGET = "node-west.example"
TARGET_KEY = "NODE-WEST.EXAMPLE_SUDO_KEY"
TARGET_SECRET = "synthetic-target-secret"
GENERIC_SECRET = "synthetic-generic-secret"


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

args = sys.argv[1:]
with open(os.environ["FAKE_SUDO_ARGS_FILE"], "w", encoding="utf-8") as stream:
    json.dump(args, stream)
if "-A" not in args or "--" not in args:
    raise SystemExit(90)
if any(key.endswith("_SUDO_KEY") for key in os.environ):
    with open(os.environ["FAKE_SUDO_CHILD_ENV_FILE"], "w", encoding="utf-8") as stream:
        stream.write("credential-key-inherited")
command = args[args.index("--") + 1:]
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


def test_target_candidate_runs_once_and_preserves_argv_and_workload_stdin(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    incoming = tmp_path / "incoming"
    incoming.write_bytes(b"stdin remains the workload stream\n")
    output = tmp_path / "stdout"
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        TARGET_KEY: TARGET_SECRET,
        "GENERIC_SUDO_KEY": GENERIC_SECRET,
        "FAKE_EXPECTED_SECRET": TARGET_SECRET,
    }
    runas_user = "root"
    runas_group = "wheel"
    fake_sudo(tmp_path, env)
    looked_up: list[str] = []

    def candidates(target: str, _environment):
        assert target == TARGET
        looked_up.append("target")
        yield TARGET_KEY, TARGET_SECRET
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
    assert looked_up == ["target"]
    assert (tmp_path / "workload-stdin.bin").read_bytes() == b"stdin remains the workload stream\n"
    invoked = json.loads(Path(env["FAKE_SUDO_ARGS_FILE"]).read_text(encoding="utf-8"))
    assert invoked == ["-A", "-u", runas_user, "-g", runas_group, "--", *argv]
    assert not Path(env["FAKE_SUDO_CHILD_ENV_FILE"]).exists()
    assert all(TARGET_SECRET not in json.dumps(event) for event in events)
    safe = result.safe_state()
    assert TARGET_SECRET not in json.dumps(safe)
    assert safe["credential_candidates"] == [
        {"variable": TARGET_KEY, "status": "provided_to_askpass"},
        {"variable": "GENERIC_SUDO_KEY", "status": "not_checked"},
    ]


def test_missing_or_malformed_target_uses_generic_first():
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
    assert statuses == {TARGET_KEY: "invalid", "GENERIC_SUDO_KEY": "available"}
    assert child_environment({TARGET_KEY: TARGET_SECRET, "OTHER_SUDO_KEY": "x", "PATH": "/bin"}) == {
        "PATH": "/bin"
    }


def test_qualified_second_askpass_advances_to_generic_in_same_sudo(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": GENERIC_SECRET,
        "FAKE_ASKPASS_COUNT": "2",
    }
    fake_sudo(tmp_path, env)

    def candidates(_target: str, _environment):
        yield TARGET_KEY, TARGET_SECRET
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET

    result = run_sudo(
        binding_for(argv, single_password_qualified=True),
        argv,
        environment=env,
        resolver=candidates,
    )

    assert result.status == "completed"
    assert [item.status for item in result.credential_attempts] == ["retry_requested", "provided_to_askpass"]


def test_repeated_prompt_without_qualification_stops_before_generic_lookup(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": GENERIC_SECRET,
        "FAKE_ASKPASS_COUNT": "2",
    }
    fake_sudo(tmp_path, env)
    requested: list[str] = []

    def candidates(_target: str, _environment):
        requested.append("target")
        yield TARGET_KEY, TARGET_SECRET
        requested.append("generic")
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET

    result = run_sudo(binding_for(argv), argv, environment=env, resolver=candidates)

    assert result.status == "unsupported_authentication"
    assert result.action_required is True
    assert requested == ["target"]
    assert {item.variable: item.status for item in result.credential_attempts} == {
        TARGET_KEY: "provided_to_askpass",
        "GENERIC_SUDO_KEY": "not_checked",
    }
    assert not (tmp_path / "workload-stdin.bin").exists()


def test_empty_candidates_return_action_required_without_workload_start(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": GENERIC_SECRET,
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
        "FAKE_EXPECTED_SECRET": TARGET_SECRET,
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
    env = {"PATH": os.environ.get("PATH", os.defpath), "FAKE_EXPECTED_SECRET": TARGET_SECRET}
    fake_sudo(tmp_path, env)
    lookups: list[str] = []

    def candidates(*_):
        lookups.append("target")
        yield TARGET_KEY, TARGET_SECRET
        lookups.append("generic")
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET

    result = run_sudo(binding_for(argv), argv, environment=env, resolver=candidates)

    assert result.returncode == 1
    assert result.status == "failed"
    assert marker.read_bytes() == b"x"
    assert lookups == ["target"]


def test_attempt_limit_does_not_start_another_sudo_process(tmp_path: Path):
    argv = command_writes_stdin(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "FAKE_EXPECTED_SECRET": GENERIC_SECRET,
        "FAKE_ASKPASS_COUNT": "1",
    }
    fake_sudo(tmp_path, env)
    lookups: list[str] = []

    def candidates(*_):
        lookups.append("target")
        yield TARGET_KEY, TARGET_SECRET
        lookups.append("generic")
        yield "GENERIC_SUDO_KEY", GENERIC_SECRET

    result = run_sudo(binding_for(argv, single_password_qualified=True), argv,
                      environment=env, resolver=candidates)

    assert result.returncode == 1
    assert lookups == ["target"]
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
        "FAKE_EXPECTED_SECRET": TARGET_SECRET,
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
        resolver=lambda *_: ((TARGET_KEY, TARGET_SECRET),),
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
        value = TARGET_SECRET if name == TARGET_KEY else GENERIC_SECRET
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps({"variable": {"name": name, "value": value}}),
            stderr="",
        )

    env = {"PATH": str(tmp_path), TARGET_KEY: TARGET_SECRET}
    statuses: dict[str, str] = {}
    candidates = iter_candidates(TARGET, env, envman_runner=runner, statuses=statuses)
    assert next(candidates).variable == TARGET_KEY
    assert calls == []
    assert next(candidates).variable == "GENERIC_SUDO_KEY"
    assert calls == [[str(envman), "get", "GENERIC_SUDO_KEY", "--json", "--reveal"]]


