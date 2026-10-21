from __future__ import annotations

from ls.tests.test_install_flow import *

def test_wizard_full_flow_renders_guided_context_for_current_repo(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "ls" / "docs", root / "ls" / "docs", dirs_exist_ok=True)
    home = tmp_path / "home"
    caller = tmp_path / "caller"
    caller.mkdir()
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n2\n\n1,3\nyes\n"),
        output_stream=output,
        color=False,
    )

    code = run_wizard(
        repo_root=root,
        home=home,
        caller_directory=caller,
        terminal=term,
        register_shell=False,
    )

    rendered = output.getvalue()
    assert code == 0
    assert wizard.WELCOME_BANNER in rendered
    assert "Skill Groups" not in rendered
    assert "Individual Skills" not in rendered
    assert "\nApplying\n" in rendered
    assert "\nResult\n" in rendered
    assert "Source and Release" in rendered
    assert "Global Package Library" in rendered
    assert "Repo Setup" in rendered
    assert "Repo Adapters" in rendered
    assert "Review" in rendered
    assert "Result" in rendered
    rendered_words = " ".join(rendered.split())
    assert "Decides: Confirms the installer source and release channel before package choices." in rendered_words
    assert "Suggested: No repo setup" in rendered
    assert "Writes adapter path .agents/skills." in rendered
    assert "Code, docs, git, testing, markdown validation, and repo repair workflows." in rendered
    assert "Global packs" in rendered
    assert "Repo-visible packages" in rendered
    assert (
        "Does: Shows source, target, packs, adapter mode, dependency mode, and concrete filesystem actions before changes."
        in rendered_words
    )
    assert "Does: Verification checked the managed library and selected adapter paths after applying the plan." in rendered_words
    assert "Enter number(s) | d details | b back | q quit | ? help" in rendered
    assert (caller / ".agents" / "skills").exists()


def test_wizard_tty_output_clears_screen_before_banner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    output = FakeTtyStringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("q\n"),
        output_stream=output,
        color=False,
    )
    monkeypatch.setenv("TERM", "xterm-256color")

    code = run_wizard(repo_root=root, home=home, terminal=term)

    assert code == 130
    assert output.getvalue().startswith("\033[2J\033[3J\033[H" + wizard.WELCOME_BANNER)


def test_wizard_scripted_output_does_not_clear_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("q\n"),
        output_stream=output,
        color=False,
    )
    monkeypatch.setenv("TERM", "xterm-256color")

    code = run_wizard(repo_root=root, home=home, terminal=term)

    assert code == 130
    assert "\033[2J" not in output.getvalue()
    assert output.getvalue().startswith(wizard.WELCOME_BANNER)


def test_wizard_cancel_exits_without_applying(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    term = TerminalWizard(
        input_stream=io.StringIO("q\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    code = run_wizard(repo_root=root, home=home, terminal=term)

    assert code == 130
    assert not (home / ".local/share/localsetup/packages").exists()


def test_wizard_keyboard_interrupt_before_apply_exits_without_applying(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=KeyboardInterruptStream(),
        output_stream=output,
        color=False,
    )

    code = run_wizard(repo_root=root, home=home, terminal=term)

    assert code == 130
    assert "Install canceled. No changes were applied." in output.getvalue()
    assert not (home / ".local/share/localsetup/packages").exists()


def test_wizard_keyboard_interrupt_during_apply_warns_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "ls" / "docs", root / "ls" / "docs", dirs_exist_ok=True)
    home = tmp_path / "home"
    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n\nyes\n"),
        output_stream=output,
        color=False,
    )

    def interrupt_apply(term: TerminalWizard, state: wizard.WizardState) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard, "_apply_and_show_result", interrupt_apply)

    code = run_wizard(repo_root=root, home=home, caller_directory=root, terminal=term, register_shell=False)

    rendered = output.getvalue()
    assert code == 130
    assert "Install interrupted during apply. Some changes may have been applied" in rendered
    assert "Install canceled. No changes were applied." not in rendered


