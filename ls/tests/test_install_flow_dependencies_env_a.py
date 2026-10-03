from __future__ import annotations

from ls.tests.test_install_flow import *

def test_doctor_reports_manifest_and_environment_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ls.core.doctor as doctor_mod

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()

    monkeypatch.setattr(doctor_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(doctor_mod, "load_pack_config", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    manifest_failure = run_doctor(root, home=home)
    assert manifest_failure["ok"] is False
    assert "native Windows is unsupported" in manifest_failure["blockers"][0]
    assert any("manifest validation failed: boom" in blocker for blocker in manifest_failure["blockers"])

    fake_pack = SimpleNamespace(
        pack_id="localsetup",
        global_root="~/.local/share/localsetup/packages",
        global_registry="~/.local/share/localsetup/registry.json",
    )
    fake_platform = SimpleNamespace(platform_id="codex")
    monkeypatch.setattr(doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor_mod, "load_pack_config", lambda *args, **kwargs: fake_pack)
    monkeypatch.setattr(doctor_mod, "load_platforms", lambda *args, **kwargs: [fake_platform])
    monkeypatch.setattr(doctor_mod, "validate_skill_catalog", lambda *args, **kwargs: ["bad skill"])
    monkeypatch.setattr(doctor_mod, "validate_workflow_catalog", lambda *args, **kwargs: ["bad workflow"])
    monkeypatch.setattr(doctor_mod, "tool_status", lambda name: {"name": name, "ok": False})
    monkeypatch.setattr(
        doctor_mod,
        "dependency_status",
        lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "mode": "uv-sync",
                "warnings": ["dependency warning"],
                "blockers": ["dependency blocker"],
            }
        ),
    )
    monkeypatch.setattr(
        doctor_mod,
        "adapter_status",
        lambda *args, **kwargs: [
            {
                "platform": "codex",
                "repo_path": str(target / ".codex" / "skills"),
                "collision_reason": "regular file",
                "package_integrity_failures": [{"package": "ls-context"}],
            }
        ],
    )
    monkeypatch.setattr(doctor_mod, "adapter_targets", lambda *args, **kwargs: [{"repo_path": target / ".codex" / "skills"}])
    monkeypatch.setattr(doctor_mod, "_writable_status", lambda path: {"path": str(path), "nearest_existing": str(path), "ok": False})
    monkeypatch.setattr(doctor_mod, "detect_legacy_artifacts", lambda *args, **kwargs: [{"kind": "legacy"}])
    monkeypatch.setattr(doctor_mod, "scan_legacy_references", lambda *args, **kwargs: [])
    monkeypatch.setattr(doctor_mod, "provenance_report", lambda *args, **kwargs: {"warnings": ["prov"], "repair_hints": ["hint"]})
    monkeypatch.setattr(doctor_mod, "install_inventory", lambda *args, **kwargs: {"inventory": []})

    result = run_doctor(root, home=home, platform_ids=["codex"], target_root=target)

    assert result["ok"] is False
    assert any("skill catalog: bad skill" in blocker for blocker in result["blockers"])
    assert any("workflow catalog: bad workflow" in blocker for blocker in result["blockers"])
    assert "missing required tool: git" in result["blockers"]
    assert "missing recommended tool: rg" in result["warnings"]
    assert "dependency blocker" in result["blockers"]
    assert "dependency warning" in result["warnings"]
    assert any("adapter collision (regular file)" in blocker for blocker in result["blockers"])
    assert any("adapter package target mismatch (ls-context)" in blocker for blocker in result["blockers"])
    assert any("path is not writable" in blocker for blocker in result["blockers"])
    assert any("legacy artifacts detected" in warning for warning in result["warnings"])


def test_cli_install_passes_configured_data_root_to_uv_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    data_root = tmp_path / "runtime-root"
    config_path = tmp_path / "install.json"
    config_path.write_text(
        json.dumps(
            {
                "platforms": [],
                "packs": ["core"],
                "data_root": str(data_root),
                "dependency_mode": "managed-venv",
            }
        ),
        encoding="utf-8",
    )
    captured: list[Path | None] = []

    def fake_ensure_dependencies(
        repo_root: Path,
        *,
        mode: str,
        data_root: Path | None = None,
        target_root: Path | None = None,
        runner: object | None = None,
    ) -> dict:
        captured.append(data_root)
        assert repo_root == root
        assert mode == "uv-sync"
        assert data_root is not None
        interpreter = root / ".venv" / "bin" / "python"
        return {
            "mode": mode,
            "interpreter": str(interpreter),
            "dependency_manager": "uv",
            "project_root": str(root),
            "pyproject": str(root / "pyproject.toml"),
            "lockfile": str(root / "uv.lock"),
            "environment_path": str(root / ".venv"),
            "lock": {"dependency_manager": "uv", "lock_status": "current"},
            "changed": False,
            "warnings": [],
            "blockers": [],
            "commands": [],
            "ok": True,
        }

    monkeypatch.setattr(cli_mod, "ensure_dependencies", fake_ensure_dependencies)

    rc = cli_mod._main(
        [
            "--repo",
            str(root),
            "--home",
            str(home),
            "install",
            "--config",
            str(config_path),
            "--yes",
        ]
    )

    lock = load_json(root / ".localsetup" / "lock.json")
    assert rc == 0
    assert captured == [data_root.resolve()]
    assert lock["python_interpreter"] == str(root / ".venv" / "bin" / "python")
    assert lock["dependency_state"]["dependency_manager"] == "uv"


