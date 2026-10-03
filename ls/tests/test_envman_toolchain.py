from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys
import time

import pytest

import ls.core.cli as cli_mod
import ls.core.envman as envman_mod
from ls.core.manifests import load_platforms


REPO_ROOT = Path(__file__).resolve().parents[2]


def _parser():
    return cli_mod.build_parser(
        cli_mod._add_config_flags,
        cli_mod._add_selector_flags,
        cli_mod._add_visual_flags,
        cli_mod._add_harness_target_flags,
    )


def _result(returncode=0, stdout=b"", failure=None):
    return envman_mod._CommandResult(returncode, stdout=stdout, failure=failure)


def _args(
    action="install",
    *,
    check=False,
    install_skill=False,
    skill_scope=None,
    skill_target=None,
    target_directory=None,
):
    return SimpleNamespace(
        envman_action=action,
        check=check,
        install_skill=install_skill,
        skill_scope=skill_scope,
        skill_target=skill_target,
        target_directory=target_directory,
    )


def test_envman_parser_exposes_explicit_actions_and_doctor_opt_in():
    parser = _parser()
    status = parser.parse_args(["envman", "status"])
    check = parser.parse_args(["envman", "update", "--check"])
    install = parser.parse_args(
        [
            "envman",
            "install",
            "--install-skill",
            "--skill-scope",
            "global",
            "--skill-target",
            "codex",
        ]
    )
    doctor_default = parser.parse_args(["doctor"])
    doctor_opt_in = parser.parse_args(["doctor", "--envman"])

    assert (status.cmd, status.envman_action) == ("envman", "status")
    assert (check.envman_action, check.check) == ("update", True)
    assert install.skill_scope == ["global"]
    assert install.skill_target == ["codex"]
    assert doctor_default.envman is False
    assert doctor_opt_in.envman is True


def test_status_absent_executable_is_nonfatal_and_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: None)
    monkeypatch.setattr(
        envman_mod,
        "_run_capture",
        lambda *_args, **_kwargs: pytest.fail("no command should run when Envman is absent"),
    )

    assert envman_mod.probe_status() == {
        "tool": "envman",
        "status": "unavailable",
        "code": "executable-missing",
    }


def test_status_suppresses_unexpected_probe_exception_text(monkeypatch, capsys):
    monkeypatch.setattr(
        envman_mod,
        "probe_status",
        lambda: (_ for _ in ()).throw(RuntimeError("PRIVATE_PATH_AND_SECRET")),
    )
    args = _parser().parse_args(["envman", "status"])

    assert envman_mod.handle(args, REPO_ROOT) == 0
    output = capsys.readouterr().out
    assert "PRIVATE_PATH_AND_SECRET" not in output
    assert json.loads(output) == {
        "tool": "envman",
        "status": "present-unverified",
        "code": "probe-failed",
    }


@pytest.mark.parametrize(
    ("version", "check_payload", "expected"),
    [
        (
            b"envman, version 0.1.8\n",
            {"target": "/home/secret-user/.config/envman", "variables": 2},
            {"status": "available", "version": "0.1.8", "variables": 2},
        ),
        (
            b"envman 0.1.10\n",
            {
                "target": "/home/secret-user/.config/envman",
                "variables": 0,
                "legacy_plaintext_snapshots": 0,
                "storage_encrypted": True,
            },
            {
                "status": "not-activated-or-empty",
                "version": "0.1.10",
                "variables": 0,
                "legacy_plaintext_snapshots": 0,
                "storage_encrypted": True,
            },
        ),
    ],
)
def test_status_accepts_old_and_current_schemas_and_discards_target(
    monkeypatch, version, check_payload, expected
):
    commands = []
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/envman")

    def run_capture(argv, **kwargs):
        commands.append((argv, kwargs))
        if argv[1:] == ["--version"]:
            return _result(stdout=version)
        return _result(stdout=json.dumps(check_payload).encode())

    monkeypatch.setattr(envman_mod, "_run_capture", run_capture)
    result = envman_mod.probe_status()

    for key, value in expected.items():
        assert result[key] == value
    assert [call[0][1:] for call in commands] == [["--version"], ["check", "--json"]]
    assert "/home/secret-user" not in json.dumps(result)
    assert "target" not in result


