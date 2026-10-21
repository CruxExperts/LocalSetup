from __future__ import annotations

from ls.tests.test_install_flow import *

def test_wizard_checkbox_key_mode_covers_bulk_help_back_and_empty_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = [("core", "Core"), ("dev", "Dev")]

    stream = FakeKeyInput("?djank\n")
    enable_checkbox_key_mode(monkeypatch, stream)
    term = TerminalWizard(input_stream=stream, output_stream=io.StringIO(), color=False)
    assert choose_many_checkbox(term, "Packs", choices, default=["core"], allow_none=True, help_text="Toggle packs.") == []
    rendered = term.output.getvalue()
    assert "Toggle packs." in rendered
    assert "Detail mode: compact." in rendered

    back_stream = FakeKeyInput("b")
    enable_checkbox_key_mode(monkeypatch, back_stream)
    assert (
        choose_many_checkbox(
            TerminalWizard(input_stream=back_stream, output_stream=io.StringIO(), color=False),
            "Packs",
            choices,
            default=[],
        )
        == wizard.BACK
    )

    empty_stream = FakeKeyInput("")
    patch_fake_key_input(monkeypatch, empty_stream)
    term = TerminalWizard(input_stream=empty_stream, output_stream=io.StringIO(), color=False)
    assert wizard._read_escape_sequence(term) == ""


