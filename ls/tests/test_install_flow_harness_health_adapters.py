from __future__ import annotations

from ls.tests.test_install_flow import *

def test_harness_helpers_error_and_cron_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core import harness

    repo = tmp_path / "repo"
    target = tmp_path / "target"
    repo.mkdir()
    target.mkdir()

    monkeypatch.setattr(harness.importlib.util, "spec_from_file_location", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="unable to load heartbeat runtime"):
        harness._load_runtime(repo)

    missing = target / "missing.yaml"
    assert harness._read_yaml(missing) == {}
    empty = target / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert harness._read_yaml(empty) == {}
    bad = target / "bad.yaml"
    bad.write_text("- nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML root must be a mapping"):
        harness._read_yaml(bad)

    existing = target / "existing.txt"
    existing.write_text("old", encoding="utf-8")
    assert harness._write_text_if_missing(existing, "new") is False
    with pytest.raises(ValueError, match="range 1..1440"):
        harness._interval_schedule(0)
    assert harness._interval_schedule(60) == "0 */1 * * *"

    monkeypatch.setattr(harness.shutil, "which", lambda name: "/usr/bin/localsetup")
    assert harness._heartbeat_command(repo, target)[0] == "/usr/bin/localsetup"
    monkeypatch.setattr(harness.shutil, "which", lambda name: None)
    assert harness._heartbeat_command(repo, target)[0] == sys.executable

    monkeypatch.setattr(harness, "validate_cron_manifest", lambda *args, **kwargs: None)
    cron = harness._upsert_cron_manifest(
        repo,
        target,
        {"heartbeat": {"interval_minutes": 30}},
        enabled=True,
    )
    assert cron["summary"]["task"]["enabled"] is True
    cron = harness._upsert_cron_manifest(
        repo,
        target,
        {"heartbeat": {"interval_minutes": 30}},
        enabled=False,
    )
    assert cron["summary"]["task"]["enabled"] is False

    with pytest.raises(RuntimeError, match="requires --install-crontab and --yes"):
        harness._install_live_crontab(repo, target, yes=False)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "crontab":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    assert harness._install_live_crontab(repo, target, yes=True)["installed"] is True
    assert calls[-1][0] == "crontab"

    (target / harness.HEARTBEAT_CONFIG).parent.mkdir(parents=True, exist_ok=True)
    (target / harness.HEARTBEAT_CONFIG).write_text("heartbeat: bad\n", encoding="utf-8")
    with pytest.raises(ValueError, match="heartbeat config must be a mapping"):
        harness.enable(repo, target)
    assert harness.payload_to_text({"ok": True}).startswith("{")


