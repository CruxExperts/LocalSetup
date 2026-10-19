from __future__ import annotations

from ls.tests.test_install_flow import *

def test_cli_dispatches_stubbed_branches_without_heavy_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    report = tmp_path / "report.json"
    out = tmp_path / "out.json"
    artifact = tmp_path / "artifact.tar.gz"

    fake_plan = SimpleNamespace(actions=[], rollback_metadata={"skills": [], "workflows": [], "platforms": [], "global_only": True})
    monkeypatch.setattr(cli_mod, "build_install_plan", lambda *args, **kwargs: fake_plan)
    monkeypatch.setattr(cli_mod, "_policy_findings", lambda *args, **kwargs: {"mode": "standard", "warnings": [], "blockers": []})
    monkeypatch.setattr(cli_mod, "install_inventory", lambda *args, **kwargs: {"items": []})
    monkeypatch.setattr(cli_mod, "apply_plan", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "ensure_dependencies", lambda *args, **kwargs: {"ok": True, "changed": False})
    monkeypatch.setattr(cli_mod, "verify_install", lambda *args, **kwargs: {"ok": True, "issues": []})
    monkeypatch.setattr(cli_mod, "rollback", lambda *args, **kwargs: {"removed": []})
    monkeypatch.setattr(cli_mod, "run_wizard", lambda **kwargs: 0)
    monkeypatch.setattr(cli_mod, "adapter_status", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "recorded_adapter_status", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "provenance_report", lambda *args, **kwargs: {"warnings": [], "repair_hints": []})
    monkeypatch.setattr(cli_mod, "run_doctor", lambda *args, **kwargs: {"ok": True, "warnings": [], "blockers": []})
    monkeypatch.setattr(cli_mod, "shell_registration_status", lambda *args, **kwargs: {"warnings": []})
    monkeypatch.setattr(cli_mod, "conservative_migrate", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "convert_repo", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "build_agent_context", lambda *args, **kwargs: {"blockers": []})
    monkeypatch.setattr(cli_mod, "render_markdown_report", lambda payload: "# stub\n")
    monkeypatch.setattr(cli_mod, "generate_alias_outputs", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "repo_finalizer_plan", lambda *args, **kwargs: {"ok": True, "action": "plan"})
    monkeypatch.setattr(cli_mod, "repo_finalizer_status", lambda *args, **kwargs: {"ok": True, "action": "status"})
    monkeypatch.setattr(cli_mod, "repo_finalizer_run", lambda *args, **kwargs: {"ok": True, "action": "run"})
    monkeypatch.setattr(cli_mod, "repo_finalizer_payload_to_text", lambda payload: f"{payload['action']}\n")
    monkeypatch.setattr(cli_mod, "harness_plan", lambda *args, **kwargs: {"ok": True, "action": "plan"})
    monkeypatch.setattr(cli_mod, "harness_init", lambda *args, **kwargs: {"ok": True, "action": "init"})
    monkeypatch.setattr(cli_mod, "harness_enable", lambda *args, **kwargs: {"ok": True, "action": "enable"})
    monkeypatch.setattr(cli_mod, "harness_disable", lambda *args, **kwargs: {"ok": True, "action": "disable"})
    monkeypatch.setattr(cli_mod, "harness_status", lambda *args, **kwargs: {"ok": True, "action": "status"})
    monkeypatch.setattr(cli_mod, "harness_budget", lambda *args, **kwargs: {"ok": True, "action": "budget"})
    monkeypatch.setattr(cli_mod, "harness_run", lambda *args, **kwargs: {"ok": True, "action": "run"})
    monkeypatch.setattr(cli_mod, "harness_payload_to_text", lambda payload: f"{payload['action']}\n")
    monkeypatch.setattr(cli_mod, "load_skill_catalog", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "load_workflow_catalog", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "diff_plan_current", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "skill_payload", lambda *args, **kwargs: {"count": 1, "skills": []})
    monkeypatch.setattr(cli_mod, "workflow_payload", lambda *args, **kwargs: {"count": 1, "workflows": []})
    monkeypatch.setattr(cli_mod, "pack_reasoning", lambda *args, **kwargs: {"packs": []})
    monkeypatch.setattr(cli_mod, "graph_payload", lambda *args, **kwargs: {"edges": []})
    monkeypatch.setattr(cli_mod, "adopt_recommendations", lambda *args, **kwargs: {"recommended_packs": []})
    monkeypatch.setattr(cli_mod, "write_source_sbom", lambda *args, **kwargs: {"ok": True, "kind": "source"})
    monkeypatch.setattr(cli_mod, "write_installed_sbom", lambda *args, **kwargs: {"ok": True, "kind": "installed"})
    monkeypatch.setattr(cli_mod, "validate_manifest_schemas", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "validate_plugin_pack_manifest", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "validate_package_surfaces", lambda *args, **kwargs: {"ok": True, "issues": []})
    monkeypatch.setattr(
        cli_mod,
        "load_plugin_pack_configs",
        lambda *args, **kwargs: [
            SimpleNamespace(
                plugin_id="localsetup-bootstrap",
                display_name="LocalSetup Bootstrap",
                description="Bootstrap plugin pack.",
                category="bootstrap",
                source_pack="bootstrap",
                platforms={"codex": {"interface": "v1"}},
            )
        ],
    )
    monkeypatch.setattr(cli_mod, "plan_plugin_packs", lambda *args, **kwargs: {"ok": True, "plugin_packs": []})
    monkeypatch.setattr(cli_mod, "build_codex_plugins", lambda *args, **kwargs: {"ok": True, "plugins": []})
    monkeypatch.setattr(cli_mod, "validate_codex_plugin_path", lambda *args, **kwargs: {"ok": True, "issues": []})
    monkeypatch.setattr(cli_mod, "validate_skill_catalog", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "validate_workflow_catalog", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "scan_legacy_references", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli_mod, "audit_global_first", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "run_maintainer_gate", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "plan_version", lambda *args, **kwargs: {"ok": True, "bump": "none", "target_version": "9.9.9"})
    monkeypatch.setattr(cli_mod, "push_lines_to_plans", lambda *args, **kwargs: [{"ok": True}])
    monkeypatch.setattr(cli_mod, "publish_preflight", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "check_version_files", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "sync_version_files", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "stage_version_files", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_mod, "commit_version_sync", lambda *args, **kwargs: "abc123")
    monkeypatch.setattr(cli_mod, "_run_self_refresh", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "register_shell_command", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "build_public_artifact", lambda *args, **kwargs: {"ok": True, "leaks": []})
    monkeypatch.setattr(cli_mod, "verify_release_artifact", lambda *args, **kwargs: {"ok": True})

    def fake_subprocess_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_subprocess_run)

    def run_cli(*args: str) -> str:
        code = cli_mod._main(["--source-root", str(root), "--home", str(home), *args])
        output = capsys.readouterr().out
        assert code == 0
        return output

    run_cli("plan", "--report", str(report), "--global-packs", "core", "--repo-packs", "dev")
    assert report.is_file()
    run_cli("install", "--apply", "--target-directory", str(root), "--platforms", "codex")
    run_cli("wizard", "--caller-directory", str(root), "--target-directory-origin", "inferred", "--no-register-shell")
    run_cli("verify", "--platforms", "codex")
    run_cli("rollback", "--platforms", "codex")
    run_cli("adapters", "--platforms", "codex")
    run_cli("adapters", "--provenance")
    run_cli("configure", "--home-override", str(home), "--global-preset", "core")
    run_cli("doctor", "--platforms", "codex")
    run_cli("provenance", "repair", "--plan")
    run_cli("provenance", "report", "--platforms", "codex")
    run_cli("migrate", "--dry-run", "--platforms", "codex")
    run_cli("convert", "--apply", "--global-packs", "core", "--repo-packs", "dev", "--platforms", "codex")
    run_cli("context", "--markdown", "--report", str(tmp_path / "context.md"))
    run_cli("generate-docs")
    run_cli("plugin", "list", "--platform", "codex")
    run_cli("plugin", "plan", "--platform", "codex", "--plugin-packs", "bootstrap", "--output", str(tmp_path / "plugin-plan"))
    run_cli("plugin", "build", "--platform", "codex", "--plugin-packs", "bootstrap", "--output", str(tmp_path / "plugin-build"))
    run_cli("plugin", "validate", "--platform", "codex", "--path", str(tmp_path / "plugin-build"))
    run_cli("harness", "repo-finalizer", "plan", "--json")
    run_cli("harness", "repo-finalizer", "status")
    run_cli("harness", "repo-finalizer", "run", "--json", "--no-commit", "--checkpoint", "--message", "checkpoint")
    run_cli("harness", "codex-heartbeat", "plan")
    run_cli("harness", "codex-heartbeat", "init")
    run_cli("harness", "codex-heartbeat", "enable", "--install-crontab", "--yes")
    run_cli("harness", "codex-heartbeat", "disable", "--install-crontab", "--yes")
    run_cli("harness", "codex-heartbeat", "status")
    run_cli("harness", "codex-heartbeat", "budget")
    run_cli("harness", "codex-heartbeat", "run", "--no-agent", "--force")
    run_cli("docs-align", "inventory")
    run_cli("context-index", "refresh")
    run_cli("catalog")
    run_cli("diff", "--packs", "core")
    run_cli("skill", "search", "context")
    run_cli("workflow", "search", "heartbeat")
    run_cli("why", "--packs", "core")
    run_cli("graph")
    run_cli("adopt", "--target-directory", str(root))
    run_cli("detach", "--platforms", "codex")
    run_cli("sbom", "--out", str(out))
    run_cli("sbom", "--installed", "--out", str(tmp_path / "installed.json"), "--target-directory", str(root))
    run_cli("validate-catalog")
    run_cli("scan-migration", "--include-expected")
    run_cli("audit-global-first", "--target-directory", str(root))
    run_cli("hook-gate", "--out", str(artifact), "--runner", "pytest")
    run_cli("version-plan")
    monkeypatch.setattr(cli_mod.sys, "stdin", io.StringIO("refs/heads/main abc refs/heads/main def\n"))
    run_cli("version-plan", "--push-stdin")
    run_cli("publish-preflight")
    run_cli("version-sync", "--check")
    run_cli("version-sync", "--target", "9.9.9", "--stage", "--commit")
    run_cli("release-push", "--", "origin", "main")
    run_cli("self-refresh", "--packs", "core", "--platforms", "codex")
    run_cli("install-hooks")
    run_cli("register-shell")
    run_cli("package", "--out", str(tmp_path / "pkg.tar.gz"))
    run_cli("verify-release", str(tmp_path / "pkg.tar.gz"), "--expected-tag", "v9.9.9")

    assert cli_mod.main(["configure", "--packs", "core,,dev"]) == 2
    assert "empty value in comma-separated list" in capsys.readouterr().err


