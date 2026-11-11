from subprocess import CompletedProcess

import pytest

from ls.core.process_identity import NameContext
from ls.core.process_naming_container import create_container


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_create_uses_all_names_and_verifies_native_id(monkeypatch, engine):
    calls = []
    container_id = "a" * 64

    def run(argv, **_kwargs):
        calls.append(argv)
        if "ps" in argv:
            return CompletedProcess(argv, 0, "reporter-demo\nreporter-demo-1\n", "")
        if "create" in argv:
            assert argv[argv.index("--name") + 1] == "reporter-demo-2"
            assert "io.localsetup.workload-name=reporter-demo-2" in argv
            return CompletedProcess(argv, 0, container_id + "\n", "")
        assert argv[-1] == container_id
        return CompletedProcess(argv, 0, "/reporter-demo-2\n", "")

    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", run)
    result = create_container(engine, "local-image", NameContext("reporter", "demo"), command=["serve"])
    assert (result.status, result.container_id, result.observed_name, result.inner_process_status) == (
        "verified", container_id, "reporter-demo-2", "not_observed")
    assert len(calls) == 3


def test_orchestrated_creation_preserves_supplied_name(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return CompletedProcess(argv, 0, "b" * 64 if "create" in argv else "/compose-project-web-1", "")

    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", run)
    result = create_container("podman", "local-image", NameContext("web"),
                              create_options=["--name", "compose-project-web-1"], orchestrated=True)
    assert result.status == "orchestrator_name_preserved"
    assert result.observed_name == "compose-project-web-1"
    assert calls[0].count("--name") == 1
    assert len(calls) == 2


def test_engine_failures_do_not_report_success_or_capture_arguments(monkeypatch):
    def run(argv, **_kwargs):
        return CompletedProcess(argv, 1, "", "sensitive diagnostic")

    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", run)
    result = create_container("docker", "local-image", NameContext("app"), command=["private-argument"])
    assert result.status == "inventory_failed"
    assert "private" not in repr(result) and "sensitive" not in repr(result)


def test_explicit_name_requires_orchestrator_mode():
    with pytest.raises(ValueError):
        create_container("docker", "image", NameContext("app"), create_options=["--name=other"])


@pytest.mark.parametrize("kwargs", [
    {"engine_options": ["create", "evil"]},
    {"engine_options": ["--host", "create"]},
    {"create_options": ["evil"]},
    {"create_options": ["--label", "io.localsetup.workload-name=other"]},
    {"create_options": ["--name", "safe", "evil"], "orchestrated": True},
    {"create_options": ["--name", "--label=bad"], "orchestrated": True},
    {"create_options": ["--name=other"], "orchestrated": True},
])
def test_option_tokens_cannot_change_creation_boundary(monkeypatch, kwargs):
    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run",
                        lambda *a, **k: pytest.fail("engine must not be invoked"))
    with pytest.raises(ValueError):
        create_container("docker", "image", NameContext("app"), **kwargs)


def test_orchestrator_name_mismatch_is_not_success(monkeypatch):
    def run(argv, **_kwargs):
        return CompletedProcess(argv, 0, "b" * 64 if "create" in argv else "/different", "")
    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", run)
    result = create_container("docker", "image", NameContext("app"), orchestrated=True,
                              create_options=["--name", "expected"])
    assert result.status == "verification_failed"


def test_canonical_cli_creates_with_owned_name_and_preserves_argv(monkeypatch, tmp_path, capsys):
    import json
    from ls.core import cli, process_identity

    calls = []
    def run(argv, **_kwargs):
        calls.append(argv)
        if "ps" in argv:
            return CompletedProcess(argv, 0, "worker\n", "")
        if "create" in argv:
            return CompletedProcess(argv, 0, "a" * 64, "")
        return CompletedProcess(argv, 0, "/worker-1", "")
    monkeypatch.setattr(process_identity, "set_process_name", lambda *_: True)
    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", run)
    code = cli.main(["--home", str(tmp_path), "process-name", "create", "--who", "worker",
                     "image", "--", "serve", "--name=workload-argument"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    created = calls[1]
    assert created[created.index("--name") + 1] == "worker-1"
    assert created[-3:] == ["image", "serve", "--name=workload-argument"]


def test_canonical_cli_format_and_verify(monkeypatch, tmp_path, capsys):
    import json
    from ls.core import cli, process_identity, process_naming_cli

    monkeypatch.setattr(process_identity, "set_process_name", lambda *_: True)
    prefix = ["--home", str(tmp_path), "process-name"]
    assert cli.main(prefix + ["format", "--who", "app", "--scope", "task", "--no-scope"]) == 0
    assert json.loads(capsys.readouterr().out)["full_name"] == "app"
    monkeypatch.setattr(process_naming_cli, "verify_process_name", lambda context, pid, options:
                        process_identity.NameResult("verification_failed", "app", "other", pid))
    assert cli.main(prefix + ["verify", "--who", "app", "--pid", "42"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "verification_failed"


def test_interrupt_is_not_swallowed(monkeypatch):
    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        create_container("docker", "image", NameContext("app"))