def test_repo_finalizer_helpers_and_run_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core import repo_finalizer as rf

    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config" / "localsetup_finalizer.yaml"
    config.parent.mkdir()
    config.write_text("- bad\n", encoding="utf-8")
    with pytest.raises(ValueError, match="finalizer config must be a mapping"):
        rf._read_config(repo)

    config.write_text("managed_output_globs: [123]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="managed_output_globs"):
        rf._settings(repo)

    settings = rf.FinalizerSettings(
        managed_output_globs=["managed/**"],
        generated_artifact_globs=["generated/**"],
        runtime_ignored_globs=[".state", ".state/**"],
        stage_allowlist_globs=["managed/**", "generated/**"],
    )
    items = [
        {"path": "managed/file", "status": " M", "tracked": True, "deleted": False, "renamed_or_copied": False},
        {"path": "generated/file", "status": "??", "tracked": False, "deleted": False, "renamed_or_copied": False},
        {"path": ".state/log", "status": "!!", "tracked": False, "ignored": True, "deleted": False, "renamed_or_copied": False},
        {"path": "ls/file", "status": " M", "tracked": True, "deleted": False, "renamed_or_copied": False},
        {"path": "README.md", "status": " M", "tracked": True, "deleted": False, "renamed_or_copied": False},
        {"path": "copy.txt", "status": "R ", "tracked": True, "deleted": False, "renamed_or_copied": True},
        {"path": "gone.txt", "status": " D", "tracked": True, "deleted": True, "renamed_or_copied": False},
        {"path": "ls", "status": "??", "tracked": False, "deleted": False, "renamed_or_copied": False},
    ]
    classified = rf._classify(repo, items, settings, mode="target")
    categories = {row["path"]: row["classification"] for row in classified}
    assert categories["managed/file"] == "managed_output"
    assert categories["generated/file"] == "generated_artifact"
    assert categories[".state/log"] == "runtime_ignored"
    assert categories["ls"] == "stale_legacy_framework_source"
    assert any(row["renamed_or_copied"] for row in classified)
    assert any(row["deleted"] for row in classified)

    assert rf._runtime_roots(["", ".state/**", "logs/*.json"]) == [".state", "logs"]

    def unsupported_git(target_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(args, 1, "", "not git")
        if args[:2] == ["rev-parse", "--git-path"]:
            return subprocess.CompletedProcess(args, 0, ".git/info/exclude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rf, "_git", unsupported_git)
    unsupported = rf.run(repo)
    assert unsupported["report_only"] is True
    assert "report_paths" in unsupported

    monkeypatch.setattr(rf, "_snapshot", lambda *args, **kwargs: {"git_supported": True, "target_root": str(repo), "files": [], "actions": [], "state_dir": str(repo / rf.STATE_DIR), "summary": {}})
    no_commit = rf.run(repo, no_commit=True)
    assert no_commit["took_action"] is False
    with pytest.raises(ValueError, match="--checkpoint requires --message"):
        rf.run(repo, checkpoint=True)

    staged_payload = {
        "git_supported": True,
        "target_root": str(repo),
        "files": [{"path": "managed/file", "planned_action": "stage", "blocker": False}],
        "actions": [],
        "state_dir": str(repo / rf.STATE_DIR),
        "summary": {},
    }
    monkeypatch.setattr(rf, "_snapshot", lambda *args, **kwargs: dict(staged_payload))

    def git_add_fail(target_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "add":
            return subprocess.CompletedProcess(args, 1, "", "add failed")
        if args[:2] == ["rev-parse", "--git-path"]:
            return subprocess.CompletedProcess(args, 0, ".git/info/exclude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rf, "_git", git_add_fail)
    with pytest.raises(RuntimeError, match="add failed"):
        rf.run(repo)

    def git_commit_fail(target_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "add":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "commit":
            return subprocess.CompletedProcess(args, 1, "", "commit failed")
        if args[:2] == ["rev-parse", "--git-path"]:
            return subprocess.CompletedProcess(args, 0, ".git/info/exclude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rf, "_git", git_commit_fail)
    with pytest.raises(RuntimeError, match="commit failed"):
        rf.run(repo, checkpoint=True, message="checkpoint")

    text = rf.payload_to_text({"mode": "run", "target_root": str(repo), "git_supported": True, "status": "blocked", "summary": {"total_dirty_files": 1, "blockers": 1, "stage_candidates": 0}, "files": classified[:1], "actions": [{"kind": "evaluate"}]})
    assert "actions:" in text


def test_adapter_classification_status_codes(tmp_path: Path) -> None:
    from ls.core.adapters import ADAPTER_MARKER_JSON, adapter_path_state
    from ls.core.lockfile import save_json

    global_root = tmp_path / "global"
    managed = global_root / "ls-localsetup-project-context"
    managed.mkdir(parents=True)
    (managed / "SKILL.md").write_text("# Context\n", encoding="utf-8")

    assert adapter_path_state(tmp_path / "absent", global_root)["status_code"] == "absent"

    scoped = tmp_path / "scoped"
    scoped.mkdir()
    save_json(scoped / ADAPTER_MARKER_JSON, {"version": 1, "managed_by": "localsetup", "mode": "symlink"})
    (scoped / "ls-localsetup-project-context").symlink_to(managed, target_is_directory=True)
    assert adapter_path_state(scoped, global_root)["status_code"] == "managed_scoped_adapter"

    custom = tmp_path / "custom-only"
    (custom / "media-batch-ops").mkdir(parents=True)
    (custom / "media-batch-ops" / "SKILL.md").write_text("# Custom\n", encoding="utf-8")
    assert adapter_path_state(custom, global_root)["status_code"] == "custom_repo_skills"

    (scoped / "media-batch-ops").mkdir()
    (scoped / "media-batch-ops" / "SKILL.md").write_text("# Custom\n", encoding="utf-8")
    mixed = adapter_path_state(scoped, global_root)
    assert mixed["status_code"] == "mixed_managed_custom_adapter"
    assert mixed["custom_entries"] == ["media-batch-ops"]

    portable = tmp_path / "portable"
    portable.mkdir()
    save_json(
        portable / ADAPTER_MARKER_JSON,
        {"version": 1, "managed_by": "localsetup", "mode": "portable", "packages": ["ls-localsetup-project-context"]},
    )
    shutil.copytree(managed, portable / "ls-localsetup-project-context")
    (portable / "ls-localsetup-project-context" / MARKER_JSON).write_text("{}", encoding="utf-8")
    (portable / "media-batch-ops").mkdir()
    (portable / "media-batch-ops" / "SKILL.md").write_text("# Custom\n", encoding="utf-8")
    portable_state = adapter_path_state(portable, global_root)
    assert portable_state["status_code"] == "mixed_managed_custom_adapter"
    assert portable_state["custom_entries"] == ["media-batch-ops"]

    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    (unmanaged / "notes.txt").write_text("user content\n", encoding="utf-8")
    shared = adapter_path_state(unmanaged, global_root)
    assert shared["status_code"] == "shared_adapter_directory"
    assert shared["unknown_entries"] == ["notes.txt"]

    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing", target_is_directory=True)
    assert adapter_path_state(dangling, global_root)["status_code"] == "dangling_symlink"


def test_full_plan_preflight_blocks_before_adapter_mutation(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    # OpenCode fresh installs share the current .agents path; Claude is a distinct target.
    opencode = root / ".agents" / "skills"
    opencode.mkdir(parents=True)
    (opencode / "ls-localsetup-project-context").write_text("user content\n", encoding="utf-8")

    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["claude-code", "opencode"])
    with pytest.raises(RuntimeError, match="adapter_custom_package_name_collision"):
        apply_plan(root, plan, home=home, dry_run=False)

    assert not (root / ".claude" / "skills").exists()
    assert (opencode / "ls-localsetup-project-context").read_bytes() == b"user content\n"


def test_mixed_managed_adapter_preserves_custom_skill(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    apply_plan(root, plan, home=home, dry_run=False)

    custom = root / ".agents" / "skills" / "media-batch-ops"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    apply_plan(root, plan, home=home, dry_run=False)
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom\n"
    assert (root / ".agents" / "skills" / "ls-localsetup-project-context").exists()


def test_initial_install_into_custom_skill_directory_preserves_custom_skill(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    adapter = root / ".agents" / "skills"
    custom = adapter / "media-batch-ops"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text("# Custom before install\n", encoding="utf-8")

    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    apply_plan(root, plan, home=home, dry_run=False)
    verify = verify_install(root, home=home, platform_ids=["codex"])

    assert verify["ok"] is True
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom before install\n"
    assert (adapter / "ls-localsetup-project-context").is_symlink()


def test_unproven_legacy_codex_symlink_blocks_install_and_preserves_target(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = root / ".agents" / "skills"
    custom = target / "fleetctl"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text("# Fleet custom\n", encoding="utf-8")
    codex = root / ".codex" / "skills"
    codex.parent.mkdir(parents=True)
    codex.symlink_to(Path("..") / ".agents" / "skills", target_is_directory=True)

    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    with pytest.raises(RuntimeError, match="unproven_legacy_codex_symlink"):
        apply_plan(root, plan, home=home, dry_run=False)

    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Fleet custom\n"
    assert codex.is_symlink()
    assert not (target / "ls-localsetup-project-context").exists()
    assert not (root / ".localsetup" / "lock.json").exists()


def test_mixed_managed_adapter_custom_sidecar_does_not_fail_verify_or_doctor(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(
        root,
        home=home,
        global_packs=["core"],
        repo_preset="custom",
        repo_skills=["localsetup-context"],
        platform_ids=["codex"],
    )
    apply_plan(root, plan, home=home, dry_run=False)
    custom = root / ".agents" / "skills" / "custom-skill"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    verify = verify_install(root, home=home, platform_ids=["codex"])
    doctor = run_doctor(root, home=home, platform_ids=["codex"])

    adapter = verify["adapters"][0]
    assert verify["ok"] is True
    assert adapter["status_code"] == "mixed_managed_custom_adapter"
    assert adapter["visible_packages"] == ["custom-skill", "ls-localsetup-project-context"]
    assert adapter["managed_visible_packages"] == ["ls-localsetup-project-context"]
    assert adapter["custom_entries"] == ["custom-skill"]
    assert adapter["package_integrity_ok"] is True
    assert doctor["adapter_collisions"] == []
    assert not any("adapter package target mismatch" in blocker for blocker in doctor["blockers"])


def test_in_place_adapter_update_removes_deselected_managed_packages(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    broad_plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    apply_plan(root, broad_plan, home=home, dry_run=False)
    adapter = root / ".agents" / "skills"
    stale_managed = adapter / "ls-write-run-tests"
    assert stale_managed.exists() or stale_managed.is_symlink()

    custom = adapter / "media-batch-ops"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    narrow_plan = build_install_plan(
        root,
        home=home,
        global_packs=["core"],
        repo_preset="custom",
        repo_skills=["localsetup-context"],
        platform_ids=["codex"],
    )
    apply_plan(root, narrow_plan, home=home, dry_run=False)

    assert (adapter / "ls-localsetup-project-context").exists()
    assert not stale_managed.exists()
    assert not stale_managed.is_symlink()
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom\n"


def test_deselected_same_name_custom_adapter_entry_is_preserved(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    broad_plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    apply_plan(root, broad_plan, home=home, dry_run=False)
    adapter = root / ".agents" / "skills"
    custom = adapter / "ls-write-run-tests"
    custom.unlink()
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom deselected\n", encoding="utf-8")

    narrow_plan = build_install_plan(
        root,
        home=home,
        global_packs=["core"],
        repo_preset="custom",
        repo_skills=["localsetup-context"],
        platform_ids=["codex"],
    )
    apply_plan(root, narrow_plan, home=home, dry_run=False)

    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom deselected\n"


def test_deselected_same_name_custom_portable_adapter_entry_is_preserved(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    broad_plan = build_install_plan(root, home=home, packs=["core"], attach_mode="portable", platform_ids=["codex"])
    apply_plan(root, broad_plan, home=home, dry_run=False)
    adapter = root / ".agents" / "skills"
    custom = adapter / "ls-write-run-tests"
    shutil.rmtree(custom)
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom portable deselected\n", encoding="utf-8")

    narrow_plan = build_install_plan(
        root,
        home=home,
        global_packs=["core"],
        repo_preset="custom",
        repo_skills=["localsetup-context"],
        attach_mode="portable",
        platform_ids=["codex"],
    )
    apply_plan(root, narrow_plan, home=home, dry_run=False)

    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom portable deselected\n"


def test_same_name_custom_adapter_entry_blocks_install_preflight(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    apply_plan(root, plan, home=home, dry_run=False)

    adapter = root / ".agents" / "skills"
    (adapter / "ls-localsetup-project-context").unlink()
    custom = adapter / "ls-localsetup-project-context"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom override\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="adapter_custom_package_name_collision"):
        apply_plan(root, plan, home=home, dry_run=False)
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom override\n"


def test_same_name_custom_portable_adapter_entry_blocks_install_preflight(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    plan = build_install_plan(root, home=home, packs=["core"], attach_mode="portable", platform_ids=["codex"])
    apply_plan(root, plan, home=home, dry_run=False)

    custom = root / ".agents" / "skills" / "ls-localsetup-project-context"
    shutil.rmtree(custom)
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom portable override\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="adapter_custom_package_name_collision"):
        apply_plan(root, plan, home=home, dry_run=False)
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom portable override\n"


def test_doctor_repair_blocks_same_name_custom_adapter_entry(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"], target_root=target)
    apply_plan(root, plan, home=home, dry_run=False, target_root=target)

    adapter = target / ".agents" / "skills"
    (adapter / "ls-localsetup-project-context").unlink()
    custom = adapter / "ls-localsetup-project-context"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom override\n", encoding="utf-8")

    report = run_repair(root, home=home, target_root=target, platform_ids=["codex"], apply=True)
    assert report["applied"] is False
    assert any(item["kind"] == "adapter_content" for item in report["decisions"])
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom override\n"
