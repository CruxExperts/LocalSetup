from __future__ import annotations

from ls.tests.test_install_flow import *

def test_health_summary_is_ignored_in_git_worktree(tmp_path: Path) -> None:
    from ls.core.health import write_health_event

    source = make_temp_repo(tmp_path / "source")
    base = tmp_path / "base"
    linked = tmp_path / "linked"
    base.mkdir()
    subprocess.run(["git", "init"], cwd=base, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=base, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=base, check=True, capture_output=True)
    (base / "README.md").write_text("# Base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=base, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=base, check=True, capture_output=True)
    subprocess.run(["git", "worktree", "add", str(linked)], cwd=base, check=True, capture_output=True)

    write_health_event(
        repo_root=source,
        home=tmp_path / "home",
        target_root=linked,
        operation="doctor",
        mode="report-only",
        status="ok",
        payload={},
    )

    assert (linked / ".git").is_file()
    assert subprocess.run(["git", "check-ignore", "-q", "--", ".localsetup/health.json"], cwd=linked, check=False).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", "--", ".localsetup/AGENT_STATUS.md"], cwd=linked, check=False).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", "--", ".localsetup/install-journal/x.json"], cwd=linked, check=False).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", "--", ".localsetup/backups/x"], cwd=linked, check=False).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", "--", ".localsetup/state/x"], cwd=linked, check=False).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", "--", ".localsetup/context-index/x"], cwd=linked, check=False).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", "--", ".localsetup/lock.json"], cwd=linked, check=False).returncode != 0
    assert subprocess.run(["git", "status", "--short"], cwd=linked, text=True, capture_output=True).stdout == ""


def test_clean_tracked_stale_framework_is_untracked_and_removed(tmp_path: Path) -> None:
    source = make_temp_repo(tmp_path / "source")
    target = tmp_path / "target"
    target.mkdir()
    shutil.copytree(source / "ls", target / "ls")
    subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "add", "ls"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "track framework"], cwd=target, check=True, capture_output=True)

    report = run_repair(source, home=tmp_path / "home", target_root=target, apply=False)
    assert not any(item["kind"] == "tracked_framework_removal" for item in report["decisions"])
    assert report["detected_shape"]["stale_framework"]["classification"] == "clean_tracked_stale_framework"
    assert [action["kind"] for action in report["actions"][:2]] == [
        "git_untrack_stale_framework",
        "backup_remove_stale_framework",
    ]

    applied = run_repair(
        source,
        home=tmp_path / "home",
        target_root=target,
        apply=True,
        repair_mode="safe-repair",
    )
    assert applied["applied"] is True
    assert not (target / "ls").exists()
    tracked = subprocess.run(["git", "ls-files", "--", "ls"], cwd=target, text=True, capture_output=True, check=True)
    assert tracked.stdout == ""


@pytest.mark.parametrize("tracked", [False, True])
def test_framework_shaped_ls_with_extra_files_blocks_repair(tmp_path: Path, tracked: bool) -> None:
    source = make_temp_repo(tmp_path / "source")
    target = tmp_path / "target"
    target.mkdir()
    shutil.copytree(source / "ls", target / "ls")
    extra = target / "ls" / "private-notes.txt"
    extra.write_text("custom\n", encoding="utf-8")
    if tracked:
        subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=target, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=target, check=True, capture_output=True)
        subprocess.run(["git", "add", "ls"], cwd=target, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "track framework"], cwd=target, check=True, capture_output=True)

    report = run_repair(source, home=tmp_path / "home", target_root=target, apply=True, repair_mode="safe-repair")

    assert report["applied"] is False
    stale = report["detected_shape"]["stale_framework"]
    assert stale["classification"] == "custom_framework_content"
    assert "private-notes.txt" in stale["unknown_entries"]
    assert any(item.get("code") == "custom_framework_content" for item in report["decisions"])
    assert extra.read_text(encoding="utf-8") == "custom\n"


@pytest.mark.parametrize("git_mode", ["none", "untracked", "tracked"])
def test_framework_shaped_ls_with_modified_existing_file_blocks_repair(tmp_path: Path, git_mode: str) -> None:
    source = make_temp_repo(tmp_path / "source")
    target = tmp_path / "target"
    target.mkdir()
    shutil.copytree(source / "ls", target / "ls")
    modified = target / "ls" / "config" / "pack.yaml"
    modified.write_text(modified.read_text(encoding="utf-8") + "\n# custom edit\n", encoding="utf-8")
    if git_mode != "none":
        subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=target, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=target, check=True, capture_output=True)
        if git_mode == "tracked":
            subprocess.run(["git", "add", "ls"], cwd=target, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "track framework"], cwd=target, check=True, capture_output=True)

    report = run_repair(source, home=tmp_path / "home", target_root=target, apply=True, repair_mode="safe-repair")

    stale = report["detected_shape"]["stale_framework"]
    assert report["applied"] is False
    assert stale["classification"] == "custom_framework_content"
    assert "config/pack.yaml" in stale["modified_entries"]
    assert modified.exists()


