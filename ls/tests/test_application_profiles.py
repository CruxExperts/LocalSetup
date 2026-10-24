from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import tarfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from ls.core.application_profiles import (
    ApplicationProfileError,
    list_profiles,
    reset_profile,
    resolve_profile,
    save_profile,
    validate_profile,
)
from ls.core.application_profiles.cli import main as profile_cli
from ls.core.application_profiles.service import MAX_PROFILE_BYTES, load_tmux_runtime_defaults, plan_profile, resolve_tmux_runtime_profile
from ls.core.paths import global_layout


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def test_tmux_runtime_sidecar_creates_once_without_changing_legacy_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy = resolve_profile(source_root=SOURCE_ROOT, home=home)
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolved = list(pool.map(lambda _: resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home), range(2)))
    assert resolved[0]["settings"] == resolved[1]["settings"]
    assert resolved[0]["settings"]["theme"] == load_tmux_runtime_defaults(source_root=SOURCE_ROOT)["settings"]["theme"]
    assert resolved[0]["settings"]["layout"]["canvas_columns"] == 120
    assert resolved[0]["settings"]["layout"]["retained_history_lines"] == 6000
    assert resolved[0]["settings"]["baseline"]["status_rows"] == 1
    path = global_layout(home).localsetup_home / "config/application-profiles/tmux/runtime/agent-shell.json"
    assert path.is_file() and path.stat().st_mode & 0o077 == 0
    assert not path.parent.parent.joinpath("agent-shell.json").exists()
    assert resolve_profile(source_root=SOURCE_ROOT, home=home)["digest"] == legacy["digest"]
    assert {item["pointer"] for result in resolved for item in result["warnings"]} <= {""}


def test_tmux_runtime_bad_leaf_defaults_only_that_leaf_and_preserves_extensions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home)
    path = global_layout(home).localsetup_home / "config/application-profiles/tmux/runtime/agent-shell.json"
    document = json.loads(path.read_text())
    document["settings"]["theme"]["accent"] = "#ff0000"
    document["settings"]["baseline"]["status_interval_seconds"] = 7
    document["settings"]["layout"]["canvas_columns"] = 501
    document["settings"]["layout"]["retained_history_lines"] = 8500
    document["schema_version"] = 2
    document["extensions"] = {"future.optional": {"value": 3}}
    original = json.dumps(document)
    path.write_text(original)
    resolved = resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home)
    assert resolved["settings"]["theme"]["accent"] == load_tmux_runtime_defaults(source_root=SOURCE_ROOT)["settings"]["theme"]["accent"]
    assert resolved["settings"]["baseline"]["status_interval_seconds"] == 7
    assert resolved["settings"]["layout"]["canvas_columns"] == 120
    assert resolved["settings"]["layout"]["retained_history_lines"] == 8500
    assert resolved["extensions"] == document["extensions"]
    assert resolved["schema_version"] == 2
    assert path.read_text() == original
    assert any(item["pointer"] == "/settings/theme/accent" for item in resolved["warnings"])
    assert any(item["pointer"] == "/settings/layout/canvas_columns" for item in resolved["warnings"])
    document["settings"]["layout"]["canvas_columns"] = 200
    path.write_text(json.dumps(document))
    assert resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home)["settings"]["layout"]["canvas_columns"] == 200
    document["required_features"] = ["unknown-feature"]
    path.write_text(json.dumps(document))
    with pytest.raises(ApplicationProfileError, match="unsupported capabilities"):
        resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home)


def test_tmux_runtime_unsafe_symlink_is_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home)
    path = global_layout(home).localsetup_home / "config/application-profiles/tmux/runtime/agent-shell.json"
    outside = tmp_path / "outside.json"
    outside.write_text("private")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ApplicationProfileError):
        resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home)
    assert outside.read_text() == "private"