def test_cli_self_refresh_mixed_modes_and_success_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    config = InstallConfig(target_directory=str(root), dependency_mode="prompt-only")

    monkeypatch.setattr(
        cli_mod,
        "_existing_target_platforms",
        lambda *args, **kwargs: [
            {"platform": "codex", "mode": "symlink"},
            {"platform": "cursor", "mode": "portable"},
        ],
    )
    # Mode discovery is limited to explicitly selected legacy clients.
    with pytest.raises(ValueError, match="mixed adapter modes"):
        cli_mod._run_self_refresh(root, config, home, platforms_override=["codex", "cursor"])

    fake_plan = SimpleNamespace(rollback_metadata={})
    monkeypatch.setattr("ls.core.apply_preflight.preflight_install_plan", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(cli_mod, "_existing_target_platforms", lambda *args, **kwargs: [{"platform": "codex", "mode": "symlink"}])
    monkeypatch.setattr(cli_mod, "build_install_plan", lambda *args, **kwargs: fake_plan)
    monkeypatch.setattr(cli_mod, "apply_plan", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(cli_mod, "verify_install", lambda *args, **kwargs: {"ok": True})

    refreshed = cli_mod._run_self_refresh(root, config, home, platforms_override=["codex"])
    assert refreshed["ok"] is True
    assert refreshed["selected"]["platforms"] == ["codex"]
    assert refreshed["selected"]["attach_mode"] == "symlink"


def test_wizard_control_helpers_cover_help_detail_and_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.StringIO()
    term = TerminalWizard(input_stream=io.StringIO(), output_stream=output, color=False)

    with monkeypatch.context() as terminal_patch:
        terminal_patch.setattr(wizard.shutil, "get_terminal_size", lambda fallback: (_ for _ in ()).throw(OSError("no tty")))
        assert term.width() == 88
    term.key_value_block([("Empty", "")])
    term.choice_row(1, Choice("risky", "Risky", "summary", "effect", "best", "tradeoff", caution="careful"))
    monkeypatch.setenv("TERM", "dumb")
    term.clear_screen()
    assert "\033[2J" not in output.getvalue()

    with pytest.raises(ValueError, match="invalid color mode"):
        TerminalWizard(io.StringIO(), io.StringIO(), color_mode="loud")
    with pytest.raises(ValueError, match="invalid glyphs mode"):
        TerminalWizard(io.StringIO(), io.StringIO(), glyph_mode="emoji")
    with monkeypatch.context() as terminal_patch:
        terminal_patch.setattr("builtins.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no tty")))
        with pytest.raises(RuntimeError, match="interactive installer requires a terminal"):
            wizard.open_tty()

    continue_term = TerminalWizard(io.StringIO("?\nd\nd\n\n"), io.StringIO(), color=False)
    assert wizard._continue_prompt(
        continue_term,
        "Continue",
        help_text="Helpful text.",
        detail_text="Detailed text.",
    ) == "continue"
    assert "Helpful text." in continue_term.output.getvalue()
    assert "Detailed text." in continue_term.output.getvalue()

    confirm_term = TerminalWizard(io.StringIO("?\nd\nyes\n"), io.StringIO(), color=False)
    assert wizard._confirm_apply(confirm_term) == "apply"
    assert "Type yes only after" in confirm_term.output.getvalue()
    assert "writes the managed library" in confirm_term.output.getvalue()
    assert wizard._confirm_apply(TerminalWizard(io.StringIO("no\n"), io.StringIO(), color=False)) == wizard.BACK
    assert wizard._blocker_prompt(TerminalWizard(io.StringIO("?\nd\nx\n"), io.StringIO(), color=False)) == wizard.BACK
    assert wizard._target_directory_prompt(TerminalWizard(io.StringIO("?\nd\n/tmp/repo\n"), io.StringIO(), color=False)) == "/tmp/repo"

    invalid_choice = TerminalWizard(io.StringIO("bad\nGlobal\n"), io.StringIO(), color=False)
    assert choose_one(invalid_choice, "Mode", [("global", "Global")], default="global") == "global"
    invalid_many = TerminalWizard(io.StringIO("bad\n0\n"), io.StringIO(), color=False)
    assert choose_many(invalid_many, "Packs", [("core", "Core")], default=[], allow_none=True) == []
