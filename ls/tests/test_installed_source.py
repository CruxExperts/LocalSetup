import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ls.core import cli, installed_source, shell


def _write_minimal_source(root: Path, *, project_name: str = "localsetup") -> Path:
    (root / "ls" / "tools").mkdir(parents=True)
    (root / "ls" / "tools" / "localsetup.py").write_text("# source archive entrypoint\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{project_name}"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    return root


def _args(*, source_root=None, repo=None, cmd="doctor"):
    return SimpleNamespace(source_root=source_root, repo=repo, cmd=cmd)


def test_distribution_record_must_match_running_module(tmp_path, monkeypatch):
    module = tmp_path / 'ls/core/cli.py'
    dist = SimpleNamespace(files=[Path('ls/core/cli.py')], locate_file=lambda entry: tmp_path / entry,
                           read_text=lambda name: 'present' if name in {'RECORD', 'WHEEL'} else None)
    monkeypatch.setattr(installed_source, 'distribution', lambda name: dist)
    assert installed_source.wheel_module(module)
    assert not installed_source.wheel_module(tmp_path / 'checkout/ls/core/cli.py')
    dist.read_text = lambda name: None
    assert not installed_source.wheel_module(module)  # SOURCES-only metadata.
    dist.read_text = lambda name: '{"dir_info":{"editable":true}}' if name == 'direct_url.json' else 'present'
    assert not installed_source.wheel_module(module)
    dist.read_text = lambda name: 'present' if name in {'RECORD', 'WHEEL'} else None
    dist.files = [Path('editable.pth')]
    assert not installed_source.wheel_module(module)


def test_wheel_target_default_and_explicit_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(installed_source, 'wheel_module', lambda path: True)
    monkeypatch.setattr(cli, '_is_global_shim_invocation', lambda: False)
    monkeypatch.setattr(cli, 'detect_invocation_target', lambda: tmp_path / 'caller')
    args = argparse.Namespace(cmd='plan', target_directory=None, config=None)
    cli._inject_global_target(args)
    assert args.target_directory == str(tmp_path / 'caller')
    assert args.detected_target_directory
    args.target_directory = str(tmp_path / 'explicit')
    cli._inject_global_target(args)
    assert args.target_directory == str(tmp_path / 'explicit')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'target_directory': str(tmp_path / 'configured')}))
    args = argparse.Namespace(cmd='plan', target_directory=None, config=str(config))
    cli._inject_global_target(args)
    assert args.target_directory is None
    assert cli._resolved_config(args, tmp_path).target_directory == str(tmp_path / 'configured')


def test_source_checkout_default_and_non_target_command_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(installed_source, 'wheel_module', lambda path: False)
    monkeypatch.setattr(cli, '_is_global_shim_invocation', lambda: False)
    args = argparse.Namespace(cmd='plan', target_directory=None, config=None)
    cli._inject_global_target(args)
    assert args.target_directory is None
    monkeypatch.setattr(installed_source, 'wheel_module', lambda path: True)
    args.cmd = 'test-workers'
    cli._inject_global_target(args)
    assert args.target_directory is None


def test_wheel_uses_valid_registered_source_archive_without_git_or_lock(tmp_path, monkeypatch):
    source = _write_minimal_source(tmp_path / "source-archive")
    home = tmp_path / "home"
    shell.register_shell_command(source, home=home)
    monkeypatch.setattr(installed_source, "wheel_module", lambda _path: True)

    selected, available = cli._select_source_root(_args(), home=home, global_shim=False)

    assert selected == source.resolve()
    assert available is True
    assert not (source / ".git").exists()
    assert not (source / "uv.lock").exists()