@pytest.mark.parametrize(
    "payload",
    [
        {"target": "/home/private", "variables": True},
        {"target": "/home/private", "variables": 1, "unexpected": "DO_NOT_LEAK"},
        {"target": "/home/private", "variables": 1, "storage_encrypted": False},
        {"target": "/home/private", "variables": -1},
        {
            "target": "/home/private",
            "variables": 1,
            "legacy_plaintext_snapshots": 0,
            "storage_encrypted": 1,
        },
    ],
)
def test_malformed_or_unknown_health_schema_fails_closed_without_echo(
    monkeypatch, payload
):
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/envman")
    monkeypatch.setattr(
        envman_mod,
        "_run_capture",
        lambda argv, **_kwargs: (
            _result(stdout=b"envman 0.1.10\n")
            if argv[1:] == ["--version"]
            else _result(stdout=json.dumps(payload).encode())
        ),
    )

    result = envman_mod.probe_status()

    assert result["status"] == "present-unverified"
    assert result["code"] == "invalid-response"
    assert "/home/private" not in json.dumps(result)
    assert "DO_NOT_LEAK" not in json.dumps(result)


@pytest.mark.parametrize(
    ("version_result", "check_result", "expected_code"),
    [
        (_result(failure="timeout"), _result(stdout=b"{}"), "version-unverified"),
        (
            _result(stdout=b"envman 0.1.10\n"),
            _result(failure="timeout"),
            "probe-timeout",
        ),
        (
            _result(stdout=b"envman 0.1.10\n"),
            _result(failure="output-limit"),
            "probe-output-limit",
        ),
    ],
)
def test_probe_timeouts_and_oversized_output_return_fixed_codes(
    monkeypatch, version_result, check_result, expected_code
):
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/envman")
    monkeypatch.setattr(
        envman_mod,
        "_run_capture",
        lambda argv, **_kwargs: version_result if argv[1:] == ["--version"] else check_result,
    )

    result = envman_mod.probe_status()

    assert result["status"] == "present-unverified"
    assert result["code"] == expected_code
    assert "stdout" not in result and "stderr" not in result


def test_status_reports_unsafe_storage_without_blocking_doctor(monkeypatch):
    payload = {
        "target": "/home/private/.config/envman",
        "variables": 3,
        "legacy_plaintext_snapshots": 1,
        "storage_encrypted": False,
    }
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/envman")
    monkeypatch.setattr(
        envman_mod,
        "_run_capture",
        lambda argv, **_kwargs: (
            _result(stdout=b"envman 0.1.10\n")
            if argv[1:] == ["--version"]
            else _result(stdout=json.dumps(payload).encode())
        ),
    )

    result = envman_mod.probe_status()

    assert result["status"] == "present-unverified"
    assert result["code"] == "storage-not-encrypted"
    assert result["storage_encrypted"] is False
    assert "/home/private" not in json.dumps(result)


def test_update_check_uses_only_the_explicit_read_only_json_command(monkeypatch):
    payload = {
        "schema": "envman.update-result",
        "schema_version": 1,
        "status": "update-available",
        "installed_version": "0.1.8",
        "available_version": "0.1.10",
    }
    commands = []
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/envman")

    def run_capture(argv, **kwargs):
        commands.append((argv, kwargs))
        return _result(stdout=json.dumps(payload).encode())

    monkeypatch.setattr(envman_mod, "_run_capture", run_capture)
    result, code = envman_mod.update_check()

    assert code == 0
    assert result == {
        "tool": "envman",
        "status": "update-available",
        "installed_version": "0.1.8",
        "available_version": "0.1.10",
        "code": None,
    }
    assert [call[0][1:] for call in commands] == [["update", "--check", "--json"]]
    assert commands[0][1]["output_limit"] == envman_mod.CHECK_OUTPUT_LIMIT


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema": "wrong-schema",
            "schema_version": 1,
            "status": "current",
            "installed_version": "0.1.8",
            "available_version": "0.1.8",
        },
        {
            "schema": "envman.update-result",
            "schema_version": True,
            "status": "current",
            "installed_version": "0.1.8",
            "available_version": "0.1.8",
        },
        {
            "schema": "envman.update-result",
            "schema_version": 1,
            "status": ["current"],
            "installed_version": "0.1.8",
            "available_version": "0.1.8",
        },
        {
            "schema": "envman.update-result",
            "schema_version": 1,
            "status": "current",
            "installed_version": "0.1.8",
            "available_version": "0.1.8",
            "token": "DO_NOT_LEAK",
        },
    ],
)
def test_update_check_rejects_unknown_shapes_without_echo(monkeypatch, payload):
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/envman")
    monkeypatch.setattr(
        envman_mod,
        "_run_capture",
        lambda *_args, **_kwargs: _result(stdout=json.dumps(payload).encode()),
    )

    result, code = envman_mod.update_check()

    assert code == 1
    assert result["status"] == "present-unverified"
    assert result["code"] == "invalid-response"
    assert "DO_NOT_LEAK" not in json.dumps(result)


