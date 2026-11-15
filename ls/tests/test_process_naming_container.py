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
    monkeypatch.setattr(process_identity, "set_process_name", lambda *_, **__: True)
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

    monkeypatch.setattr(process_identity, "set_process_name", lambda *_, **__: True)
    prefix = ["--home", str(tmp_path), "process-name"]
    assert cli.main(prefix + ["format", "--who", "app", "--scope", "task", "--no-scope"]) == 0
    assert json.loads(capsys.readouterr().out)["full_name"] == "app"
    monkeypatch.setattr(process_naming_cli, "verify_process_name", lambda context, pid, options:
                        process_identity.NameResult("verification_failed", "app", "other", pid))
    assert cli.main(prefix + ["verify", "--who", "app", "--pid", "42"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "verification_failed"


@pytest.mark.parametrize("action", ["format", "verify", "resolve"])
def test_cli_resolution_uses_selected_format_options(monkeypatch, capsys, action):
    import json
    import os
    from ls.core import process_identity, process_naming_cli

    monkeypatch.setattr(process_identity, "set_process_name", lambda *_, **__: True)
    args = [action, "--who", "app", "--scope", "private-scope", "--version", "1.2",
            "--version-verified", "--no-scope", "--no-version", "--dispatch", "worker"]
    if action != "format":
        args += ["--pid", str(os.getpid())]
    process_naming_cli.main(args)
    payload = json.loads(capsys.readouterr().out)
    resolution = payload if action == "resolve" else payload["resolution"]
    assert resolution["full_name"] == "app-worker"
    assert resolution["compact_name"] == "app-worker"
    assert resolution["version_source"] == "caller_asserted"
    if action != "resolve":
        assert payload["full_name"] == resolution["full_name"]
        assert payload["expected_linux_name"] == resolution["compact_name"]
    else:
        assert resolution["pid"] == os.getpid()


@pytest.mark.parametrize("action", ["format", "verify"])
def test_cli_unrepresentable_native_name_keeps_structured_result(monkeypatch, capsys, action):
    import json
    import os
    from ls.core import process_identity, process_naming_cli

    monkeypatch.setattr(process_identity, "set_process_name", lambda *_, **__: True)
    index = "12345678901234567890"
    args = [action, "--who", "app", "--index", index]
    if action == "verify":
        args += ["--pid", str(os.getpid())]
    assert process_naming_cli.main(args) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert not captured.err
    assert payload["status"] == "unsupported"
    assert payload["full_name"] == "app-" + index
    assert payload["expected_linux_name"] is None
    assert payload["resolution"]["compact_detail"]


def test_interrupt_is_not_swallowed(monkeypatch):
    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        create_container("docker", "image", NameContext("app"))


def test_container_uses_shared_resolver_formatter_and_verifier(monkeypatch):
    from ls.core import process_naming_container as adapter

    calls = []
    resolver, formatter, verifier = adapter.resolve_workload, adapter.format_name, adapter.verify_native_name
    def resolve(*args, **kwargs):
        calls.append("resolve")
        return resolver(*args, **kwargs)
    def format_tag(*args, **kwargs):
        calls.append("format")
        return formatter(*args, **kwargs)
    def verify(*args, **kwargs):
        calls.append("verify")
        return verifier(*args, **kwargs)
    monkeypatch.setattr(adapter, "resolve_workload", resolve)
    monkeypatch.setattr(adapter, "format_name", format_tag)
    monkeypatch.setattr(adapter, "verify_native_name", verify)
    def run(argv, **_kwargs):
        if "ps" in argv:
            return CompletedProcess(argv, 0, "", "")
        if "create" in argv:
            return CompletedProcess(argv, 0, "c" * 64, "")
        return CompletedProcess(argv, 0, "/app-v1.0", "")
    monkeypatch.setattr(adapter.subprocess, "run", run)
    result = create_container("docker", "image", NameContext("app", version="1.0", version_verified=True))
    assert result.status == "verified"
    assert result.full_name == "app-v1.0"
    assert calls[0] == "resolve" and "format" in calls and calls[-1] == "verify"


def test_engine_managed_name_is_explicitly_preserved(monkeypatch):
    def run(argv, **_kwargs):
        return CompletedProcess(argv, 0, "d" * 64 if "create" in argv else "/generated", "")
    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", run)
    result = create_container("docker", "image", NameContext("app"), orchestrated=True)
    assert result.status == "orchestrator_name_preserved"
    assert result.naming_disposition == "engine_name_preserved"
    assert result.observed_name == "generated"


def test_container_nested_long_name_collision_preserves_command(monkeypatch):
    from dataclasses import replace
    from ls.core.process_identity import bounded_name, format_name

    context = NameContext("app", command_path=("application", "library" * 17, "documents", "check"))
    first = bounded_name(context, 128)
    second = bounded_name(replace(context, index=1), 128)
    calls = []
    command = ["serve", "--token=private"]
    def run(argv, **_kwargs):
        calls.append(argv)
        if "ps" in argv:
            return CompletedProcess(argv, 0, first + "\n", "")
        if "create" in argv:
            assert argv[argv.index("--name") + 1] == second
            assert "io.localsetup.workload-name=" + format_name(replace(context, index=1)) in argv
            return CompletedProcess(argv, 0, "e" * 64, "")
        return CompletedProcess(argv, 0, "/" + second, "")
    monkeypatch.setattr("ls.core.process_naming_container.subprocess.run", run)
    result = create_container("docker", "image", context, command=command)
    assert result.status == "truncated"
    assert result.observed_name == second and second.endswith("-1")
    assert calls[1][-2:] == command == ["serve", "--token=private"]
    assert "private" not in repr(result)