def test_source_precedence_keeps_explicit_repo_and_enabled_shim_ahead_of_registration(tmp_path, monkeypatch):
    registered = _write_minimal_source(tmp_path / "registered")
    home = tmp_path / "home"
    shell.register_shell_command(registered, home=home)
    monkeypatch.setattr(installed_source, "wheel_module", lambda _path: True)

    explicit = tmp_path / "explicit-source"
    selected, available = cli._select_source_root(
        _args(source_root=str(explicit), repo=str(tmp_path / "repo")),
        home=home,
        global_shim=True,
    )
    assert selected == explicit.resolve()
    assert available is True

    repo = tmp_path / "repo-source"
    selected, _ = cli._select_source_root(_args(repo=str(repo)), home=home, global_shim=True)
    assert selected == repo.resolve()

    shim_source = tmp_path / "active-shim-source"
    monkeypatch.setenv(shell.SHIM_SOURCE_ROOT_ENV, str(shim_source))
    selected, _ = cli._select_source_root(_args(), home=home, global_shim=True)
    assert selected == shim_source.resolve()

    selected, _ = cli._select_source_root(_args(), home=home, global_shim=False)
    assert selected == registered.resolve()


@pytest.mark.parametrize("doctor_args", [("doctor",), ("doctor", "repair")])
def test_wheel_doctor_without_usable_registered_source_stops_before_dispatch(
    tmp_path, monkeypatch, capsys, doctor_args
):
    package_root = tmp_path / "wheel-package"
    monkeypatch.setattr(installed_source, "wheel_module", lambda _path: True)
    monkeypatch.setattr(cli, "registered_source_root", lambda _home: None)
    monkeypatch.setattr(cli, "_repo_root", lambda: package_root)
    monkeypatch.setattr(cli, "_inject_global_target", lambda _args: None)
    monkeypatch.setattr(
        cli.cli_client_state_commands,
        "handle",
        lambda *_args: pytest.fail("wheel doctor must stop before state or health handling"),
    )

    result = cli._main(["--home", str(tmp_path / "home"), *doctor_args])

    captured = capsys.readouterr()
    assert result == 2
    assert "source unavailable" in captured.err
    assert "--source-root" in captured.err
    assert captured.out == ""


def test_wheel_source_independent_command_keeps_package_resource_fallback(tmp_path, monkeypatch):
    package_root = tmp_path / "wheel-package"
    seen = []
    monkeypatch.setattr(installed_source, "wheel_module", lambda _path: True)
    monkeypatch.setattr(cli, "registered_source_root", lambda _home: None)
    monkeypatch.setattr(cli, "_repo_root", lambda: package_root)
    monkeypatch.setattr(cli, "_inject_global_target", lambda _args: None)
    monkeypatch.setattr(cli.cli_client_state_commands, "handle", lambda *_args: None)
    monkeypatch.setattr(cli.cli_install_commands, "handle", lambda *_args: None)
    monkeypatch.setattr(cli.cli_state_commands, "handle", lambda *_args: None)
    monkeypatch.setattr(
        cli.cli_misc_commands,
        "handle",
        lambda _facade, _args, root, _home: (seen.append(root) or 0),
    )

    result = cli._main(["catalog"])

    assert result == 0
    assert seen == [package_root]


def test_registered_wheel_source_does_not_change_explicit_consumer_target(tmp_path, monkeypatch):
    source = _write_minimal_source(tmp_path / "source-archive")
    home = tmp_path / "home"
    target = tmp_path / "consumer-repo"
    shell.register_shell_command(source, home=home)
    seen = []
    monkeypatch.setattr(installed_source, "wheel_module", lambda _path: True)
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path / "wheel-package")
    monkeypatch.setattr(cli.cli_client_state_commands, "handle", lambda *_args: None)
    monkeypatch.setattr(cli.cli_install_commands, "handle", lambda *_args: None)
    monkeypatch.setattr(
        cli.cli_state_commands,
        "handle",
        lambda _facade, args, root, selected_home: (
            seen.append((args.target_directory, root, selected_home)) or 0
        ),
    )

    result = cli._main(
        ["--home", str(home), "--target-directory", str(target), "doctor"]
    )

    assert result == 0
    assert seen == [(str(target), source.resolve(), home.resolve())]