def test_tmux_runtime_malformed_file_uses_packaged_defaults_without_overwrite(tmp_path: Path) -> None:
    home = tmp_path / "home"
    resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home)
    path = global_layout(home).localsetup_home / "config/application-profiles/tmux/runtime/agent-shell.json"
    path.write_bytes(b"{broken-json")
    resolved = resolve_tmux_runtime_profile(source_root=SOURCE_ROOT, home=home)
    assert resolved["settings"]["baseline"]["status_rows"] == 1
    assert path.read_bytes() == b"{broken-json"
    assert any("malformed" in item["reason"] for item in resolved["warnings"])


def _backup_payload(path: Path, expected_name: str) -> bytes:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [expected_name]
        assert members[0].mode == 0o600
        extracted = archive.extractfile(members[0])
        assert extracted is not None
        return extracted.read()


def test_tmux_defaults_and_private_override_have_stable_origins_and_digest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    defaults = resolve_profile(source_root=SOURCE_ROOT, home=home)
    assert defaults["application"] == "tmux"
    assert defaults["name"] == "agent-shell"
    assert defaults["base_profile"] == "agent-shell"
    assert defaults["settings"] == {
        "mouse": True,
        "history_limit": 20000,
        "status_position": "bottom",
        "status": True,
        "preserve_theme": True,
        "default_lines": 200,
        "max_response_bytes": 262144,
        "log_max_bytes": 67108864,
        "completed_log_retention_days": 7,
        "idle_grace_seconds": 900,
        "collector_interval_seconds": 60,
        "observer_pins": True,
    }
    assert defaults["origins"]["/settings/history_limit"] == "default"
    assert len(defaults["digest"]) == 64
    assert defaults["digest"] == resolve_profile(source_root=SOURCE_ROOT, home=home)["digest"]

    saved = save_profile(
        "tmux",
        "agent-shell",
        {"history_limit": 4096, "mouse": False},
        source_root=SOURCE_ROOT,
        home=home,
    )
    path = global_layout(home).localsetup_home / "config" / "application-profiles" / "tmux" / "agent-shell.json"
    assert saved["path"] == str(path)
    assert saved["backup"] is None
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "application": "tmux",
        "name": "agent-shell",
        "base_profile": "agent-shell",
        "settings": {"history_limit": 4096, "mouse": False},
    }
    assert path.stat().st_mode & 0o777 == 0o600
    resolved = saved["profile"]
    assert resolved["settings"]["history_limit"] == 4096
    assert resolved["settings"]["status_position"] == "bottom"
    assert resolved["origins"]["/settings/history_limit"] == "user"
    assert resolved["origins"]["/settings/status_position"] == "default"

    updated = save_profile(
        "tmux",
        "agent-shell",
        {"history_limit": 8192},
        source_root=SOURCE_ROOT,
        home=home,
    )
    assert updated["backup"] is not None
    backup = Path(updated["backup"])
    assert backup.is_file()
    assert re.fullmatch(
        r"\.agent-shell\.json\.localsetup-backup-\d{8}T\d{6}\.\d{6}Z-[a-f0-9]{32}\.tar\.gz",
        backup.name,
    )
    assert backup.stat().st_mode & 0o777 == 0o600
    assert json.loads(_backup_payload(backup, path.name))["settings"] == {"history_limit": 4096, "mouse": False}
    assert updated["profile"]["settings"]["mouse"] is False

    reset = reset_profile("tmux", "agent-shell", source_root=SOURCE_ROOT, home=home)
    assert reset["changed"] is True
    assert json.loads(_backup_payload(Path(reset["backup"]), path.name))["settings"] == {
        "history_limit": 8192, "mouse": False,
    }
    assert reset["profile"]["settings"]["history_limit"] == 20000
    assert not path.exists()


@pytest.mark.parametrize(
    "application,name,settings,match",
    [
        ("missing-app", "agent-shell", {}, "unknown application"),
        ("tmux", "missing-profile", {}, "unknown profile"),
        ("tmux", "agent-shell", {"command": "tmux set-option -g status off"}, "[Aa]dditional properties"),
        ("tmux", "agent-shell", {"hooks": []}, "[Aa]dditional properties"),
        ("tmux", "agent-shell", {"history_limit": True}, "not of type 'integer'"),
    ],
)
def test_unknown_profiles_and_untyped_or_executable_settings_are_rejected(
    tmp_path: Path,
    application: str,
    name: str,
    settings: dict,
    match: str,
) -> None:
    with pytest.raises(ApplicationProfileError, match=match):
        validate_profile(application, name, settings, source_root=SOURCE_ROOT)


