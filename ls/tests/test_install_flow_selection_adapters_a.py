from __future__ import annotations

import os
import shutil
import subprocess
import sys

from ls.tests.test_install_flow import *
from ls.core.adapters import adapter_targets
from ls.core.apply_journal import restore_failed_mutations, write_journal
from ls.core.apply_packages import install_managed_packages, install_shared_runtime_lib
from ls.core.apply_preflight import preflight_install_plan
from ls.core.selection import resolve_package_selection

def test_plan_apply_verify_rollback(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    plan = build_install_plan(root, home=home, packs=["core"])
    assert not any(a.kind == "attach_repo_path" for a in plan.actions)
    assert plan.rollback_metadata["platforms"] == []
    assert plan.rollback_metadata["global_only"] is True

    result = apply_plan(root, plan, home=home, dry_run=False)
    assert result["dry_run"] is False
    assert result["transaction"]
    journal = load_json(Path(result["journal"]))
    assert journal["status"] == "committed"
    assert journal["txid"] == result["transaction"]
    assert not (root / ".localsetup" / "staging" / result["transaction"]).exists()
    assert not (home / ".local/share/localsetup/packages/.localsetup-staging" / result["transaction"]).exists()
    assert any(item["kind"] == "staging_root" for item in journal["touched"])

    verify = verify_install(root, home)
    assert verify["ok"] is True
    assert verify["provenance"]["ok"] is True
    assert verify["provenance_warnings"] == []
    assert (home / ".local/share/localsetup/packages/ls-context").is_dir()
    marker = load_json(home / ".local/share/localsetup/packages/ls-context" / MARKER_JSON)
    assert marker["schema_version"] == 1
    assert marker["framework_version"]
    assert marker["source_commit"]
    assert marker["source_tree_sha"]
    assert marker["source_dirty"] in {False, True}
    assert marker["emitter"] == "package-install"
    assert marker["package_name"] == "ls-context"
    assert marker["package_type"] == "skill"
    assert marker["artifact_sha256"] == marker["package_digest"]
    assert marker["transform_manifest_digest"]
    assert marker["artifact_path"] == str(home / ".local/share/localsetup/packages/ls-context")
    assert marker["marker_path"] == str(home / ".local/share/localsetup/packages/ls-context" / MARKER_JSON)
    assert (home / ".local/share/localsetup/packages/ls-context/references/localsetup/.localsetup-reference-bundle.json").is_file()
    assert not (home / ".local/share/localsetup/packages/ls-cloudflare-dns").exists()
    assert verify["adapters"] == []
    lock = load_json(root / ".localsetup/lock.json")
    assert lock["platforms"] == []
    assert lock["adapter_state"] == []
    assert lock["package_provenance"]["ls-context"]["package_digest"] == marker["package_digest"]
    for rel in (".agents/skills", ".claude/skills", ".cursor/skills", ".kilo/skills", ".opencode/skills"):
        assert not (root / rel).exists()

    rolled = rollback(root, home)
    assert rolled["removed"]
    assert verify_install(root, home)["ok"] is False


def test_shared_agents_adapter_is_coalesced_across_platforms(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    selected = ["codex", "cursor", "openclaw"]

    targets = adapter_targets(root, home, platform_ids=selected)
    shared = next(target for target in targets if target["repo_path"] == root / ".agents" / "skills")
    assert shared["platforms"] == selected
    assert len([target for target in targets if target["repo_path"] == shared["repo_path"]]) == 1

    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=selected)
    attach_actions = [action for action in plan.actions if action.kind == "attach_repo_path"]
    shared_actions = [action for action in attach_actions if action.path == shared["repo_path"]]
    assert len(shared_actions) == 1
    assert shared_actions[0].details["platforms"] == selected

    result = apply_plan(root, plan, home=home, dry_run=False)
    lock = load_json(root / ".localsetup" / "lock.json")
    shared_locks = [item for item in lock["adapter_targets"] if item["path"] == str(shared["repo_path"])]
    assert len(shared_locks) == 1
    assert shared_locks[0]["platforms"] == selected
    assert shared_locks[0]["owners"] == [{"scope": "repo", "root": str(root.resolve()), "client": client} for client in selected]
    assert lock["adapter_state"].count(str(shared["repo_path"])) == 1

    verified = verify_install(root, home, platform_ids=selected)
    assert verified["ok"] is True
    assert len([item for item in verified["adapters"] if item["repo_path"] == str(shared["repo_path"])]) == 1
    shared_rules = [
        rule
        for rule in verified["rules"]
        if rule.get("path") == str(shared["repo_path"]) or rule.get("platforms") == selected
    ]
    assert {rule["rule"] for rule in shared_rules} >= {
        "adapter_path_exists",
        "adapter_points_to_managed_root",
        "adapter_visible_packages_match_selection",
        "adapter_package_targets_match_managed_root",
        "namespace_ls",
        "skills_visible_filesystem",
    }
    assert all(rule["platform"] == "codex" for rule in shared_rules)
    assert all(rule["platforms"] == selected for rule in shared_rules)

    rolled = rollback(root, home)
    assert rolled["removed"].count(str(shared["repo_path"])) <= 1
    assert not shared["repo_path"].exists()
    journal = load_json(Path(result["journal"]))
    assert journal["status"] == "committed"
    assert len(
        [item for item in journal["touched"] if item["kind"] == "adapter" and item["path"] == str(shared["repo_path"])]
    ) == 1


def test_selected_workflows_install_as_skill_packages(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    plan = build_install_plan(root, home=home, packs=["ops"], platform_ids=["codex"])
    workflow_action = next(a for a in plan.actions if a.kind == "install_workflows")
    assert "ls-workflow-ops-tmux-session" in workflow_action.details["workflows"]
    assert "ls-workflow-pipeline-server-triage-patch" in workflow_action.details["workflows"]
    assert "ls-linux-patcher" in plan.rollback_metadata["skills"]

    result = apply_plan(root, plan, home=home, dry_run=False)
    lock = load_json(root / ".localsetup/lock.json")
    global_root = home / ".local/share/localsetup/packages"

    assert result["dry_run"] is False
    assert (global_root / "ls-workflow-ops-tmux-session" / "SKILL.md").is_file()
    assert (global_root / "ls-workflow-ops-tmux-session" / "workflow.yaml").is_file()
    assert "ls-workflow-ops-tmux-session" in lock["workflows"]
    assert any(path.endswith("ls-workflow-ops-tmux-session") for path in lock["installed_workflows"])
    assert verify_install(root, home, platform_ids=["codex"])["ok"] is True


def test_core_installs_tmux_workflow_packages(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    workflow_action = next(a for a in plan.actions if a.kind == "install_workflows")

    assert "ls-workflow-ops-tmux-session" in workflow_action.details["workflows"]
    assert "ls-workflow-tmux-terminal-mode" in workflow_action.details["workflows"]

    apply_plan(root, plan, home=home, dry_run=False)
    lock = load_json(root / ".localsetup/lock.json")
    global_root = home / ".local/share/localsetup/packages"

    assert (global_root / "ls-workflow-ops-tmux-session" / "SKILL.md").is_file()
    assert (global_root / "ls-workflow-tmux-terminal-mode" / "workflow.yaml").is_file()
    assert "ls-workflow-ops-tmux-session" in lock["workflows"]
    assert "ls-workflow-tmux-terminal-mode" in lock["workflows"]
    verify = verify_install(root, home, platform_ids=["codex"])
    doctor = run_doctor(root, home=home, platform_ids=["codex"])
    assert verify["ok"] is True
    assert verify["tmux_terminal_mode"]["workflows"]["lock_present"] == [
        "ls-workflow-ops-tmux-session",
        "ls-workflow-tmux-terminal-mode",
    ]
    assert verify["tmux_terminal_mode"]["workflows"]["adapters"][0]["missing_workflows"] == []
    assert doctor["tmux_terminal_mode"]["workflows"]["adapters"][0]["missing_workflows"] == []


def test_installed_omniroute_helpers_can_import_shared_deps(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    global_root = home / ".local/share/localsetup/packages"
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "lib", root / "ls/lib")

    install_managed_packages(
        root,
        global_root,
        ["ls-omniroute", "ls-omniroute-admin-automation", "ls-omniroute-proxy"],
        "skills",
        home=home,
        replace_func=os.replace,
    )

    assert (home / ".local/share/localsetup/lib/deps.py").is_file()
    for helper in (
        global_root / "ls-omniroute/scripts/omniroute_api.py",
        global_root / "ls-omniroute-proxy/scripts/omniroute_discover.py",
        global_root / "ls-omniroute-admin-automation/scripts/omniroute_admin.py",
    ):
        result = subprocess.run(
            [sys.executable, str(helper), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "ModuleNotFoundError" not in result.stderr


def test_shared_runtime_lib_replaces_symlink_without_following(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "lib", root / "ls/lib")
    home = tmp_path / "home"
    global_root = home / ".local/share/localsetup/packages"
    runtime_lib = home / ".local/share/localsetup/lib"
    runtime_lib.mkdir(parents=True)
    outside = tmp_path / "outside-deps.py"
    outside.write_text("outside sentinel\n", encoding="utf-8")
    (runtime_lib / "deps.py").symlink_to(outside)

    installed = install_shared_runtime_lib(root, global_root, replace_func=os.replace)

    target = runtime_lib / "deps.py"
    assert installed == [str(target)]
    assert target.is_file()
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == (source / "lib/deps.py").read_text(encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == "outside sentinel\n"


def test_failed_shared_runtime_install_restores_existing_symlink(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "lib", root / "ls/lib")
    home = tmp_path / "home"
    global_root = home / ".local/share/localsetup/packages"
    runtime_lib = home / ".local/share/localsetup/lib"
    runtime_lib.mkdir(parents=True)
    outside = tmp_path / "outside-deps.py"
    outside.write_text("outside sentinel\n", encoding="utf-8")
    target = runtime_lib / "deps.py"
    target.symlink_to(outside)
    journal_path = tmp_path / "journal.json"
    journal = {"touched": []}
    write_journal(journal_path, journal)

    install_shared_runtime_lib(root, global_root, journal=journal, journal_path=journal_path, replace_func=os.replace)
    restore_failed_mutations(journal, os.replace)

    assert target.is_symlink()
    assert target.readlink() == outside
    assert target.read_text(encoding="utf-8") == "outside sentinel\n"


def test_shared_runtime_lib_refuses_symlinked_lib_dir(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "lib", root / "ls/lib")
    home = tmp_path / "home"
    global_root = home / ".local/share/localsetup/packages"
    localsetup_home = home / ".local/share/localsetup"
    localsetup_home.mkdir(parents=True)
    outside_lib = tmp_path / "outside-lib"
    outside_lib.mkdir()
    (localsetup_home / "lib").symlink_to(outside_lib, target_is_directory=True)

    with pytest.raises(RuntimeError, match="refusing to install shared runtime lib through symlink"):
        install_shared_runtime_lib(root, global_root, replace_func=os.replace)

    assert not (outside_lib / "deps.py").exists()


def test_full_rollback_removes_managed_shared_runtime_helper(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "lib", root / "ls/lib")
    lock = root / ".localsetup/lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps({"installed_skills": [], "installed_workflows": [], "adapter_state": []}),
        encoding="utf-8",
    )
    target = home / ".local/share/localsetup/lib/deps.py"
    target.parent.mkdir(parents=True)
    shutil.copy2(root / "ls/lib/deps.py", target)

    result = rollback(root, home)

    assert str(target) in result["removed"]
    assert not target.exists()


def test_full_rollback_preserves_custom_shared_runtime_helper(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    lock = root / ".localsetup/lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps({"installed_skills": [], "installed_workflows": [], "adapter_state": []}),
        encoding="utf-8",
    )
    target = home / ".local/share/localsetup/lib/deps.py"
    target.parent.mkdir(parents=True)
    target.write_text("custom helper\n", encoding="utf-8")

    result = rollback(root, home)

    assert str(target) not in result["removed"]
    assert target.read_text(encoding="utf-8") == "custom helper\n"


def test_codex_platform_installs_guardian_subagent(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    agent_action = next(a for a in plan.actions if a.kind == "install_codex_agents")
    assert agent_action.path == home / ".codex" / "agents"
    assert agent_action.details["agents"] == ["guardian_subagent"]

    result = apply_plan(root, plan, home=home)
    agent_path = home / ".codex" / "agents" / "guardian_subagent.toml"
    lock = load_json(root / ".localsetup/lock.json")
    text = agent_path.read_text(encoding="utf-8")

    assert agent_path.is_file()
    assert 'model = "gpt-5.5"' in text
    assert 'model_reasoning_effort = "low"' in text
    assert str(agent_path) in result["installed_codex_agents"]
    assert str(agent_path) in lock["installed_codex_agents"]
    assert lock["codex_agents"] == ["guardian_subagent"]


def test_codex_agent_conflict_blocks_overwrite(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    agent_path = home / ".codex" / "agents" / "guardian_subagent.toml"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text("name = \"guardian_subagent\"\nmodel = \"custom\"\n", encoding="utf-8")

    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])

    with pytest.raises(RuntimeError, match="codex_agent_conflict"):
        apply_plan(root, plan, home=home)

    assert agent_path.read_text(encoding="utf-8") == "name = \"guardian_subagent\"\nmodel = \"custom\"\n"


def test_codex_agent_preserve_does_not_allow_symlink_target(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    agent_path = home / ".codex" / "agents" / "guardian_subagent.toml"
    agent_path.parent.mkdir(parents=True)
    agent_path.symlink_to(home / "missing-agent.toml")

    plan = build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"])
    plan.codex_agent_conflict = "preserve"
    preflight = preflight_install_plan(root, plan, home=home)

    assert not preflight["ok"]
    assert preflight["preserved_codex_agents"] == []
    assert any(
        blocker["status_code"] == "codex_agent_conflict" and blocker["path"] == str(agent_path)
        for blocker in preflight["blockers"]
    )


def test_selection_resolves_preset_classes_tags_skills_and_exclusions(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    plan = build_install_plan(
        root,
        home=home,
        preset="custom",
        skills=["localsetup-context"],
        skill_classes=["operations"],
        skill_tags=["git"],
        exclude_skills=["ls-linux-patcher"],
    )

    selected = set(plan.rollback_metadata["skills"])
    assert "ls-context" in selected
    assert "ls-git-workflows" in selected
    assert "ls-system-info" in selected
    assert "ls-linux-patcher" not in selected
    assert plan.rollback_metadata["packs"] == []
    assert plan.rollback_metadata["selectors"]["skill_classes"] == ["operations"]
    assert plan.rollback_metadata["selectors"]["skill_tags"] == ["git"]


def test_selection_resolves_normal_profile_from_pack_config(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)

    selected = resolve_package_selection(root, preset="normal")

    assert selected.preset == "normal"
    assert selected.packs == ["bootstrap", "core", "dev", "frontend", "architecture", "ops", "publishing"]
    assert selected.selectors["preset"] == "normal"
    assert selected.selectors["packs"] == selected.packs


def test_selection_explicit_packs_override_normal_profile_contents(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)

    selected = resolve_package_selection(root, preset="normal", packs=["core"])

    assert selected.preset == "normal"
    assert selected.packs == ["core"]
    assert selected.selectors["packs"] == ["core"]


def test_selector_free_plan_uses_normal_global_baseline_without_repo_adapters(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    plan = build_install_plan(root, home=home)

    assert plan.rollback_metadata["global_baseline_selectors"]["preset"] == "normal"
    assert plan.rollback_metadata["global_baseline_packs"] == [
        "bootstrap",
        "core",
        "dev",
        "frontend",
        "architecture",
        "ops",
        "publishing",
    ]
    assert plan.rollback_metadata["repo_packs"] == []
    assert not any(action.kind == "attach_repo_path" for action in plan.actions)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"preset": "unknown"}, "unknown preset: unknown"),
        ({"packs": ["unknown"]}, "unknown pack"),
        ({"skill_classes": ["unknown"]}, "unknown skill class"),
        ({"skill_tags": ["unknown"]}, "unknown skill tag"),
        ({"skills": ["unknown"]}, "unknown skill selector: unknown"),
        ({"exclude_skills": ["unknown"]}, "unknown excluded skill: unknown"),
    ],
)
def test_selection_rejects_unknown_selectors(tmp_path: Path, kwargs: dict, message: str) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    with pytest.raises(ValueError, match=message):
        build_install_plan(root, home=home, **kwargs)


def test_selection_keeps_workflow_required_skills_after_exclusion(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    plan = build_install_plan(
        root,
        home=home,
        packs=["publishing"],
        exclude_skills=["ls-framework-audit"],
    )

    assert "ls-workflow-pipeline-pre-publish" in plan.rollback_metadata["workflows"]
    assert "ls-framework-audit" in plan.rollback_metadata["skills"]
    assert plan.rollback_metadata["selectors"]["exclude_skills"] == ["ls-framework-audit"]


def test_omniroute_hard_dependencies_are_installed_even_when_excluded(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    plan = build_install_plan(
        root,
        home=home,
        preset="custom",
        skills=["ls-omniroute"],
        exclude_skills=["ls-omniroute-proxy"],
    )
    expected = {
        "ls-omniroute",
        "ls-omniroute-admin-automation",
        "ls-omniroute-proxy",
        "ls-omniroute-update",
    }
    assert set(plan.rollback_metadata["skills"]) == expected

    apply_plan(root, plan, home=home)
    package_root = home / ".local/share/localsetup/packages"
    assert {path.name for path in package_root.iterdir() if path.name.startswith("ls-omniroute")} == expected


def test_scoped_adapter_exposes_only_selected_packages_even_when_global_has_more(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    apply_plan(
        root,
        build_install_plan(root, home=home, global_packs=["dev"], repo_packs=["core"], platform_ids=["codex"]),
        home=home,
    )

    global_root = home / ".local/share/localsetup/packages"
    adapter = root / ".agents" / "skills"
    assert (global_root / "ls-nodejs-nextjs").is_dir()
    assert_scoped_adapter(adapter, "ls-context")
    assert not (adapter / "ls-nodejs-nextjs").exists()
    assert verify_install(root, home, platform_ids=["codex"])["ok"] is True
    doctor = run_doctor(root, home=home)
    assert not any(
        artifact["kind"] == "unmanaged_adapter" and Path(artifact["path"]) == adapter
        for artifact in doctor["legacy"]["artifacts"]
    )


def test_split_global_and_repo_packs_install_union_but_expose_repo_subset(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    plan = build_install_plan(
        root,
        home=home,
        global_packs=["dev"],
        repo_packs=["core"],
        platform_ids=["codex"],
    )
    apply_plan(root, plan, home=home)

    global_root = home / ".local/share/localsetup/packages"
    adapter = root / ".agents" / "skills"
    lock = load_json(root / ".localsetup/lock.json")
    registry = load_json(home / ".local/share/localsetup/registry.json")

    assert (global_root / "ls-nodejs-nextjs").is_dir()
    assert (global_root / "ls-context").is_dir()
    assert (adapter / "ls-context").exists()
    assert not (adapter / "ls-nodejs-nextjs").exists()
    assert "ls-nodejs-nextjs" in lock["global_baseline_packages"]
    assert "ls-nodejs-nextjs" not in lock["repo_packages"]
    assert lock["adapter_packages"] == lock["repo_packages"]
    assert lock["adapter_targets"][0]["packages"] == lock["repo_packages"]
    assert "ls-nodejs-nextjs" in registry["global_baseline"]["packages"]
    assert "ls-nodejs-nextjs" not in registry["targets"][str(root.resolve())]["repo_selection"]["packages"]
    assert verify_install(root, home, platform_ids=["codex"])["ok"] is True


def test_legacy_selector_flags_apply_to_global_and_repo_selection(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    apply_plan(root, build_install_plan(root, home=home, packs=["dev"], platform_ids=["codex"]), home=home)

    lock = load_json(root / ".localsetup/lock.json")
    assert "ls-nodejs-nextjs" in lock["global_baseline_packages"]
    assert "ls-nodejs-nextjs" in lock["repo_packages"]
    assert (root / ".agents" / "skills" / "ls-nodejs-nextjs").exists()


def test_global_selector_aliases_do_not_imply_repo_visibility(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    apply_plan(root, build_install_plan(root, home=home, global_packs=["dev"], platform_ids=["codex"]), home=home)

    lock = load_json(root / ".localsetup/lock.json")
    adapter = root / ".agents" / "skills"

    assert "ls-nodejs-nextjs" in lock["global_baseline_packages"]
    assert "ls-nodejs-nextjs" not in lock["repo_packages"]
    assert (home / ".local/share/localsetup/packages/ls-nodejs-nextjs").is_dir()
    assert not (adapter / "ls-nodejs-nextjs").exists()
    assert (adapter / "ls-context").exists()


def test_scoped_adapter_detects_tampered_child_symlink(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    apply_plan(root, build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"]), home=home)

    adapter = root / ".agents" / "skills"
    bad_target = tmp_path / "elsewhere" / "ls-context"
    bad_target.mkdir(parents=True)
    (bad_target / "SKILL.md").write_text("---\nname: ls-context\n---\n", encoding="utf-8")
    (adapter / "ls-context").unlink()
    (adapter / "ls-context").symlink_to(bad_target, target_is_directory=True)

    verify = verify_install(root, home, platform_ids=["codex"])
    doctor = run_doctor(root, home=home, platform_ids=["codex"])

    assert verify["ok"] is False
    assert any("adapter package target mismatch" in issue for issue in verify["issues"])
    assert any(
        warning == "scoped adapter package target differs from managed package: ls-context"
        for warning in verify["provenance_warnings"]
    )
    assert doctor["ok"] is False
    assert any("adapter package target mismatch (ls-context)" in blocker for blocker in doctor["blockers"])


@pytest.mark.parametrize(
    ("attach_mode", "marker_text", "reason"),
    [
        ("symlink", "{not-json", "adapter marker is not valid JSON"),
        ("symlink", '{"version": 1}', "adapter marker has unsupported mode"),
        ("symlink", '{"mode": "elsewhere"}', "adapter marker has unsupported mode"),
        ("portable", "{not-json", "adapter marker is not valid JSON"),
        ("portable", '{"version": 1}', "adapter marker has unsupported mode"),
        ("portable", '{"mode": "elsewhere"}', "adapter marker has unsupported mode"),
    ],
)
def test_adapter_invalid_marker_fails_integrity(
    tmp_path: Path, attach_mode: str, marker_text: str, reason: str
) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    apply_plan(
        root,
        build_install_plan(root, home=home, packs=["core"], attach_mode=attach_mode, platform_ids=["codex"]),
        home=home,
    )
    adapter = root / ".agents" / "skills"
    (adapter / ".localsetup-adapter.json").write_text(marker_text, encoding="utf-8")

    verify = verify_install(root, home, platform_ids=["codex"])
    doctor = run_doctor(root, home=home)

    assert verify["ok"] is False
    assert any(reason in str(failure.get("reason")) for failure in verify["adapters"][0]["package_integrity_failures"])
    assert any("scoped adapter integrity failure" in warning and reason in warning for warning in verify["provenance_warnings"])
    assert doctor["ok"] is False
    assert any("adapter package target mismatch (adapter marker)" in blocker for blocker in doctor["blockers"])