def test_wheel_doctor_uses_configured_home_registration_not_default_home(tmp_path, monkeypatch):
    configured_home = tmp_path / "configured-home"
    default_home = tmp_path / "default-home"
    configured_source = _write_minimal_source(tmp_path / "configured-source")
    default_source = _write_minimal_source(tmp_path / "default-source")
    shell.register_shell_command(configured_source, home=configured_home)
    shell.register_shell_command(default_source, home=default_home)
    config_path = tmp_path / "localsetup.json"
    config_path.write_text(json.dumps({"home": str(configured_home)}), encoding="utf-8")
    seen = []

    monkeypatch.setattr(installed_source, "wheel_module", lambda _path: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: default_home))
    monkeypatch.setattr(cli, "_inject_global_target", lambda _args: None)
    monkeypatch.setattr(cli.cli_client_state_commands, "handle", lambda *_args: None)
    monkeypatch.setattr(cli.cli_install_commands, "handle", lambda *_args: None)
    monkeypatch.setattr(
        cli.cli_state_commands,
        "handle",
        lambda _facade, args, root, selected_home: (
            seen.append(
                (
                    root,
                    Path(cli._resolved_config(args, selected_home).home).expanduser().resolve(),
                )
            )
            or 0
        ),
    )

    result = cli._main(["doctor", "--config", str(config_path)])

    assert result == 0
    assert seen == [(configured_source.resolve(), configured_home.resolve())]
    assert configured_source.resolve() != default_source.resolve()


def test_wheel_install_uses_configured_home_registration_not_default_home(tmp_path, monkeypatch):
    configured_home = tmp_path / "configured-home"
    default_home = tmp_path / "default-home"
    configured_source = _write_minimal_source(tmp_path / "configured-source")
    default_source = _write_minimal_source(tmp_path / "default-source")
    shell.register_shell_command(configured_source, home=configured_home)
    shell.register_shell_command(default_source, home=default_home)
    config_path = tmp_path / "localsetup.json"
    config_path.write_text(json.dumps({"home": str(configured_home)}), encoding="utf-8")
    seen = []

    monkeypatch.setattr(installed_source, "wheel_module", lambda _path: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: default_home))
    monkeypatch.setattr(cli, "_is_global_shim_invocation", lambda: False)
    monkeypatch.setattr(cli, "_inject_global_target", lambda _args: None)
    monkeypatch.setattr(cli.cli_client_state_commands, "handle", lambda *_args: None)
    monkeypatch.setattr(
        cli.cli_install_commands,
        "handle",
        lambda _facade, args, root, selected_home: (
            seen.append(
                (
                    root,
                    Path(cli._resolved_config(args, selected_home).home).expanduser().resolve(),
                )
            )
            or 0
        ),
    )

    result = cli._main(["install", "--config", str(config_path)])

    assert result == 0
    assert seen == [(configured_source.resolve(), configured_home.resolve())]
    assert configured_source.resolve() != default_source.resolve()


@pytest.mark.parametrize("bad_registration", ["stale", "unmanaged", "wrong-project", "malformed"])
def test_wheel_rejects_stale_malformed_unmanaged_or_unqualified_registration(
    tmp_path, monkeypatch, bad_registration
):
    home = tmp_path / "home"
    shim_path = shell.shim_path(home)
    shim_path.parent.mkdir(parents=True)
    source = _write_minimal_source(
        tmp_path / "source-archive",
        project_name="other-project" if bad_registration == "wrong-project" else "localsetup",
    )
    recorded_root = tmp_path / "missing-source" if bad_registration == "stale" else source
    managed = bad_registration != "unmanaged"
    recorded_value = "'unterminated" if bad_registration == "malformed" else str(recorded_root)
    content = ["#!/usr/bin/env bash"]
    if managed:
        content.append(f"# {shell.SHIM_MARKER}")
        content.append(f"export {shell.SHIM_ENV}=1")
    content.extend([f"LOCALSETUP_SOURCE_ROOT={recorded_value}", ""])
    shim_path.write_text("\n".join(content), encoding="utf-8")
    monkeypatch.setattr(installed_source, "wheel_module", lambda _path: True)

    selected, available = cli._select_source_root(_args(), home=home, global_shim=False)

    assert selected == cli._repo_root()
    assert available is False