def test_repair_infers_visible_workflows_and_preserves_custom_repo_skills(tmp_path: Path) -> None:
    source = make_temp_repo(tmp_path / "source")
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    plan = build_install_plan(source, home=home, packs=["ops"], platform_ids=["codex"], target_root=target)
    apply_plan(source, plan, home=home, dry_run=False, target_root=target)
    (target / ".localsetup" / "lock.json").unlink()

    custom = target / ".agents" / "skills" / "media-batch-ops"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    report = run_repair(source, home=home, target_root=target, apply=False)

    assert report["ok"] is True
    assert "ls-wf-sudo-tmux-session" in report["inferred"]["repo_workflows"]
    assert "ls-wf-server-triage-patch-planning" in report["inferred"]["repo_workflows"]
    assert {"name": "media-batch-ops", "path": str(custom)} in report["inferred"]["custom_repo_skills"]
    assert not any(item["kind"] == "package_selection" for item in report["decisions"])


def test_doctor_repair_emits_agent_prompt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = make_temp_repo(tmp_path / "source")
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    custom = target / "ls"
    custom.mkdir()
    (custom / "notes.txt").write_text("custom\n", encoding="utf-8")
    prompt_path = tmp_path / "prompt.md"

    status = cli_mod._main(
        [
            "--source-root",
            str(source),
            "--home",
            str(home),
            "doctor",
            "repair",
            "--target-directory",
            str(target),
            "--repair-mode",
            "migration-plan",
            "--agent-prompt",
            "--emit-agent-prompt",
            str(prompt_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 1
    assert payload["agent_prompt"]["path"] == str(prompt_path.resolve())
    text = prompt_path.read_text(encoding="utf-8")
    assert "LocalSetup Repair Handoff" in text
    assert "custom_framework_content" in text
    assert "notes.txt" not in text


def test_agent_prompt_includes_blocker_only_payload() -> None:
    from ls.core.handoff import agent_prompt_payload

    first = agent_prompt_payload({"target_root": "/tmp/repo", "blockers": ["blocked by policy"]})
    second = agent_prompt_payload({"target_root": "/tmp/repo", "blockers": ["different blocker"]})
    spaced = agent_prompt_payload({"target_root": "/tmp/target repo", "blockers": ["blocked"]})

    assert "blocked by policy" in first["text"]
    assert "Status: `blocked`" in first["text"]
    assert first["context_hash"] != second["context_hash"]
    command_line = next(line[3:-1] for line in spaced["text"].splitlines() if "doctor repair" in line)
    assert shlex.split(command_line)[4] == "/tmp/target repo"


def test_context_and_diff_honor_explicit_workflow_selectors(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    config = InstallConfig(
        preset="custom",
        workflows=["ls-wf-risky-operations-safety-approval"],
        global_preset="custom",
        global_workflows=["ls-wf-risky-operations-safety-approval"],
        repo_preset="custom",
        repo_workflows=["ls-wf-risky-operations-safety-approval"],
        platforms=["codex"],
    )

    context = build_agent_context(root, home=home, config=config)

    assert "ls-wf-risky-operations-safety-approval" in context["rollback"]["workflows"]

    from ls.core.diffing import diff_plan_current

    payload = diff_plan_current(
        root,
        home=home,
        packs=None,
        preset="custom",
        workflows=["ls-wf-risky-operations-safety-approval"],
        global_preset="custom",
        global_workflows=["ls-wf-risky-operations-safety-approval"],
        repo_preset="custom",
        repo_workflows=["ls-wf-risky-operations-safety-approval"],
        platform_ids=["codex"],
        target_root=None,
        attach_mode="symlink",
    )
    assert "ls-wf-risky-operations-safety-approval" in payload["workflows"]["added"]


def test_auto_inferred_plan_preserves_global_workflows_from_lock(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    (root / ".localsetup").mkdir()
    (root / ".localsetup" / "lock.json").write_text(
        json.dumps(
            {
                "global_baseline_selectors": {
                    "preset": "custom",
                    "workflows": ["ls-wf-risky-operations-safety-approval"],
                },
                "repo_packages": ["ls-context"],
                "repo_skills": ["ls-context"],
                "repo_workflows": [],
                "platforms": ["codex"],
            }
        ),
        encoding="utf-8",
    )

    plan = cli_mod._build_auto_inferred_plan(
        root,
        home,
        root,
        {
            "inferred": {
                "platforms": ["codex"],
                "repo_skills": ["ls-context"],
                "repo_workflows": [],
                "attach_mode": "symlink",
            }
        },
    )

    assert "ls-wf-risky-operations-safety-approval" in plan.rollback_metadata["global_baseline_workflows"]


def test_repair_queue_prompt_command_quotes_spaced_target_and_classifies_string_blockers(tmp_path: Path) -> None:
    from ls.core.health import repair_queue, write_health_event

    root = make_temp_repo(tmp_path / "source")
    home = tmp_path / "home"
    target = tmp_path / "target repo"
    target.mkdir()
    write_health_event(
        repo_root=root,
        home=home,
        target_root=target,
        operation="doctor.repair",
        mode="report-only",
        status="blocked",
        payload={},
        blockers=["blocked by policy"],
    )

    item = repair_queue(home=home)["items"][0]
    assert item["prompt_argv"][4] == str(target.resolve())
    assert shlex.split(item["prompt_command"])[4] == str(target.resolve())
    assert item["blocker_kinds"] == ["message"]