def test_user_override_schema_version_and_unknown_fields_fail_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = global_layout(home).localsetup_home / "config" / "application-profiles" / "tmux" / "agent-shell.json"
    save_profile("tmux", "agent-shell", {"mouse": False}, source_root=SOURCE_ROOT, home=home)
    path.write_text('{"schema_version": 2, "settings": {"mouse": false}}\n', encoding="utf-8")
    with pytest.raises(ApplicationProfileError, match="schema_version"):
        resolve_profile(source_root=SOURCE_ROOT, home=home)

    path.write_text('{"schema_version": 1, "settings": {"format": "#{pane_current_path}"}}\n', encoding="utf-8")
    with pytest.raises(ApplicationProfileError, match="[Aa]dditional properties"):
        resolve_profile(source_root=SOURCE_ROOT, home=home)

    path.write_bytes(b" " * (MAX_PROFILE_BYTES + 1))
    with pytest.raises(ApplicationProfileError, match="exceeds"):
        resolve_profile(source_root=SOURCE_ROOT, home=home)


def test_override_location_is_global_private_layout_and_symlink_escape_fails(tmp_path: Path) -> None:
    home = tmp_path / "home"
    expected_root = global_layout(home).localsetup_home / "config" / "application-profiles"
    saved = save_profile("tmux", "agent-shell", {"mouse": False}, source_root=SOURCE_ROOT, home=home)
    expected_file = expected_root / "tmux" / "agent-shell.json"
    assert Path(saved["path"]) == expected_file
    assert expected_file.is_file()

    other_home = tmp_path / "other-home"
    private_root = global_layout(other_home).localsetup_home / "config" / "application-profiles"
    private_root.mkdir(parents=True)
    for directory in (
        other_home,
        other_home / ".local",
        other_home / ".local/share",
        global_layout(other_home).localsetup_home,
        global_layout(other_home).localsetup_home / "config",
        private_root,
    ):
        directory.chmod(0o700)
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (private_root / "tmux").symlink_to(external_dir, target_is_directory=True)
    with pytest.raises(ApplicationProfileError, match="escapes its root|symlinks"):
        resolve_profile(source_root=SOURCE_ROOT, home=other_home)