@pytest.mark.parametrize("action", ["install", "update"])
def test_explicit_mutations_use_one_latest_bootstrap_without_init_or_skill_flags(
    monkeypatch, action
):
    calls = []
    monkeypatch.setattr(envman_mod, "_supported_bootstrap_host", lambda: True)
    monkeypatch.setattr(envman_mod, "_find_executable", lambda name: f"/tools/{name}")

    def run_discard(argv, *, cwd, timeout):
        calls.append((argv, cwd, timeout))
        return _result(stdout=b"could contain secret output")

    monkeypatch.setattr(envman_mod, "_run_discard", run_discard)
    result, code = envman_mod.mutate(_args(action), REPO_ROOT)

    assert code == 0
    assert result["status"] == "completed"
    assert len(calls) == 1
    argv, cwd, timeout = calls[0]
    assert argv == [
        f"/tools/uv",
        "run",
        "--python",
        "3.12",
        "--script",
        envman_mod.BOOTSTRAP_URL,
    ]
    assert "init" not in argv
    assert "--install-skill" not in argv
    assert cwd is None
    assert timeout == envman_mod.MUTATION_TIMEOUT_SECONDS


def test_explicit_skill_install_requires_a_current_platform_and_one_selection(monkeypatch):
    calls = []
    monkeypatch.setattr(envman_mod, "_supported_bootstrap_host", lambda: True)
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/uv")
    monkeypatch.setattr(
        envman_mod,
        "_run_discard",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or _result()),
    )
    target = load_platforms(REPO_ROOT)[0].platform_id

    result, code = envman_mod.mutate(
        _args(
            "install",
            install_skill=True,
            skill_scope=["global"],
            skill_target=[target],
        ),
        REPO_ROOT,
    )

    assert code == 0
    assert result["status"] == "completed"
    assert len(calls) == 1
    argv = calls[0][0][0]
    assert argv[-5:] == ["--install-skill", "--skill-scope", "global", "--skill-target", target]

    repeated, repeated_code = envman_mod.mutate(
        _args(
            "install",
            install_skill=True,
            skill_scope=["global", "global"],
            skill_target=[target],
        ),
        REPO_ROOT,
    )
    unsupported, unsupported_code = envman_mod.mutate(
        _args(
            "install",
            install_skill=True,
            skill_scope=["global"],
            skill_target=["all"],
        ),
        REPO_ROOT,
    )
    assert repeated_code == unsupported_code == 2
    assert repeated["code"] == "skill-selection-required"
    assert unsupported["code"] == "unsupported-skill-target"
    assert len(calls) == 1


def test_repeated_skill_target_and_check_with_skill_are_rejected(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(envman_mod, "_supported_bootstrap_host", lambda: True)
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/uv")
    monkeypatch.setattr(
        envman_mod,
        "_run_discard",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or _result()),
    )
    target = load_platforms(REPO_ROOT)[0].platform_id

    repeated, code = envman_mod.mutate(
        _args(
            install_skill=True,
            skill_scope=["global"],
            skill_target=[target, target],
        ),
        REPO_ROOT,
    )
    assert code == 2
    assert repeated["code"] == "skill-selection-required"
    assert not calls

    parsed = _parser().parse_args(
        [
            "envman",
            "update",
            "--check",
            "--install-skill",
            "--skill-scope",
            "global",
            "--skill-target",
            target,
        ]
    )
    monkeypatch.setattr(
        envman_mod,
        "update_check",
        lambda: pytest.fail("a skill install must not be ignored by --check"),
    )
    assert envman_mod.handle(parsed, REPO_ROOT) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "check-cannot-install-skill"
    assert not calls