def test_wizard_global_only_apply_with_scripted_confirmation(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "ls" / "docs", root / "ls" / "docs", dirs_exist_ok=True)
    home = tmp_path / "home"
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n\nyes\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    code = run_wizard(repo_root=root, home=home, caller_directory=root, terminal=term, register_shell=False)

    assert code == 0
    assert (home / ".local/share/localsetup/packages/ls-context").is_dir()
    assert not (root / ".codex").exists()


def test_wizard_no_repo_rerun_detaches_managed_adapters_and_preserves_global_packages(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "ls" / "docs", root / "ls" / "docs", dirs_exist_ok=True)
    home = tmp_path / "home"
    apply_plan(
        root,
        build_install_plan(root, home=home, global_packs=["dev"], repo_packs=["core"], platform_ids=["codex"]),
        home=home,
    )
    assert (root / ".agents" / "skills").is_dir()
    assert (home / ".local/share/localsetup/packages/ls-nodejs-nextjs").is_dir()
    custom = root / ".agents" / "skills" / "custom-skill"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    output = io.StringIO()
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n1\nyes\n"),
        output_stream=output,
        color=False,
    )

    code = run_wizard(repo_root=root, home=home, caller_directory=root, terminal=term, register_shell=False)

    rendered = output.getvalue()
    lock = load_json(root / ".localsetup/lock.json")
    assert code == 0
    assert "Repo Adapters" not in rendered
    assert "Detached adapters" in rendered
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom\n"
    assert not (root / ".agents" / "skills" / "ls-context").exists()
    assert (home / ".local/share/localsetup/packages/ls-nodejs-nextjs").is_dir()
    assert lock["global_only"] is True
    assert lock["adapter_targets"] == []
    assert lock["platforms"] == []


def test_wizard_no_repo_detach_ignores_absolute_adapter_outside_target(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "ls" / "docs", root / "ls" / "docs", dirs_exist_ok=True)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    global_root = home / ".local/share/localsetup/packages"
    global_root.mkdir(parents=True)
    inside = target / ".codex" / "skills"
    inside.parent.mkdir(parents=True)
    inside.symlink_to(global_root, target_is_directory=True)
    outside = tmp_path / "outside-skills"
    outside.symlink_to(global_root, target_is_directory=True)
    lock_path = target / ".localsetup" / "lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "target_root": str(target),
                "platforms": ["codex"],
                "adapter_targets": [
                    {"path": str(inside), "packages": ["ls-context"]},
                    {"path": str(outside), "packages": ["ls-context"]},
                ],
                "global_baseline_selectors": {"packs": ["core"]},
                "repo_selectors": {"packs": ["core"]},
            }
        ),
        encoding="utf-8",
    )
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n1\nyes\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    code = run_wizard(repo_root=root, home=home, caller_directory=target, terminal=term, register_shell=False)

    assert code == 0
    assert not inside.exists()
    assert outside.is_symlink()


def test_wizard_legacy_lock_selectors_seed_global_and_repo_defaults(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    adapter = root / ".codex" / "skills"
    lock_path = root / ".localsetup" / "lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "version": 2,
                "target_root": str(root),
                "platforms": ["codex"],
                "attach_mode": "symlink",
                "dependency_mode": "prompt-only",
                "selectors": {
                    "preset": "custom",
                    "packs": ["dev"],
                    "skills": ["ls-context"],
                    "workflows": ["ls-wf-risky-operations-safety-approval"],
                    "skill_classes": ["development"],
                    "skill_tags": ["git"],
                    "exclude_skills": ["ls-linux-patcher"],
                },
                "adapter_targets": [{"platform": "codex", "path": str(adapter), "mode": "symlink"}],
            }
        ),
        encoding="utf-8",
    )
    state = wizard.WizardState(repo_root=root.resolve(), home=home.resolve(), caller_directory=root.resolve())

    wizard._load_prior_defaults(state)

    assert state.global_packs == ["dev"]
    assert state.global_preset == "custom"
    assert state.global_skills == ["ls-context"]
    assert state.global_workflows == ["ls-wf-risky-operations-safety-approval"]
    assert state.global_skill_classes == ["development"]
    assert state.global_skill_tags == ["git"]
    assert state.global_exclude_skills == ["ls-linux-patcher"]
    assert state.repo_packs == ["dev"]
    assert state.repo_preset == "custom"
    assert state.repo_skills == ["ls-context"]
    assert state.repo_workflows == ["ls-wf-risky-operations-safety-approval"]
    assert state.repo_skill_classes == ["development"]
    assert state.repo_skill_tags == ["git"]
    assert state.repo_exclude_skills == ["ls-linux-patcher"]