def test_uv_sync_commands_and_lock_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("LOCALSETUP_UV_BIN", "uv")
    commands: list[list[str]] = []

    def fake_runner(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        if cmd[-1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.11.5\n", stderr="")
        if "sync" in cmd:
            python_path = root / ".venv" / "bin" / "python"
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("# fake python\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    deps = ensure_dependencies(root, mode="managed-venv", data_root=home / ".local/share/localsetup", runner=fake_runner)
    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    result = apply_plan(root, plan, home=home, dependency_info=deps)
    lock = load_json(root / ".localsetup/lock.json")

    assert any(cmd[-2:] == ["lock", "--check"] for cmd in commands)
    assert any("sync" in cmd and "--locked" in cmd and "--no-dev" in cmd for cmd in commands)
    assert deps["mode"] == "uv-sync"
    assert deps["interpreter"].endswith(".venv/bin/python")
    assert deps["lock"]["dependency_manager"] == "uv"
    assert deps["lock"]["lock_status"] == "current"
    assert lock["python_interpreter"] == deps["interpreter"]
    assert lock["dependency_state"]["dependency_manager"] == "uv"
    assert result["lockfile"].endswith(".localsetup/lock.json")


def test_uv_prompt_only_reports_lock_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    monkeypatch.setenv("LOCALSETUP_UV_BIN", "uv")

    def fake_runner(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[-1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.11.5\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    deps = ensure_dependencies(root, mode="prompt-only", runner=fake_runner)

    assert deps["changed"] is False
    assert deps["lock_status"] == "current"
    assert deps["lock"]["dependency_manager"] == "uv"


def test_doctor_reports_corrupt_legacy_global_venv_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ls.core.dependencies as deps_mod
    import ls.core.doctor as doctor_mod

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    data_root = home / ".local" / "share" / "localsetup"
    legacy_python = data_root / "venv" / "bin" / "python"
    legacy_python.parent.mkdir(parents=True)
    legacy_python.write_text("# fake python\n", encoding="utf-8")
    legacy_python.chmod(0o644)

    def fake_runner(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[-1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.11.5\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setenv("LOCALSETUP_UV_BIN", "uv")
    monkeypatch.setattr(
        doctor_mod,
        "dependency_status",
        lambda repo_root, **kwargs: deps_mod.dependency_status(repo_root, runner=fake_runner, **kwargs),
    )

    prompt_only = run_doctor(root, home=home, dependency_mode="prompt-only", data_root=data_root)
    default_mode = run_doctor(root, home=home, data_root=data_root)

    for result in (prompt_only, default_mode):
        assert result["dependencies"]["mode"] == "prompt-only"
        assert result["dependencies"]["blockers"] == []
        assert not any("Permission denied" in blocker for blocker in result["blockers"])
        assert any("legacy global venv interpreter is not executable" in warning for warning in result["warnings"])
        legacy_environment = result["dependencies"]["legacy_environment"]
        assert legacy_environment["ignored"] is True
        assert legacy_environment["ok"] is False
        assert legacy_environment["interpreter"] == str(legacy_python)
        assert any("Remove or quarantine" in step for step in result["dependencies"]["recoverable_next_steps"])
    assert legacy_python.read_text(encoding="utf-8") == "# fake python\n"


def test_doctor_envman_probe_is_opt_in_and_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ls.core.envman as envman_mod

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    calls: list[bool] = []

    def fake_probe() -> dict:
        calls.append(True)
        return {"tool": "envman", "status": "unavailable", "code": "executable-missing"}

    monkeypatch.setattr(envman_mod, "probe_status", fake_probe)

    baseline = run_doctor(root, home=home)
    opted_in = run_doctor(root, home=home, envman=True)

    assert "envman" not in baseline
    assert calls == [True]
    assert opted_in["envman"]["status"] == "unavailable"
    assert opted_in["ok"] == baseline["ok"]


def test_uv_already_synced_skips_nested_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    monkeypatch.setenv("LOCALSETUP_UV_BIN", "uv")
    monkeypatch.setenv("LOCALSETUP_UV_ALREADY_SYNCED", "1")
    commands: list[list[str]] = []

    def fake_runner(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        if cmd[-1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.11.5\n", stderr="")
        assert "sync" not in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    deps = ensure_dependencies(root, mode="uv-sync", runner=fake_runner)

    assert deps["sync_status"] == "success"
    assert deps["changed"] is False
    assert any(cmd[-2:] == ["lock", "--check"] for cmd in commands)
    assert not any("sync" in cmd for cmd in commands)