def test_unsupported_host_does_not_invoke_latest_bootstrap(monkeypatch):
    calls = []
    monkeypatch.setattr(envman_mod, "_supported_bootstrap_host", lambda: False)
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/uv")
    monkeypatch.setattr(
        envman_mod,
        "_run_discard",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or _result()),
    )

    result, code = envman_mod.mutate(_args("install"), REPO_ROOT)

    assert code == 2
    assert result["code"] == "unsupported-host"
    assert not calls


def test_repository_skill_scope_requires_explicit_git_root_and_uses_it_as_cwd(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    nested = repository / "nested"
    nested.mkdir()
    calls = []
    monkeypatch.setattr(envman_mod, "_supported_bootstrap_host", lambda: True)
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/uv")

    def run_capture(argv, **kwargs):
        assert argv[:4] == ["git", "-C", str(nested), "rev-parse"]
        return _result(stdout=(str(repository) + "\n").encode())

    def run_discard(argv, *, cwd, timeout):
        calls.append((argv, cwd))
        return _result()

    monkeypatch.setattr(envman_mod, "_run_capture", run_capture)
    monkeypatch.setattr(envman_mod, "_run_discard", run_discard)
    target = load_platforms(REPO_ROOT)[0].platform_id

    result, code = envman_mod.mutate(
        _args(
            install_skill=True,
            skill_scope=["repository"],
            skill_target=[target],
            target_directory=str(nested),
        ),
        REPO_ROOT,
    )

    assert code == 2
    assert result["code"] == "repository-root-required"
    assert not calls

    monkeypatch.setattr(
        envman_mod,
        "_run_capture",
        lambda _argv, **_kwargs: _result(stdout=(str(repository) + "\n").encode()),
    )
    result, code = envman_mod.mutate(
        _args(
            "update",
            install_skill=True,
            skill_scope=["repository"],
            skill_target=[target],
            target_directory=str(repository),
        ),
        REPO_ROOT,
    )
    assert code == 0
    assert result["status"] == "completed"
    assert len(calls) == 1
    assert calls[0][1] == repository.resolve()
    assert calls[0][0][-5:] == ["--install-skill", "--skill-scope", "repository", "--skill-target", target]


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        (_result(returncode=9, stdout=b"secret failure detail"), "mutation-failed-unknown"),
        (_result(failure="timeout"), "mutation-timeout-unknown"),
    ],
)
def test_mutation_failure_or_timeout_is_unknown_and_never_replayed(
    monkeypatch, result, expected_code
):
    calls = []
    monkeypatch.setattr(envman_mod, "_supported_bootstrap_host", lambda: True)
    monkeypatch.setattr(envman_mod, "_find_executable", lambda _name: "/tools/uv")
    monkeypatch.setattr(
        envman_mod,
        "_run_discard",
        lambda argv, **kwargs: (calls.append((argv, kwargs)) or result),
    )

    payload, code = envman_mod.mutate(_args("update"), REPO_ROOT)

    assert code == 1
    assert payload["status"] == "unknown"
    assert payload["code"] == expected_code
    assert "secret failure detail" not in json.dumps(payload)
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="process-group regression inspects Linux /proc")
def test_mutation_timeout_kills_local_descendants_before_return(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    script = "\n".join(
        [
            "import subprocess, sys, time",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
            "with open(sys.argv[1], 'w', encoding='ascii') as stream:",
            "    stream.write(str(child.pid))",
            "time.sleep(30)",
        ]
    )

    result = envman_mod._run_discard(
        [sys.executable, "-c", script, str(child_pid_path)],
        cwd=tmp_path,
        timeout=0.5,
    )

    assert result.failure == "timeout"
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            stat = Path(f"/proc/{child_pid}/stat").read_text(encoding="ascii")
            process_state = stat[stat.rfind(")") + 2 :].split()[0]
        except FileNotFoundError:
            break
        if process_state == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("bootstrap descendant remained runnable after timeout return")


def test_ordinary_cli_command_does_not_probe_or_mutate_envman(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        envman_mod,
        "_run_capture",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or pytest.fail("unexpected Envman probe")),
    )
    monkeypatch.setattr(
        envman_mod,
        "_run_discard",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or pytest.fail("unexpected Envman mutation")),
    )

    assert cli_mod._main(["--source-root", str(REPO_ROOT), "catalog"]) == 0
    capsys.readouterr()
    assert not calls