def test_wizard_root_legacy_lock_selectors_seed_global_and_repo_defaults(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    adapter = root / ".codex" / "skills"
    (root / "localsetup.lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "target_root": str(root),
                "platforms": ["codex"],
                "selectors": {
                    "preset": "custom",
                    "packs": ["dev"],
                    "skills": ["ls-context"],
                    "workflows": ["ls-wf-risky-operations-safety-approval"],
                    "skill_classes": ["development"],
                    "skill_tags": ["git"],
                    "exclude_skills": ["ls-linux-patcher"],
                },
                "adapter_targets": [{"platform": "codex", "path": str(adapter), "mode": "symlink"}],
            }
        ),
        encoding="utf-8",
    )
    state = wizard.WizardState(repo_root=root.resolve(), home=home.resolve(), caller_directory=root.resolve())

    wizard._load_prior_defaults(state)

    assert state.global_packs == ["dev"]
    assert state.global_preset == "custom"
    assert state.global_skills == ["ls-context"]
    assert state.global_workflows == ["ls-wf-risky-operations-safety-approval"]
    assert state.global_skill_classes == ["development"]
    assert state.global_skill_tags == ["git"]
    assert state.global_exclude_skills == ["ls-linux-patcher"]
    assert state.repo_packs == ["dev"]
    assert state.repo_preset == "custom"
    assert state.repo_skills == ["ls-context"]
    assert state.repo_workflows == ["ls-wf-risky-operations-safety-approval"]
    assert state.repo_skill_classes == ["development"]
    assert state.repo_skill_tags == ["git"]
    assert state.repo_exclude_skills == ["ls-linux-patcher"]


def test_wizard_custom_preset_can_install_individual_skill_without_pack(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "ls" / "docs", root / "ls" / "docs", dirs_exist_ok=True)
    home = tmp_path / "home"
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n\nyes\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    code = run_wizard(
        repo_root=root,
        home=home,
        caller_directory=root,
        terminal=term,
        preset="custom",
        skills=["ls-context"],
        register_shell=False,
    )

    lock = load_json(root / ".localsetup/lock.json")
    assert code == 0
    assert (home / ".local/share/localsetup/packages/ls-context").is_dir()
    assert not (home / ".local/share/localsetup/packages/ls-test-runner").exists()
    assert lock["skills"] == ["ls-context"]


def test_wizard_explicit_target_is_default_when_provided(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "ls" / "docs", root / "ls" / "docs", dirs_exist_ok=True)
    home = tmp_path / "home"
    caller = tmp_path / "caller"
    target = tmp_path / "target"
    caller.mkdir()
    target.mkdir()
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n\n\n\nyes\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    code = run_wizard(
        repo_root=root,
        home=home,
        caller_directory=caller,
        target_directory=target,
        target_directory_is_explicit=True,
        platforms=["cursor"],
        terminal=term,
        register_shell=False,
    )

    assert code == 0
    assert_scoped_adapter(target / ".agents" / "skills", "ls-context")
    assert (target / ".localsetup/lock.json").is_file()
    assert not (caller / ".cursor").exists()


def test_wizard_explicit_target_without_platforms_defaults_global_only(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "ls" / "docs", root / "ls" / "docs", dirs_exist_ok=True)
    home = tmp_path / "home"
    caller = tmp_path / "caller"
    target = tmp_path / "target"
    caller.mkdir()
    target.mkdir()
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n\n\nyes\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    code = run_wizard(
        repo_root=root,
        home=home,
        caller_directory=caller,
        target_directory=target,
        target_directory_is_explicit=True,
        terminal=term,
        register_shell=False,
    )

    assert code == 0
    assert (target / ".localsetup/lock.json").is_file()
    assert not (target / ".codex").exists()
    assert not (caller / ".codex").exists()