def test_registry_reuses_recursive_merge_for_a_second_test_application(tmp_path: Path) -> None:
    source_root = tmp_path / "fixture-source"
    config = source_root / "ls" / "config"
    shutil.copytree(SOURCE_ROOT / "ls" / "config" / "application-profiles", config / "application-profiles")
    for filename in (
        "application-profiles.schema.json",
        "application-profile.schema.json",
        "application-profile-tmux-agent-shell.schema.json",
    ):
        shutil.copy2(SOURCE_ROOT / "ls" / "config" / filename, config / filename)

    registry_path = config / "application-profiles" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["applications"]["testapp"] = {
        "profiles": {
            "nested": {
                "defaults": "application-profiles/testapp/nested.json",
                "settings_schema": "application-profile-testapp-nested.schema.json",
            }
        }
    }
    registry_path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    (config / "application-profile-testapp-nested.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "nested": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                    },
                    "items": {"type": "array", "items": {"type": "integer"}},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    defaults_path = config / "application-profiles" / "testapp" / "nested.json"
    defaults_path.parent.mkdir(parents=True)
    defaults_path.write_text(
        '{"schema_version": 1, "application": "testapp", "name": "nested", '
        '"base_profile": "nested", "settings": {"nested": {"left": 1, "right": 2}, "items": [1, 2]}}\n',
        encoding="utf-8",
    )

    home = tmp_path / "home"
    save_profile("testapp", "nested", {"nested": {"left": 9}, "items": [3]}, source_root=source_root, home=home)
    profile = resolve_profile("testapp", "nested", source_root=source_root, home=home)
    assert profile["settings"] == {"nested": {"left": 9, "right": 2}, "items": [3]}
    assert profile["base_profile"] == "nested"
    assert profile["origins"]["/settings/nested/left"] == "user"
    assert profile["origins"]["/settings/nested/right"] == "default"
    assert profile["origins"]["/settings/items"] == "user"
    rows = list_profiles(source_root=source_root, home=home, application="testapp")
    assert rows == [{
        "application": "testapp", "name": "nested", "base_profile": "nested",
        "has_user_override": True, "digest": profile["digest"],
    }]

    saved_variant = save_profile(
        "testapp", "nested", {"nested": {"right": 8}},
        source_root=source_root, home=home, as_name="nested-variant",
    )
    variant = resolve_profile("testapp", "nested-variant", source_root=source_root, home=home)
    assert variant["base_profile"] == "nested"
    assert variant["settings"] == {"nested": {"left": 1, "right": 8}, "items": [1, 2]}
    assert saved_variant["profile"]["name"] == "nested-variant"
    assert any(row["name"] == "nested-variant" for row in list_profiles(
        source_root=source_root, home=home, application="testapp",
    ))

    from ls.core.application_profiles import native

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(native, "_shell_service", lambda *_args: pytest.fail("non-tmux profile reached native adapter"))
        unsupported = native.apply_tmux_profile(
            variant,
            session_handle="lsshell1.abc.def",
            source_root=source_root,
            state_root=None,
            owner_pid=None,
            owner_starttime=None,
            agent_id=None,
        )
    assert unsupported["ok"] is False
    assert "only for tmux" in unsupported["error"]


def test_named_variants_support_cli_lifecycle_and_preserve_partial_updates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    first = save_profile(
        "tmux", "agent-shell", {"history_limit": 4096},
        source_root=SOURCE_ROOT, home=home, as_name="quiet-shell",
    )
    path = Path(first["path"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope == {
        "schema_version": 1,
        "application": "tmux",
        "name": "quiet-shell",
        "base_profile": "agent-shell",
        "settings": {"history_limit": 4096},
    }
    assert path.stat().st_mode & 0o777 == 0o600
    profile = resolve_profile("tmux", "quiet-shell", source_root=SOURCE_ROOT, home=home)
    assert profile["base_profile"] == "agent-shell"
    assert profile["settings"]["history_limit"] == 4096
    assert profile["settings"]["mouse"] is True

    update = save_profile(
        "tmux", "quiet-shell", {"mouse": False},
        source_root=SOURCE_ROOT, home=home,
    )
    assert update["profile"]["settings"]["history_limit"] == 4096
    assert update["profile"]["settings"]["mouse"] is False
    assert profile_cli(
        ["show", "tmux", "quiet-shell"], source_root=SOURCE_ROOT, home=home,
    ) == 0
    shown = json.loads(capsys.readouterr().out)["profile"]
    assert shown["base_profile"] == "agent-shell"
    assert shown["settings"]["mouse"] is False

    assert profile_cli(
        ["validate", "tmux", "quiet-shell", "--settings-json", '{"mouse": true}'],
        source_root=SOURCE_ROOT,
        home=home,
    ) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["base_profile"] == "agent-shell"
    assert validated["settings"] == {"mouse": True}

    listed = list_profiles(source_root=SOURCE_ROOT, home=home, application="tmux")
    variant_row = next(row for row in listed if row["name"] == "quiet-shell")
    assert variant_row["base_profile"] == "agent-shell"
    assert variant_row["has_user_override"] is True

    assert profile_cli(
        ["reset", "tmux", "quiet-shell"], source_root=SOURCE_ROOT, home=home,
    ) == 0
    reset = json.loads(capsys.readouterr().out)
    assert reset["changed"] is True
    assert reset["profile"]["name"] == "quiet-shell"
    assert reset["profile"]["settings"]["mouse"] is True
    assert json.loads(_backup_payload(Path(reset["backup"]), path.name))["base_profile"] == "agent-shell"
    with pytest.raises(ApplicationProfileError, match="unknown profile"):
        resolve_profile("tmux", "quiet-shell", source_root=SOURCE_ROOT, home=home)


def test_registered_base_identity_cannot_be_swapped_and_paths_bind_document_identity(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    config = source_root / "ls" / "config"
    config.mkdir(parents=True)
    shutil.copytree(SOURCE_ROOT / "ls" / "config" / "application-profiles", config / "application-profiles")
    for filename in (
        "application-profiles.schema.json",
        "application-profile.schema.json",
        "application-profile-tmux-agent-shell.schema.json",
    ):
        shutil.copy2(SOURCE_ROOT / "ls" / "config" / filename, config / filename)
    registry_path = config / "application-profiles" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["applications"]["tmux"]["profiles"]["alternate-shell"] = {
        "defaults": "application-profiles/tmux/alternate-shell.json",
        "settings_schema": "application-profile-tmux-agent-shell.schema.json",
    }
    registry_path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    alternate = json.loads((config / "application-profiles/tmux/agent-shell.json").read_text(encoding="utf-8"))
    alternate.update(name="alternate-shell", base_profile="alternate-shell")
    (config / "application-profiles/tmux/alternate-shell.json").write_text(
        json.dumps(alternate, indent=2) + "\n", encoding="utf-8",
    )

    home = tmp_path / "home"
    save_profile(
        "tmux", "agent-shell", {"mouse": False},
        source_root=source_root, home=home, as_name="quiet-shell",
    )
    with pytest.raises(ApplicationProfileError, match="cannot change its registered base"):
        save_profile(
            "tmux", "alternate-shell", {"status": False},
            source_root=source_root, home=home, as_name="quiet-shell",
        )

    path = global_layout(home).localsetup_home / "config/application-profiles/tmux/quiet-shell.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["name"] = "other-profile"
    path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    with pytest.raises(ApplicationProfileError, match="identity does not match"):
        resolve_profile("tmux", "quiet-shell", source_root=source_root, home=home)


def test_legacy_base_override_migrates_to_bound_envelope_and_defaults_are_complete(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = global_layout(home).localsetup_home / "config/application-profiles/tmux/agent-shell.json"
    save_profile("tmux", "agent-shell", {"mouse": False}, source_root=SOURCE_ROOT, home=home)
    path.write_text('{"schema_version": 1, "settings": {"mouse": false}}\n', encoding="utf-8")
    legacy = resolve_profile(source_root=SOURCE_ROOT, home=home)
    assert legacy["settings"]["mouse"] is False
    save_profile("tmux", "agent-shell", {"status": False}, source_root=SOURCE_ROOT, home=home)
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["application"] == "tmux"
    assert migrated["name"] == "agent-shell"
    assert migrated["base_profile"] == "agent-shell"
    assert migrated["settings"] == {"mouse": False, "status": False}

    source_root = tmp_path / "source"
    config = source_root / "ls/config"
    config.mkdir(parents=True)
    shutil.copytree(SOURCE_ROOT / "ls/config/application-profiles", config / "application-profiles")
    for filename in (
        "application-profiles.schema.json",
        "application-profile.schema.json",
        "application-profile-tmux-agent-shell.schema.json",
    ):
        shutil.copy2(SOURCE_ROOT / "ls/config" / filename, config / filename)
    defaults_path = config / "application-profiles/tmux/agent-shell.json"
    defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    defaults["settings"].pop("observer_pins")
    defaults_path.write_text(json.dumps(defaults) + "\n", encoding="utf-8")
    with pytest.raises(ApplicationProfileError, match="define every registered setting"):
        resolve_profile(source_root=source_root, home=tmp_path / "other-home")


def test_private_profile_ancestors_are_owner_private_and_parallel_partial_saves_merge(tmp_path: Path) -> None:
    home = tmp_path / "home"
    save_profile("tmux", "agent-shell", {}, source_root=SOURCE_ROOT, home=home, as_name="quiet-shell")
    app_dir = global_layout(home).localsetup_home / "config/application-profiles/tmux"
    app_dir.chmod(0o755)
    with pytest.raises(ApplicationProfileError, match="unsafe permissions"):
        resolve_profile("tmux", "quiet-shell", source_root=SOURCE_ROOT, home=home)

    app_dir.chmod(0o700)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                save_profile, "tmux", "quiet-shell", settings,
                source_root=SOURCE_ROOT, home=home,
            )
            for settings in ({"history_limit": 4096}, {"mouse": False})
        ]
        for future in futures:
            future.result()
    result = resolve_profile("tmux", "quiet-shell", source_root=SOURCE_ROOT, home=home)
    assert result["settings"]["history_limit"] == 4096
    assert result["settings"]["mouse"] is False


def test_native_apply_failure_is_reported_and_plan_returns_target_readback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ls.core.application_profiles import native

    home = tmp_path / "home"
    profile = save_profile(
        "tmux", "agent-shell", {"mouse": False},
        source_root=SOURCE_ROOT, home=home, as_name="quiet-shell",
    )["profile"]
    calls: list[dict[str, object]] = []
    target_state = {"native": "ready"}

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def apply_profile(self, handle, settings, digest, **kwargs):
            calls.append({"handle": handle, "settings": settings, "digest": digest, **kwargs})
            return {"ok": False, "state": "cleanup_pending", "cleanup_state": "pending"}

        def status(self, handle, *, owner=None):
            return {
                "ok": True,
                "state": "ready",
                "native_state": target_state["native"],
                "settings_digest": "0" * 64,
                "profile_name": "quiet-shell",
                "profile_base": "agent-shell",
                "profile_settings": profile["settings"],
                "profile_readback": {"mouse": False, "status": True, "status_position": "bottom", "history_limit": {"@0": 20000}},
            }

    monkeypatch.setattr(native, "_shell_service", lambda *_args: FakeService())
    monkeypatch.setattr(native, "_owner_binding", lambda **_kwargs: {"pid": 1})

    rc = profile_cli(
        ["apply", "tmux", "quiet-shell", "--session-handle", "lsshell1.abc.def"],
        source_root=SOURCE_ROOT,
        home=home,
    )
    applied = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert applied["ok"] is False
    assert applied["state"] == "cleanup_pending"
    assert calls[0]["profile_name"] == "quiet-shell"
    assert calls[0]["profile_base"] == "agent-shell"

    rc = profile_cli(
        ["plan", "tmux", "agent-shell", "--session-handle", "lsshell1.abc.def"],
        source_root=SOURCE_ROOT,
        home=home,
    )
    planned = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert planned["plan_scope"] == "native-target"
    assert planned["target_readback"]["mouse"] is False
    assert planned["target_readback_complete"] is True
    assert planned["native_adapter_registered"] is True
    assert planned["diff"]["mouse"]["changed"] is True
    assert planned["native_diff"]["history_limit"]["requested"] == 20000
    assert planned["native_diff"]["mouse"]["changed"] is True

    target_state["native"] = "unknown"
    assert profile_cli(
        ["plan", "tmux", "agent-shell", "--session-handle", "lsshell1.abc.def"],
        source_root=SOURCE_ROOT,
        home=home,
    ) == 2
    unverified = json.loads(capsys.readouterr().out)
    assert unverified["ok"] is False
    assert "verified ready state" in unverified["error"]


def test_cli_apply_requires_an_explicit_native_adapter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planned = plan_profile(source_root=SOURCE_ROOT, home=tmp_path / "home")
    assert planned["native_adapter_registered"] is True
    assert planned["plan_scope"] == "profile-only"
    assert planned["actions"] == [{"kind": "apply_profile_settings", "status": "planned"}]

    assert profile_cli(["apply"], source_root=SOURCE_ROOT, home=tmp_path / "home") == 2
    assert json.loads(capsys.readouterr().out)["error"] == "native application apply requires --session-handle"

    received: list[tuple[str, str, dict]] = []

    def apply(application: str, name: str, settings: dict) -> dict:
        received.append((application, name, settings))
        return {"applied": True}

    assert profile_cli(["apply"], source_root=SOURCE_ROOT, home=tmp_path / "home", apply_callback=apply) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"] == {"applied": True}
    assert received[0][0:2] == ("tmux", "agent-shell")
    assert received[0][2]["mouse"] is True