def test_wizard_review_blockers_apply_failure_and_warning_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_temp_repo(tmp_path)
    state = wizard.WizardState(
        repo_root=root,
        home=tmp_path / "home",
        caller_directory=root,
        target_directory=root,
        platforms=["codex"],
        global_packs=["core"],
        repo_packs=["core"],
    )
    fake_action = SimpleNamespace(kind="attach_repo_path", path=root / ".codex" / "skills", details={"platform": "codex"})
    fake_plan = SimpleNamespace(
        actions=[fake_action],
        rollback_metadata={
            "global_baseline_packs": ["core"],
            "repo_packs": ["core"],
            "global_baseline_packages": ["ls-localsetup-project-context"],
            "repo_packages": ["ls-localsetup-project-context"],
            "skills": ["ls-localsetup-project-context"],
            "workflows": [],
        },
    )
    monkeypatch.setattr(wizard, "build_install_plan", lambda *args, **kwargs: fake_plan)
    monkeypatch.setattr(wizard, "run_doctor", lambda *args, **kwargs: {"warnings": ["warn"], "blockers": ["block"]})

    blocker_term = TerminalWizard(io.StringIO("?\nd\nb\n"), io.StringIO(), color=False)
    assert wizard._review_step(blocker_term, state) == wizard.BACK
    assert "Diagnostic command:" in blocker_term.output.getvalue()

    monkeypatch.setattr(wizard, "build_install_plan", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    failed_term = TerminalWizard(io.StringIO(), io.StringIO(), color=False)
    assert wizard._apply_and_show_result(failed_term, state) == 2
    assert "Install failed." in failed_term.output.getvalue()

    monkeypatch.setattr(wizard, "build_install_plan", lambda *args, **kwargs: fake_plan)
    monkeypatch.setattr(wizard, "ensure_dependencies", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(wizard, "apply_plan", lambda *args, **kwargs: {"ok": True, "lockfile": str(root / ".localsetup" / "lock.json")})
    monkeypatch.setattr(wizard, "register_shell_command", lambda *args, **kwargs: {"warnings": ["path warning"]})
    monkeypatch.setattr(wizard, "verify_install", lambda *args, **kwargs: {"ok": False})
    warning_term = TerminalWizard(io.StringIO(), io.StringIO(), color=False)
    assert wizard._apply_and_show_result(warning_term, state) == 1
    assert "verification reported issues" in warning_term.output.getvalue()
    assert "path warning" in warning_term.output.getvalue()


def test_wizard_first_run_global_pack_step_defaults_to_normal_profile(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    state = wizard.WizardState(
        repo_root=root,
        home=tmp_path / "home",
        caller_directory=root,
    )
    term = TerminalWizard(io.StringIO("\n"), io.StringIO(), color=False)

    assert wizard._pack_step(term, state) == "continue"

    assert state.global_preset == "normal"
    assert state.global_packs == [
        "bootstrap",
        "core",
        "dev",
        "frontend",
        "architecture",
        "ops",
        "publishing",
    ]


def test_wizard_global_skill_selector_does_not_use_normal_pack_step_fallback(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    state = wizard.WizardState(
        repo_root=root,
        home=tmp_path / "home",
        caller_directory=root,
        global_skills=["ls-localsetup-project-context"],
    )
    term = TerminalWizard(io.StringIO("\n"), io.StringIO(), color=False)

    assert wizard._pack_step(term, state) == "continue"

    assert state.global_packs == ["core"]
    assert state.global_preset == "core"


def test_wizard_review_fallback_uses_normal_profile_packs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    state = wizard.WizardState(
        repo_root=root,
        home=tmp_path / "home",
        caller_directory=root,
    )
    captured: dict[str, object] = {}
    fake_plan = SimpleNamespace(
        actions=[],
        rollback_metadata={
            "global_baseline_packs": ["bootstrap", "core", "dev", "frontend", "architecture", "ops", "publishing"],
            "repo_packs": [],
            "global_baseline_packages": ["ls-localsetup-project-context"],
            "repo_packages": [],
            "skills": ["ls-localsetup-project-context"],
            "workflows": [],
        },
    )

    def fake_build_install_plan(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_plan

    monkeypatch.setattr(wizard, "build_install_plan", fake_build_install_plan)
    monkeypatch.setattr(wizard, "run_doctor", lambda *args, **kwargs: {"warnings": [], "blockers": []})

    assert wizard._review_step(TerminalWizard(io.StringIO("\n"), io.StringIO(), color=False), state) == wizard.BACK

    assert captured["global_packs"] == [
        "bootstrap",
        "core",
        "dev",
        "frontend",
        "architecture",
        "ops",
        "publishing",
    ]
    assert captured["global_preset"] == "normal"


def test_wizard_review_global_skill_selector_does_not_use_normal_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_temp_repo(tmp_path)
    state = wizard.WizardState(
        repo_root=root,
        home=tmp_path / "home",
        caller_directory=root,
        global_skills=["ls-localsetup-project-context"],
    )
    captured: dict[str, object] = {}
    fake_plan = SimpleNamespace(
        actions=[],
        rollback_metadata={
            "global_baseline_packs": ["core"],
            "repo_packs": [],
            "global_baseline_packages": ["ls-localsetup-project-context"],
            "repo_packages": [],
            "skills": ["ls-localsetup-project-context"],
            "workflows": [],
        },
    )

    def fake_build_install_plan(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_plan

    monkeypatch.setattr(wizard, "build_install_plan", fake_build_install_plan)
    monkeypatch.setattr(wizard, "run_doctor", lambda *args, **kwargs: {"warnings": [], "blockers": []})

    assert wizard._review_step(TerminalWizard(io.StringIO("\n"), io.StringIO(), color=False), state) == wizard.BACK

    assert captured["global_packs"] == ["core"]
    assert captured["global_preset"] is None
    assert captured["global_skills"] == ["ls-localsetup-project-context"]


def test_wizard_apply_fallback_uses_normal_profile_packs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    state = wizard.WizardState(
        repo_root=root,
        home=tmp_path / "home",
        caller_directory=root,
        register_shell=False,
    )
    captured: dict[str, object] = {}
    fake_plan = SimpleNamespace(actions=[], rollback_metadata={"platforms": [], "global_only": True})

    def fake_build_install_plan(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_plan

    monkeypatch.setattr(wizard, "build_install_plan", fake_build_install_plan)
    monkeypatch.setattr(wizard, "apply_plan", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(wizard, "verify_install", lambda *args, **kwargs: {"ok": True})

    code = wizard._apply_and_show_result(TerminalWizard(io.StringIO(), io.StringIO(), color=False), state)

    assert code == 0
    assert captured["global_packs"] == [
        "bootstrap",
        "core",
        "dev",
        "frontend",
        "architecture",
        "ops",
        "publishing",
    ]
    assert captured["global_preset"] == "normal"


def test_wizard_apply_global_skill_selector_does_not_use_normal_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_temp_repo(tmp_path)
    state = wizard.WizardState(
        repo_root=root,
        home=tmp_path / "home",
        caller_directory=root,
        global_skills=["ls-localsetup-project-context"],
        register_shell=False,
    )
    captured: dict[str, object] = {}
    fake_plan = SimpleNamespace(actions=[], rollback_metadata={"platforms": [], "global_only": True})

    def fake_build_install_plan(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_plan

    monkeypatch.setattr(wizard, "build_install_plan", fake_build_install_plan)
    monkeypatch.setattr(wizard, "apply_plan", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(wizard, "verify_install", lambda *args, **kwargs: {"ok": True})

    code = wizard._apply_and_show_result(TerminalWizard(io.StringIO(), io.StringIO(), color=False), state)

    assert code == 0
    assert captured["global_packs"] == ["core"]
    assert captured["global_preset"] is None
    assert captured["global_skills"] == ["ls-localsetup-project-context"]


def test_config_rejects_invalid_shapes_and_modes(tmp_path: Path) -> None:
    from ls.core.config import validate_install_config
    from ls.core.paths import PathValidationError

    with pytest.raises(FileNotFoundError):
        load_install_config(tmp_path / "missing.json")

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid install config JSON"):
        load_install_config(invalid_json)

    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="install config must be a JSON object"):
        load_install_config(non_object)

    bad_output = tmp_path / "bad-output.json"
    bad_output.write_text('{"output": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="output"):
        load_install_config(bad_output)

    bad_list = tmp_path / "bad-list.json"
    bad_list.write_text('{"platforms": ["codex", ""]}', encoding="utf-8")
    with pytest.raises(ValueError, match="platforms"):
        load_install_config(bad_list)

    bad_string = tmp_path / "bad-string.json"
    bad_string.write_text('{"home": ""}', encoding="utf-8")
    with pytest.raises(ValueError, match="home"):
        load_install_config(bad_string)

    for kwargs, message in [
        ({"attach_mode": "copy"}, "unsupported attach mode"),
        ({"dependency_mode": "magic"}, "unsupported dependency mode"),
        ({"migration_mode": "fast"}, "unsupported migration mode"),
    ]:
        with pytest.raises(ValueError, match=message):
            merge_cli_config(InstallConfig(), **kwargs)
    with pytest.raises(ValueError, match="unsupported backup policy"):
        validate_install_config(InstallConfig(backup_policy="never"))

    with pytest.raises(PathValidationError, match="contains a NUL byte"):
        merge_cli_config(InstallConfig(), home="bad\x00path")


def test_adapter_state_edge_cases_cover_markers_symlinks_and_child_types(tmp_path: Path) -> None:
    from ls.core.adapters import adapter_targets

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    global_root = home / ".local" / "share" / "localsetup" / "packages"
    global_root.mkdir(parents=True)
    (global_root / "ls-localsetup-project-context").mkdir()

    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / ".localsetup-adapter.json").write_text("[]", encoding="utf-8")
    state = apply_mod.adapter_path_state(adapter, global_root)
    assert state["package_integrity_failures"][0]["reason"] == "adapter marker is not a JSON object"

    (adapter / ".localsetup-adapter.json").write_text('{"mode": "symlink", "packages": ["ls-localsetup-project-context"]}', encoding="utf-8")
    (adapter / "ls-localsetup-project-context").mkdir()
    (adapter / "plain.txt").write_text("plain\n", encoding="utf-8")
    integrity = apply_mod.adapter_path_state(adapter, global_root)["package_integrity_failures"]
    reasons = {row["reason"] for row in integrity}
    assert "symlink adapter package is not a symlink" in reasons
    assert "adapter package is not a supported filesystem node" not in reasons

    portable = tmp_path / "portable"
    portable.mkdir()
    (portable / ".localsetup-adapter.json").write_text('{"mode": "portable", "packages": ["ls-localsetup-project-context"]}', encoding="utf-8")
    (portable / "ls-localsetup-project-context").symlink_to(global_root / "ls-localsetup-project-context", target_is_directory=True)
    portable_failures = apply_mod.adapter_path_state(portable, global_root)["package_integrity_failures"]
    assert portable_failures[0]["reason"] == "portable adapter package is not a directory copy"

    symlink_adapter = tmp_path / "symlink-adapter"
    symlink_adapter.symlink_to(Path("missing-global"), target_is_directory=True)
    assert apply_mod.adapter_path_state(symlink_adapter, global_root)["is_dangling_symlink"] is True

    with pytest.raises(ValueError, match="unknown platform selector"):
        adapter_targets(root, home, ["not-a-platform"])


def test_verify_and_rollback_issue_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    (target / "ls").mkdir()
    (target / "localsetup.lock.json").write_text("{}", encoding="utf-8")
    global_root = home / ".local" / "share" / "localsetup" / "packages"
    global_root.mkdir(parents=True)
    package = global_root / "ls-localsetup-project-context"
    package.mkdir()
    (target / ".localsetup").mkdir()
    (target / ".localsetup" / "lock.json").write_text(
        json.dumps(
            {
                "attach_mode": "portable",
                "aliases": {"context": "missing-skill", "local": "ls-localsetup-project-context"},
                "workflows": ["missing-workflow"],
                "installed_skills": [str(package)],
                "adapter_targets": [
                    {"path": str(target / ".codex" / "skills"), "packages": ["ls-localsetup-project-context"]},
                    {"path": str(target / ".cursor" / "skills"), "packages": ["ls-localsetup-project-context"]},
                    {"path": str(target / ".kilo" / "skills"), "packages": ["ls-localsetup-project-context"]},
                ],
            }
        ),
        encoding="utf-8",
    )

    adapters = [
        {
            "platform": "codex",
            "repo_path": str(target / ".codex" / "skills"),
            "exists": False,
            "points_to_global": False,
            "is_scoped_symlink_adapter": False,
            "is_portable_copy": False,
            "visible_packages": [],
            "package_integrity_failures": [],
        },
        {
            "platform": "cursor",
            "repo_path": str(target / ".cursor" / "skills"),
            "exists": True,
            "points_to_global": False,
            "is_scoped_symlink_adapter": False,
            "is_portable_copy": False,
            "visible_packages": [],
            "package_integrity_failures": [],
        },
        {
            "platform": "kilo",
            "repo_path": str(target / ".kilo" / "skills"),
            "exists": True,
            "points_to_global": False,
            "is_scoped_symlink_adapter": False,
            "is_portable_copy": True,
            "visible_packages": ["other"],
            "package_integrity_failures": [{"package": "ls-localsetup-project-context"}],
        },
    ]
    monkeypatch.setattr("ls.core.verify.recorded_adapter_status", lambda *args, **kwargs: adapters)
    monkeypatch.setattr("ls.core.verify.validate_workflow_catalog", lambda *args, **kwargs: ["bad workflow"])
    monkeypatch.setattr(
        "ls.core.verify.provenance_report",
        lambda *args, **kwargs: {"warnings": [], "repair_hints": []},
    )

    payload = verify_install(root, home, platform_ids=["codex", "cursor", "kilo"], target_root=target)

    assert payload["ok"] is False
    assert any("legacy root lockfile remains" in issue for issue in payload["issues"])
    assert any("stale target framework source" in issue for issue in payload["issues"])
    assert any("missing managed skill" in issue for issue in payload["issues"])
    assert any("managed marker missing" in issue for issue in payload["issues"])
    assert any("missing managed workflow" in issue for issue in payload["issues"])
    assert any("adapter visible packages do not match selection" in issue for issue in payload["issues"])
    assert any("workflow manifest validation failed" in issue for issue in payload["issues"])

    outside_adapter = tmp_path / "outside" / "skills"
    (target / ".localsetup" / "lock.json").write_text(
        json.dumps({"adapter_state": [str(outside_adapter)], "installed_skills": [], "installed_workflows": []}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="outside target root"):
        rollback(root, home, target_root=target)


def test_schema_reports_missing_file_and_validation_errors(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"

    assert validate_json_schema({}, schema, label="demo") == []

    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )

    issues = validate_json_schema({}, schema, label="demo")
    assert issues
    assert issues[0].startswith("demo schema validation failed at <root>")


def test_wizard_back_help_and_interrupt_branches(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)

    close_term = TerminalWizard(io.StringIO(), io.StringIO(), color=False)
    close_term.input.close = lambda: (_ for _ in ()).throw(OSError("close failed"))  # type: ignore[method-assign]
    close_term.close()

    assert wizard._choice_from_input(Choice("x", "X", "summary", "effect", "best", "tradeoff")).value == "x"
    generic_help = TerminalWizard(io.StringIO(), io.StringIO(), color=False)
    wizard._print_step_help(generic_help, help_text=None, allow_many=True)
    wizard._print_step_help(generic_help, help_text=None, allow_many=False)
    assert "several comma-separated" in generic_help.output.getvalue()
    assert "Enter a number" in generic_help.output.getvalue()

    assert wizard._target_directory_prompt(TerminalWizard(io.StringIO("b\n"), io.StringIO(), color=False)) == wizard.BACK
    choose_many_term = TerminalWizard(io.StringIO("?\nd\ncore\n"), io.StringIO(), color=False)
    assert choose_many(choose_many_term, "Packs", [("core", "Core")], default=[], allow_none=True) == ["core"]
    assert "Enter one number" in choose_many_term.output.getvalue()

    state = wizard.WizardState(repo_root=root, home=tmp_path / "home", caller_directory=root, target_directory=root)
    for step in (
        wizard._pack_step,
        wizard._skill_group_step,
        wizard._skill_individual_step,
        wizard._options_step,
    ):
        assert step(TerminalWizard(io.StringIO("b\n"), io.StringIO(), color=False), state) == wizard.BACK
    state.platforms = ["codex"]
    state.preset = "core"
    assert wizard._platform_step(TerminalWizard(io.StringIO("\nb\n"), io.StringIO(), color=False), state) == wizard.BACK

    interrupted = TerminalWizard(io.StringIO(), io.StringIO(), color=False)
    wizard._write_interrupted_message(interrupted, apply_started=False)
    assert "No changes were applied" in interrupted.output.getvalue()


def test_cli_helper_and_policy_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    skill = root / "ls" / "skills" / "ls-localsetup-project-context" / "SKILL.md"
    skill.write_text(
        "---\nname: ls-localsetup-project-context\nrisk: high\npermissions: bad\n---\n# Context\n",
        encoding="utf-8",
    )

    args = SimpleNamespace(cmd="plan", target_directory=None)
    monkeypatch.setenv(cli_mod.SHIM_ENV, "1")
    monkeypatch.setattr(cli_mod, "detect_invocation_target", lambda: root)
    cli_mod._inject_global_target(args)
    assert args.target_directory == str(root)
    assert cli_mod._target_directory_value(args) == str(root)

    policy = cli_mod._policy_findings(root, ["ls-localsetup-project-context"], "ci")
    assert policy["warnings"]
    assert any("high-risk skill blocked" in blocker for blocker in policy["blockers"])

    global_root = home / ".local" / "share" / "localsetup" / "packages"
    global_root.mkdir(parents=True)
    adapter = root / ".agents" / "skills"
    adapter.parent.mkdir(parents=True)
    adapter.symlink_to(global_root, target_is_directory=True)
    # Isolate helper ordering/coalescing from additions to the real client catalog.
    monkeypatch.setattr(cli_mod, "load_platforms", lambda *_: [
        SimpleNamespace(platform_id="openclaw", repo_paths=[".agents/skills"]),
        SimpleNamespace(platform_id="codex", repo_paths=[".agents/skills", ".other/skills"]),
        SimpleNamespace(platform_id="cursor", repo_paths=[".agents/skills"]),
        SimpleNamespace(platform_id="native-fixture", repo_paths=[".native/skills"]),
    ])
    assert cli_mod._existing_target_platforms(root, root, home) == [
        {"platform": "codex", "mode": "symlink"},
        {"platform": "cursor", "mode": "symlink"},
        {"platform": "openclaw", "mode": "symlink"},
    ]

    fake_plan = SimpleNamespace(actions=[], rollback_metadata={"skills": [], "workflows": [], "platforms": [], "global_only": True})
    monkeypatch.setattr(cli_mod, "build_install_plan", lambda *args, **kwargs: fake_plan)
    monkeypatch.setattr(cli_mod, "install_inventory", lambda *args, **kwargs: {})
    monkeypatch.setattr(cli_mod, "_policy_findings", lambda *args, **kwargs: {"mode": "standard", "warnings": ["warn"], "blockers": []})
    assert cli_mod._main(["--source-root", str(root), "--home", str(home), "--target-directory", str(root), "plan", "--global-packs", "core"]) == 0
    assert "target directory was provided but no platforms were selected" in capsys.readouterr().out

    monkeypatch.setattr(cli_mod, "_policy_findings", lambda *args, **kwargs: {"mode": "standard", "warnings": [], "blockers": ["block"]})
    assert cli_mod._main(["--source-root", str(root), "--home", str(home), "install", "--apply"]) == 1
    assert "block" in capsys.readouterr().out

    monkeypatch.setattr(cli_mod, "run_doctor", lambda *args, **kwargs: {"ok": True, "warnings": [], "blockers": []})
    monkeypatch.setattr(cli_mod, "shell_registration_status", lambda *args, **kwargs: {"warnings": ["shell warn"]})
    assert cli_mod._main(["--source-root", str(root), "--home", str(home), "doctor"]) == 0
    assert "shell warn" in capsys.readouterr().out


def test_cli_no_command_prints_concise_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_mod._main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "localsetup: command required" in captured.err
    assert "localsetup doctor" in captured.err
    assert "localsetup verify --level filesystem" in captured.err
    assert "the following arguments are required: cmd" not in captured.err


def test_cli_version_failure_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_temp_repo(tmp_path)

    release_type_required = {"ok": False, "release_type_required": True, "bump": "major", "target_version": "9.9.9"}
    monkeypatch.setattr(cli_mod, "plan_version", lambda *args, **kwargs: release_type_required)
    assert cli_mod._main(["--source-root", str(root), "version-sync"]) == 1
    assert "release_type_required" in capsys.readouterr().out
    assert cli_mod._main(["--source-root", str(root), "release-push"]) == 1
    assert "release_type_required" in capsys.readouterr().out

    plans = iter(
        [
            {"ok": False, "release_type_required": False, "bump": "patch", "target_version": "9.9.9"},
            release_type_required,
        ]
    )
    monkeypatch.setattr(cli_mod, "plan_version", lambda *args, **kwargs: next(plans))
    monkeypatch.setattr(cli_mod, "sync_version_files", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "commit_version_sync", lambda *args, **kwargs: "abc123")
    assert cli_mod._main(["--source-root", str(root), "release-push"]) == 1
    assert "release_type_required" in capsys.readouterr().out
