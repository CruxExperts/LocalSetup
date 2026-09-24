from __future__ import annotations

from ls.tests.test_install_flow import *

def test_no_selector_plan_install_and_update_infer_existing_modern_repo(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    plan = build_install_plan(
        root,
        home=home,
        global_preset="suggested",
        repo_preset="custom",
        repo_skills=["ls-context"],
        platform_ids=["codex"],
        target_root=target,
    )
    apply_plan(root, plan, home=home, target_root=target)

    planned = run_localsetup_cli(root, home, "plan", "--target-directory", str(target))
    assert planned.returncode == 0, planned.stderr
    plan_payload = json.loads(planned.stdout)
    assert plan_payload["auto_mode"] == "inferred_existing"
    assert plan_payload["attachment"]["platforms"] == ["codex"]
    assert plan_payload["rollback"]["repo_packages"] == [
        "ls-context",
        "ls-safety-and-backup",
        "ls-workflow-openpgp-lifecycle",
        "ls-workflow-ops-tmux-session",
        "ls-workflow-tmux-terminal-mode",
    ]

    installed = run_localsetup_cli(root, home, "install", "--target-directory", str(target), "--apply")
    assert installed.returncode == 0, installed.stderr
    install_payload = json.loads(installed.stdout)
    assert install_payload["auto_mode"] == "inferred_existing"
    assert_scoped_adapter(target / ".agents" / "skills", "ls-context")

    updated = run_localsetup_cli(root, home, "update", "--target-directory", str(target))
    assert updated.returncode == 0, updated.stderr
    update_payload = json.loads(updated.stdout)
    lock = load_json(target / ".localsetup" / "lock.json")
    assert update_payload["auto_mode"] == "inferred_existing"
    assert lock["platforms"] == ["codex"]
    assert lock["repo_packages"] == [
        "ls-context",
        "ls-safety-and-backup",
        "ls-workflow-openpgp-lifecycle",
        "ls-workflow-ops-tmux-session",
        "ls-workflow-tmux-terminal-mode",
    ]
    assert lock["global_baseline_selectors"]["preset"] == "suggested"


def test_no_selector_install_repairs_legacy_lockfile(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "legacy-target"
    target.mkdir()
    (target / "localsetup.lock.json").write_text(
        json.dumps({"platforms": ["codex"], "repo_packages": ["localsetup-context"]}) + "\n",
        encoding="utf-8",
    )

    completed = run_localsetup_cli(root, home, "install", "--target-directory", str(target), "--apply")
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stderr
    assert payload["auto_mode"] == "repair_required"
    assert payload["applied"] is True
    assert not (target / "localsetup.lock.json").exists()
    assert (target / ".localsetup" / "lock.json").is_file()
    assert_scoped_adapter(target / ".agents" / "skills", "ls-context")


def test_no_selector_update_requires_decision_for_unknown_broken_adapter(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    adapter = target / ".codex" / "skills"
    adapter.parent.mkdir(parents=True)
    adapter.symlink_to(target / "missing-global", target_is_directory=True)

    completed = run_localsetup_cli(root, home, "update", "--target-directory", str(target))
    payload = json.loads(completed.stdout)

    assert completed.returncode == 1, completed.stderr
    assert payload["auto_mode"] == "repair_required"
    assert payload["applied"] is False
    assert any(decision["kind"] == "adapter_collision" for decision in payload["decisions"])
    assert adapter.is_symlink()


def test_no_selector_install_preserves_benign_adapter_file(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    collision = target / ".codex" / "skills"
    collision.mkdir(parents=True)
    (collision / "custom.txt").write_text("user content\n", encoding="utf-8")

    completed = run_localsetup_cli(root, home, "install", "--target-directory", str(target), "--apply")
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stderr
    assert payload["auto_mode"] == "repair_required"
    assert payload["decisions"] == []
    assert (collision / "custom.txt").is_file()
    assert not (collision / "ls-context").exists()
    assert_scoped_adapter(target / ".agents" / "skills", "ls-context")
    assert (target / ".localsetup" / "lock.json").exists()


def test_no_selector_install_new_repo_uses_normal_global_without_adapters(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "new-target"
    (target / ".github" / "workflows").mkdir(parents=True)
    (target / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (target / "package.json").write_text("{}", encoding="utf-8")

    completed = run_localsetup_cli(root, home, "install", "--target-directory", str(target), "--apply")
    payload = json.loads(completed.stdout)
    lock = load_json(target / ".localsetup" / "lock.json")

    assert completed.returncode == 0, completed.stderr
    assert payload["auto_mode"] == "default_new_repo"
    assert lock["global_only"] is True
    assert lock["platforms"] == []
    assert lock["repo_packages"] == []
    assert lock["global_baseline_selectors"]["preset"] == "normal"
    assert lock["global_baseline_packs"] == [
        "bootstrap",
        "core",
        "dev",
        "frontend",
        "architecture",
        "ops",
        "publishing",
    ]
    assert "publishing" in lock["global_baseline_packs"]
    assert "ls-nodejs-nextjs" in lock["global_baseline_packages"]
    assert not (target / ".codex" / "skills").exists()


def test_explicit_selectors_bypass_no_selector_auto_mode(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()

    completed = run_localsetup_cli(
        root,
        home,
        "install",
        "--target-directory",
        str(target),
        "--platforms",
        "codex",
        "--apply",
    )
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stderr
    assert payload["auto_mode"] == "explicit"
    assert payload["attachment"]["platforms"] == ["codex"]
    assert_scoped_adapter(target / ".agents" / "skills", "ls-context")


def test_no_selector_install_protected_source_checkout_allows_safe_refresh(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "maintainer"
    shutil.copytree(root, target)
    adapter = target / ".codex" / "skills"
    adapter.mkdir(parents=True)
    (adapter / "README.md").write_text("repo note\n", encoding="utf-8")

    completed = run_localsetup_cli(root, home, "install", "--target-directory", str(target), "--apply")
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stderr
    assert payload["auto_mode"] == "repair_required"
    assert payload["decisions"] == []
    assert (target / "ls" / "config" / "pack.yaml").is_file()
    assert (adapter / "README.md").is_file()
    assert not (adapter / "ls-context").exists()
    assert_scoped_adapter(target / ".agents" / "skills", "ls-context")
    assert (target / ".localsetup" / "lock.json").exists()


def test_convert_cli_accepts_split_selector_flags(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    tool = root / "ls" / "tools" / "localsetup.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(tool),
            "--repo",
            str(root),
            "--home",
            str(home),
            "--target-directory",
            str(target),
            "convert",
            "--platforms",
            "codex",
            "--global-packs",
            "dev",
            "--repo-packs",
            "core",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["applied"] is False


def test_convert_does_not_copy_framework_source(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    (root / "ls" / "docs" / "local-context").mkdir(parents=True)
    (root / "ls" / "docs" / "local-context" / "SECRETS.md").write_text("secret\n", encoding="utf-8")
    skill_data = root / "ls" / "skills" / "ls-context" / "scripts" / "data"
    skill_data.mkdir(parents=True)
    (skill_data / "runtime.txt").write_text("runtime\n", encoding="utf-8")
    workflow_data = root / "ls" / "workflows" / "ls-workflow-pipeline-repo-convert" / "scripts" / "data"
    workflow_data.mkdir(parents=True)
    (workflow_data / "runtime.txt").write_text("runtime\n", encoding="utf-8")

    report = convert_repo(root, home=home, platform_ids=["codex"], target_root=target, apply=True)

    assert report["ok"] is True
    assert report["framework_source"]["copied"] is False
    assert not (target / "ls").exists()


def test_convert_uv_sync_uses_source_checkout_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "custom-home"
    target = tmp_path / "target"
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

    def fake_ensure_dependencies(
        repo_root: Path,
        *,
        mode: str,
        data_root: Path | None = None,
        target_root: Path | None = None,
        runner: object | None = None,
    ) -> dict:
        return ensure_dependencies(repo_root, mode=mode, data_root=data_root, target_root=target_root, runner=fake_runner)

    monkeypatch.setattr(conversion_mod, "ensure_dependencies", fake_ensure_dependencies)

    report = convert_repo(
        root,
        home=home,
        packs=["core"],
        platform_ids=["codex"],
        target_root=target,
        dependency_mode="uv-sync",
        apply=True,
    )

    interpreter = report["install"]["dependencies"]["interpreter"]
    assert report["ok"] is True
    assert any("sync" in cmd and "--locked" in cmd for cmd in commands)
    assert interpreter.endswith(".venv/bin/python")
    assert str(root) in interpreter


def test_convert_late_migration_blocker_does_not_remove_target_framework(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    old_framework = target / "ls"
    old_framework.mkdir(parents=True)
    (old_framework / "OLD.txt").write_text("legacy\n", encoding="utf-8")
    legacy = home / ".local/share/localsetup/packages/localsetup-context"
    legacy.mkdir(parents=True)
    (legacy / ".localsetup-managed").write_text("source=localsetup-context\n", encoding="utf-8")
    collision = home / ".local/share/localsetup/packages/ls-context"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("unmanaged\n", encoding="utf-8")

    report = convert_repo(root, home=home, platform_ids=["codex"], target_root=target, apply=True)

    assert report["ok"] is False
    assert any(blocker["kind"] == "global_skill_collision" for blocker in report["blockers"])
    assert (old_framework / "OLD.txt").is_file()
    assert not (target / ".codex" / "skills").exists()


def test_hook_gate_accepts_mock_runner(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    runner = tmp_path / "mock_runner.sh"
    runner.write_text("#!/usr/bin/env bash\nprintf '{\"ok\": true}\\n'\n", encoding="utf-8")
    runner.chmod(0o755)

    gate = run_maintainer_gate(root, tmp_path / "artifact.tar.gz", runner=str(runner))

    assert gate["ok"] is True
    assert gate["agent_runner"]["returncode"] == 0
    assert gate["agent_runner"]["json"] == {"ok": True}


def test_refuses_unmanaged_skill_collision(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    collision = home / ".local/share/localsetup/packages/ls-context"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("unmanaged\n", encoding="utf-8")

    plan = build_install_plan(root, home=home, packs=["core"])
    try:
        apply_plan(root, plan, home=home, dry_run=False)
    except RuntimeError as exc:
        assert "unmanaged package path" in str(exc)
    else:
        raise AssertionError("expected unmanaged collision to fail")


def test_failed_apply_marks_journal_and_cleans_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"])
    original_replace = apply_mod._same_filesystem_replace

    def fail_first_replace(src: Path, dest: Path) -> None:
        if dest.name == "ls-context":
            raise OSError("simulated replace failure")
        original_replace(src, dest)

    monkeypatch.setattr(apply_mod, "_same_filesystem_replace", fail_first_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        apply_plan(root, plan, home=home, dry_run=False)

    journals = sorted((root / ".localsetup" / "install-journal").glob("*.json"))
    assert journals
    journal = load_json(journals[-1])
    assert journal["status"] == "failed"
    assert "simulated replace failure" in journal["error"]
    for item in journal["touched"]:
        if item.get("kind") == "staging_root":
            assert not Path(item["staging_root"]).exists()


def test_failed_package_promotion_restores_existing_managed_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"])
    apply_plan(root, plan, home=home, dry_run=False)
    installed = home / ".local/share/localsetup/packages/ls-context"
    original_skill_md = installed / "SKILL.md"
    original_text = original_skill_md.read_text(encoding="utf-8")
    original_replace = apply_mod._same_filesystem_replace

    def fail_promotion(src: Path, dest: Path) -> None:
        if src.name == "ls-context" and src.parent.name == "skills" and dest.name == "ls-context":
            raise OSError("simulated promotion failure")
        original_replace(src, dest)

    monkeypatch.setattr(apply_mod, "_same_filesystem_replace", fail_promotion)

    with pytest.raises(OSError, match="simulated promotion failure"):
        apply_plan(root, plan, home=home, dry_run=False)

    assert installed.is_dir()
    assert original_skill_md.read_text(encoding="utf-8") == original_text
    assert not list((home / ".local/share/localsetup/packages").glob(".ls-context.localsetup-backup-*"))
    journals = sorted((root / ".localsetup" / "install-journal").glob("*.json"))
    assert load_json(journals[-1])["status"] == "failed"


def test_failed_late_commit_restores_packages_and_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"])
    apply_plan(root, plan, home=home, dry_run=False)
    installed = home / ".local/share/localsetup/packages/ls-context"
    original_text = (installed / "SKILL.md").read_text(encoding="utf-8")
    registry_path = home / ".local/share/localsetup/registry.json"
    original_registry = registry_path.read_text(encoding="utf-8")
    (root / "ls" / "skills" / "ls-context" / "SKILL.md").write_text(
        "---\nname: ls-context\ndescription: Changed.\n---\nchanged\n",
        encoding="utf-8",
    )
    original_save_json = apply_mod.save_json

    def fail_lock_save(path: Path, payload: dict) -> None:
        if path.name == "lock.json" and path.parent.name == ".localsetup":
            raise OSError("simulated lockfile failure")
        original_save_json(path, payload)

    monkeypatch.setattr(apply_mod, "save_json", fail_lock_save)

    with pytest.raises(OSError, match="simulated lockfile failure"):
        apply_plan(root, plan, home=home, dry_run=False)

    assert (installed / "SKILL.md").read_text(encoding="utf-8") == original_text
    assert registry_path.read_text(encoding="utf-8") == original_registry
    journals = sorted((root / ".localsetup" / "install-journal").glob("*.json"))
    assert load_json(journals[-1])["status"] == "failed"


def test_failed_adapter_replace_restores_existing_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"], attach_mode="portable", platform_ids=["codex"])
    apply_plan(root, plan, home=home, dry_run=False)
    adapter = root / ".agents" / "skills"
    existing_note = adapter / "existing.txt"
    existing_note.write_text("keep me\n", encoding="utf-8")
    original_copytree = apply_mod.shutil.copytree

    def fail_adapter_copy(src: Path, dst: Path, *args: object, **kwargs: object):
        if Path(dst).parent == adapter:
            raise OSError("simulated adapter copy failure")
        return original_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(apply_mod.shutil, "copytree", fail_adapter_copy)

    with pytest.raises(OSError, match="simulated adapter copy failure"):
        apply_plan(root, plan, home=home, dry_run=False)

    assert (adapter / ".localsetup-portable").is_file()
    assert existing_note.read_text(encoding="utf-8") == "keep me\n"
    journals = sorted((root / ".localsetup" / "install-journal").glob("*.json"))
    assert load_json(journals[-1])["status"] == "failed"


def test_failed_codex_transition_restores_legacy_managed_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    legacy_root = home / ".local" / "share" / "agents" / "skills" / "localsetup"
    legacy_root.mkdir(parents=True)
    legacy = root / ".codex" / "skills"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(legacy_root, target_is_directory=True)
    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])

    def fail_new_adapter(*args: object, **kwargs: object) -> None:
        raise OSError("simulated post-transition adapter failure")

    monkeypatch.setattr(apply_mod, "_write_scoped_adapter", fail_new_adapter)

    with pytest.raises(OSError, match="post-transition"):
        apply_plan(root, plan, home=home, dry_run=False)

    assert legacy.is_symlink()
    assert legacy.resolve(strict=False) == legacy_root.resolve(strict=False)
    assert not (root / ".agents" / "skills").exists()
    journals = sorted((root / ".localsetup" / "install-journal").glob("*.json"))
    journal = load_json(journals[-1])
    assert journal["status"] == "failed"
    assert any(item.get("transition") == "codex-skills-v1" for item in journal["touched"])


def test_restore_missing_required_backup_fails_before_deleting_live_path(tmp_path: Path) -> None:
    from ls.core.apply_journal import restore_failed_mutations

    live = tmp_path / "adapter"
    live.mkdir()
    (live / "new-state").write_text("preserve\n", encoding="utf-8")
    missing = tmp_path / "missing-backup"

    with pytest.raises(RuntimeError, match="required backup is missing"):
        restore_failed_mutations(
            {"touched": [{"kind": "adapter", "path": str(live), "backup": str(missing), "existed": True}]},
            os.replace,
        )

    assert (live / "new-state").read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize("node_kind", ["regular", "fifo"])
def test_unsupported_historical_adapter_node_blocks_before_journal(tmp_path: Path, node_kind: str) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    historical = root / ".codex" / "skills"
    historical.parent.mkdir(parents=True)
    if node_kind == "regular":
        historical.write_text("preserve\n", encoding="utf-8")
    else:
        os.mkfifo(historical)

    with pytest.raises(RuntimeError, match="unsupported_historical_adapter_node"):
        apply_plan(root, build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"]), home=home)

    assert historical.exists()
    assert not (root / ".localsetup" / "install-journal").exists()


def test_historical_transition_backup_failure_records_no_unrestorable_touch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    historical = root / ".codex" / "skills"
    historical.mkdir(parents=True)
    (historical / ".localsetup-adapter.json").write_text(
        json.dumps({"version": 1, "mode": "symlink", "packages": []}) + "\n",
        encoding="utf-8",
    )
    original_copytree = apply_mod.shutil.copytree

    def fail_historical_backup(src: Path, dst: Path, *args: object, **kwargs: object):
        if Path(src) == historical and ".localsetup-backup-" in Path(dst).name:
            raise OSError("simulated historical backup failure")
        return original_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(apply_mod.shutil, "copytree", fail_historical_backup)

    with pytest.raises(OSError, match="historical backup failure"):
        apply_plan(root, build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"]), home=home)

    assert historical.is_dir()
    journal = load_json(sorted((root / ".localsetup" / "install-journal").glob("*.json"))[-1])
    assert not any(item.get("transition") == "codex-skills-v1" for item in journal["touched"])


def test_apply_rollback_restores_earlier_mutation_and_preserves_later_live_path_when_backup_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex", "claude-code"])
    apply_plan(root, plan, home=home)
    earlier = root / ".agents" / "skills"
    later = root / ".claude" / "skills"
    original_backup = apply_mod._copy_backup
    original_write = apply_mod._write_scoped_adapter

    def lose_later_backup(path: Path, backup: Path) -> None:
        original_backup(path, backup)
        if path == later:
            apply_mod._remove_path(backup)

    def fail_after_later_mutation(path: Path, *args: object, **kwargs: object) -> None:
        original_write(path, *args, **kwargs)
        (path / "transaction-sentinel").write_text("live\n", encoding="utf-8")
        if path == later:
            raise OSError("initiating later adapter failure")

    monkeypatch.setattr(apply_mod, "_copy_backup", lose_later_backup)
    monkeypatch.setattr(apply_mod, "_write_scoped_adapter", fail_after_later_mutation)

    with pytest.raises(OSError, match="initiating later adapter failure") as raised:
        apply_plan(root, plan, home=home)

    assert not (earlier / "transaction-sentinel").exists()
    assert (later / "transaction-sentinel").read_text(encoding="utf-8") == "live\n"
    assert any("required backup is missing" in note for note in getattr(raised.value, "__notes__", []))
    journal = load_json(sorted((root / ".localsetup" / "install-journal").glob("*.json"))[-1])
    assert any("required backup is missing" in error for error in journal["rollback_errors"])


@pytest.mark.parametrize("failure_path", ["action", "receipt"])
def test_failed_journal_persistence_never_replaces_initiating_apply_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_path: str
) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    failed_payloads: list[dict] = []
    original_write_journal = apply_mod._write_journal
    original_save_json = apply_mod.save_json

    def fail_failed_journal(path: Path, payload: dict) -> None:
        if payload.get("status") == "failed":
            failed_payloads.append(json.loads(json.dumps(payload)))
            raise OSError("permanent failed-journal persistence failure")
        original_write_journal(path, payload)

    monkeypatch.setattr(apply_mod, "_write_journal", fail_failed_journal)
    if failure_path == "action":
        monkeypatch.setattr(
            apply_mod,
            "_write_scoped_adapter",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("initiating action failure")),
        )
        expected = "initiating action failure"
    else:
        lock_path = root / ".localsetup" / "lock.json"

        def fail_lock_save(path: Path, payload: dict) -> None:
            if Path(path) == lock_path:
                raise OSError("initiating receipt failure")
            original_save_json(path, payload)

        monkeypatch.setattr(apply_mod, "save_json", fail_lock_save)
        expected = "initiating receipt failure"

    with pytest.raises(OSError, match=expected) as raised:
        apply_plan(root, plan, home=home)

    assert any("failed to persist failed transaction journal" in note for note in getattr(raised.value, "__notes__", []))
    assert len(failed_payloads) == 2
    assert failed_payloads[-1]["journal_persistence_errors"]


@pytest.mark.parametrize("failure_phase", ["prepare", "journal", "unlink", "post_unlink"])
def test_legacy_lock_archive_phase_failure_preserves_or_restores_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    legacy = root / "localsetup.lock.json"
    legacy_bytes = b'{"legacy": "exact-bytes"}\n'
    legacy.write_bytes(legacy_bytes)
    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    original_prepare = apply_mod._prepare_legacy_lockfile_backup
    original_write = apply_mod._write_journal
    original_unlink = apply_mod._remove_legacy_lockfile
    original_save = apply_mod.save_json

    if failure_phase == "prepare":
        monkeypatch.setattr(
            apply_mod,
            "_prepare_legacy_lockfile_backup",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("prepare phase failure")),
        )
    elif failure_phase == "journal":
        failed = False

        def fail_transition_journal(path: Path, payload: dict) -> None:
            nonlocal failed
            has_legacy_touch = any(item.get("kind") == "legacy_lockfile" for item in payload.get("touched", []))
            if has_legacy_touch and payload.get("status") == "started" and not failed:
                failed = True
                raise OSError("journal phase failure")
            original_write(path, payload)

        monkeypatch.setattr(apply_mod, "_write_journal", fail_transition_journal)
    elif failure_phase == "unlink":
        monkeypatch.setattr(
            apply_mod,
            "_remove_legacy_lockfile",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unlink phase failure")),
        )
    else:
        lock_path = root / ".localsetup" / "lock.json"

        def fail_after_unlink(path: Path, payload: dict) -> None:
            if Path(path) == lock_path:
                raise OSError("post-unlink phase failure")
            original_save(path, payload)

        monkeypatch.setattr(apply_mod, "save_json", fail_after_unlink)

    with pytest.raises(OSError, match=failure_phase.replace("post_unlink", "post-unlink")):
        apply_plan(root, plan, home=home)

    assert legacy.read_bytes() == legacy_bytes
