"""Common preferences are bounded, traceable, and cannot grant authority."""
import json
from pathlib import Path

import pytest

from ls.core.framework_configuration.service import paths, resolve

SOURCE = Path(__file__).resolve().parents[2]


def write(path, value):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o700)
    path.write_text(json.dumps({"schema_version": 1, **value}))
    path.chmod(0o600)


def test_precedence_and_origins_preserve_unset_preferences(tmp_path):
    home, project = tmp_path / "home", tmp_path / "repo"
    user, repo = paths(home, project)
    write(user, {"preset": "conservative", "testing": {"workers": 3}})
    write(repo, {"testing": {"workers": 2}})
    result = resolve(project, home)
    assert result["settings"]["testing"]["workers"] == 2
    assert result["settings"]["testing"]["host_reserve_percent"] == 20
    assert result["origins"]["testing.workers"] == str(repo)
    assert result["origins"]["testing.host_reserve_percent"] == "preset:conservative"


@pytest.mark.parametrize("value", [
    {"testing": {"adaptive": {"recovery_percent": 90}}},
    {"testing": {"workers": True}},
    {"trust": {"enabled": True}},
    {"context_index": {"identity": {"memory_uuid": "untrusted"}}},
    {"installation": {"home": "/tmp/redirect"}},
    {"installation": {"global_packs": ["all"]}},
    {"installation": {"output": {"report": "/tmp/unowned-report"}}},
    {"installation": {"skill_scope": "both"}},
    {"installation": {"packs": ["all"]}},
    {"application_profiles": []},
])
def test_repo_cannot_bypass_schema_or_protected_owners(tmp_path, value):
    home, project = tmp_path / "home", tmp_path / "repo"
    write(paths(home, project)[1], value)
    with pytest.raises(ValueError):
        resolve(project, home)


def test_duplicate_json_symlink_and_unowned_mode_fail_closed(tmp_path):
    home, project = tmp_path / "home", tmp_path / "repo"
    user, repo = paths(home, project)
    write(user, {})
    user.write_text('{"schema_version":1,"testing":{},"testing":{}}')
    with pytest.raises(ValueError):
        resolve(project, home)
    write(user, {})
    user.chmod(0o666)
    with pytest.raises(ValueError, match="writable"):
        resolve(project, home)
    user.chmod(0o600)
    repo.parent.mkdir()
    repo.symlink_to(user)
    with pytest.raises(OSError):
        resolve(project, home)


def test_testing_consumer_rejects_ambiguous_legacy_migration(tmp_path, monkeypatch):
    from ls.core.test_workers import testing_configuration
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write(tmp_path / "localsetup.json", {"testing": {"workers": 2}})
    assert testing_configuration(tmp_path)["workers"] == 2
    (tmp_path / "pyproject.toml").write_text("[tool.localsetup.testing]\nworkers=3\n")
    with pytest.raises(ValueError, match="Conflicting legacy testing"):
        testing_configuration(tmp_path)


def test_profile_validation_does_not_echo_invalid_values(tmp_path):
    home = tmp_path / "home"
    write(paths(home, tmp_path)[0], {"application_profiles": [
        {"application": "tmux", "name": "agent-shell", "settings": {"history_limit": "secret-fixture"}}]})
    with pytest.raises(ValueError) as error:
        resolve(tmp_path, home)
    assert "secret-fixture" not in str(error.value)


def test_profile_and_context_consumers_use_shared_preferences(tmp_path):
    from ls.core.application_profiles.service import resolve_profile
    from ls.core.context_index.config import load_config
    home, project = tmp_path / "home", tmp_path / "repo"
    write(paths(home, project)[0], {"application_profiles": [
        {"application": "tmux", "name": "agent-shell", "settings": {"history_limit": 1234}}]})
    write(paths(home, project)[1], {"context_index": {"retrieval": {"top_k": 7}}})
    profile = resolve_profile(source_root=SOURCE, home=home)
    assert profile["settings"]["history_limit"] == 1234
    config, loaded = load_config(project, home)
    assert config["context_index"]["retrieval"]["top_k"] == 7
    assert str(paths(home, project)[1]) in loaded


def test_public_cli_routes_common_configuration_without_authority(tmp_path, capsys):
    from ls.core.cli import main
    home, project = tmp_path / "home", tmp_path / "repo"
    write(paths(home, project)[1], {"testing": {"workers": 2}})
    assert main(["--source-root", str(SOURCE), "--home", str(home), "config", "show",
                 "--project-root", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["settings"]["testing"]["workers"] == 2


def test_conflicting_profile_save_is_nonmutating_and_reset_reports_common(tmp_path):
    from ls.core.application_profiles.service import save_profile, reset_profile
    home = tmp_path / "home"
    write(paths(home, tmp_path)[0], {"application_profiles": [
        {"application": "tmux", "name": "agent-shell", "settings": {"history_limit": 1234}}]})
    saved = save_profile("tmux", "agent-shell", {"mouse": False}, source_root=SOURCE, home=home)
    path = Path(saved["path"])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Conflicting legacy"):
        save_profile("tmux", "agent-shell", {"history_limit": 2000}, source_root=SOURCE, home=home)
    assert path.read_bytes() == before
    reset = reset_profile("tmux", "agent-shell", source_root=SOURCE, home=home)
    assert reset["profile"]["settings"]["history_limit"] == 1234


@pytest.mark.parametrize("shell", [False, True])
def test_common_capture_disabled_is_honored_on_errors(tmp_path, monkeypatch, capsys, shell):
    from ls.core import cli, health
    from ls.core.terminal_ops import command_cli
    from ls.core.terminal_ops.command_service import ShellCommandError
    home = tmp_path / "home"
    write(paths(home, tmp_path)[0], {"installation": {"feedback": {"capture_enabled": False}}})
    monkeypatch.setattr(cli, "record_failure", lambda **_: pytest.fail("captured disabled incident"))
    monkeypatch.setattr(health, "record_failure", lambda **_: pytest.fail("captured disabled incident"))
    if shell:
        class FailingService:
            def __init__(self, *args, **kwargs):
                pass
            def capabilities(self):
                raise ShellCommandError("fixture", "fixture")
        monkeypatch.setattr(command_cli, "ShellCommandService", FailingService)
        assert command_cli.main(["capabilities"], source_root=SOURCE, home=home) == 2
        capsys.readouterr()
    else:
        cli._capture_cli_failure(["--home", str(home), "plan"], ValueError("fixture"))
