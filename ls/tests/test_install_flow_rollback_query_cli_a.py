from __future__ import annotations

from ls.tests.test_install_flow import *

def test_rollback_refuses_managed_marker_outside_global_root(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    outside = tmp_path / "outside-managed"
    outside.mkdir()
    (outside / ".localsetup-managed").write_text("source=bad\n", encoding="utf-8")
    (root / ".localsetup").mkdir()
    (root / ".localsetup/lock.json").write_text(
        f"""{{
  "platforms": [],
  "installed_skills": ["{outside}"]
}}
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="outside global root"):
        rollback(root, home)


def test_rollback_reads_legacy_lock_and_removes_relative_managed_adapter(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    pack_path = root / "ls" / "config" / "pack.yaml"
    pack_path.write_text(
        pack_path.read_text(encoding="utf-8").replace("  lockfile: .localsetup/lock.json", "  lockfile: custom-lock.json"),
        encoding="utf-8",
    )
    global_root = home / ".local" / "share" / "localsetup" / "packages"
    package = global_root / "ls-localsetup-project-context"
    package.mkdir(parents=True)
    (package / MARKER_JSON).write_text("{}\n", encoding="utf-8")
    adapter = root / "relative-adapter"
    adapter.symlink_to(global_root, target_is_directory=True)
    legacy_lock = root / "localsetup.lock.json"
    legacy_lock.write_text(
        json.dumps(
            {
                "installed_skills": [str(package)],
                "installed_workflows": [],
                "adapter_state": ["relative-adapter"],
            }
        ),
        encoding="utf-8",
    )

    result = rollback(root, home=home)

    assert str(package) in result["removed"]
    assert str(adapter) in result["removed"]
    assert str(global_root) in result["removed"]
    assert str(legacy_lock) in result["removed"]
    assert not adapter.exists()
    assert not global_root.exists()


def test_rollback_preserves_custom_adapter_entries(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(
        root,
        build_install_plan(
            root,
            home=home,
            global_packs=["core"],
            repo_preset="custom",
            repo_skills=["localsetup-context"],
            platform_ids=["codex"],
        ),
        home=home,
        dry_run=False,
    )
    adapter = root / ".agents" / "skills"
    custom = adapter / "custom-skill"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    result = rollback(root, home=home)

    assert str(adapter / "ls-localsetup-project-context") in result["removed"]
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom\n"
    assert not (adapter / ".localsetup-adapter.json").exists()
    assert not (adapter / "ls-localsetup-project-context").exists()


def test_rollback_legacy_portable_adapter_removes_managed_entries_only(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    global_root = home / ".local" / "share" / "localsetup" / "packages"
    managed = global_root / "ls-localsetup-project-context"
    managed.mkdir(parents=True)
    (managed / MARKER_JSON).write_text("{}\n", encoding="utf-8")
    adapter = root / ".codex" / "skills"
    adapter.mkdir(parents=True)
    (adapter / ".localsetup-portable").write_text("managed_by=localsetup\n", encoding="utf-8")
    shutil.copytree(managed, adapter / "ls-localsetup-project-context")
    custom = adapter / "custom-skill"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")
    lock = root / ".localsetup" / "lock.json"
    lock.parent.mkdir()
    lock.write_text(
        json.dumps(
            {
                "installed_skills": [],
                "installed_workflows": [],
                "adapter_state": [str(adapter)],
            }
        ),
        encoding="utf-8",
    )

    result = rollback(root, home=home)

    assert str(adapter / "ls-localsetup-project-context") in result["removed"]
    assert not (adapter / "ls-localsetup-project-context").exists()
    assert not (adapter / ".localsetup-portable").exists()
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom\n"


def test_managed_adapter_removal_ignores_unsafe_marker_package_names(tmp_path: Path) -> None:
    from ls.core.adapters import ADAPTER_MARKER_JSON, remove_managed_adapter_entries

    global_root = tmp_path / "global"
    managed = global_root / "ls-localsetup-project-context"
    managed.mkdir(parents=True)
    (managed / MARKER_JSON).write_text("{}\n", encoding="utf-8")
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / ADAPTER_MARKER_JSON).write_text(
        json.dumps(
            {
                "version": 1,
                "managed_by": "localsetup",
                "mode": "symlink",
                "packages": ["ls-localsetup-project-context", "../outside-link", "nested/name", str(tmp_path / "absolute-link")],
            }
        ),
        encoding="utf-8",
    )
    (adapter / "ls-localsetup-project-context").symlink_to(managed, target_is_directory=True)
    outside_link = tmp_path / "outside-link"
    outside_link.symlink_to(managed, target_is_directory=True)
    absolute_link = tmp_path / "absolute-link"
    absolute_link.symlink_to(managed, target_is_directory=True)

    removed = remove_managed_adapter_entries(
        adapter,
        global_root,
        recorded_packages=["../outside-link", "nested/name", str(absolute_link)],
    )

    assert str(adapter / "ls-localsetup-project-context") in removed
    assert outside_link.is_symlink()
    assert absolute_link.is_symlink()
    assert not (adapter / "ls-localsetup-project-context").exists()


def test_adapter_update_ignores_unsafe_old_marker_package_names(tmp_path: Path) -> None:
    from ls.core.adapters import ADAPTER_MARKER_JSON

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    global_root = home / ".local" / "share" / "localsetup" / "packages"
    managed = global_root / "ls-localsetup-project-context"
    managed.mkdir(parents=True)
    (managed / MARKER_JSON).write_text("{}\n", encoding="utf-8")
    (global_root / "outside-link").mkdir()
    (global_root / "absolute-link").mkdir()
    adapter = root / ".codex" / "skills"
    adapter.mkdir(parents=True)
    (adapter / ADAPTER_MARKER_JSON).write_text(
        json.dumps(
            {
                "version": 1,
                "managed_by": "localsetup",
                "mode": "symlink",
                "packages": ["ls-localsetup-project-context", "../outside-link", "nested/name", str(tmp_path / "absolute-link")],
            }
        ),
        encoding="utf-8",
    )
    (adapter / "ls-localsetup-project-context").symlink_to(managed, target_is_directory=True)
    outside_link = root / ".codex" / "outside-link"
    outside_link.symlink_to(global_root / "outside-link", target_is_directory=True)
    absolute_link = tmp_path / "absolute-link"
    absolute_link.symlink_to(global_root / "absolute-link", target_is_directory=True)

    plan = build_install_plan(
        root,
        home=home,
        global_packs=["core"],
        repo_preset="custom",
        repo_skills=["localsetup-context"],
        platform_ids=["codex"],
    )
    apply_plan(root, plan, home=home, dry_run=False)

    assert outside_link.is_symlink()
    assert absolute_link.is_symlink()
    assert not (adapter / "ls-localsetup-project-context").exists()
    assert (root / ".agents" / "skills" / "ls-localsetup-project-context").is_symlink()


def test_repo_path_rejects_symlink_parent_escape(tmp_path: Path) -> None:
    from ls.core.paths import PathValidationError, repo_path

    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathValidationError, match="parent escapes"):
        repo_path(root, "link/adapter", "test.path")


def test_tar_leak_scan_detects_private_names(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    leak = root / "ls" / "token.secret"
    leak.write_text("do not ship\n", encoding="utf-8")
    artifact = tmp_path / "localsetup-public.tar.gz"

    package = build_public_artifact(root, artifact)

    assert "ls/token.secret" in package["leaks"]
    assert scan_tar_for_leaks(artifact, [".localsetup-maint"]) == package["leaks"]


def test_query_payloads_cover_catalog_reasoning_graph_and_adoption(tmp_path: Path) -> None:
    from ls.core.query import adopt_recommendations, graph_payload, pack_reasoning, skill_payload, workflow_payload

    root = make_temp_repo(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "package.json").write_text("{}", encoding="utf-8")
    (target / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (target / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (target / "main.tf").write_text("terraform {}\n", encoding="utf-8")
    (target / "nginx.conf").write_text("events {}\n", encoding="utf-8")
    (target / "demo.service").write_text("[Service]\n", encoding="utf-8")
    (target / ".github" / "workflows").mkdir(parents=True)

    skills = skill_payload(root, "context")
    workflows = workflow_payload(root, "repo-finalizer")
    reasoning = pack_reasoning(root, ["core"])
    graph = graph_payload(root)
    adoption = adopt_recommendations(target)

    assert skills["count"] >= 1
    assert all("risk" in item and "permissions" in item for item in skills["skills"])
    assert workflows["count"] >= 1
    assert reasoning["packs"][0]["reason"] == "selected explicitly"
    assert any(edge["type"] == "pack_skill" for edge in graph["edges"])
    assert adoption["signals"]["node"] is True
    assert adoption["signals"]["python"] is True
    assert adoption["signals"]["docker"] is True
    assert adoption["signals"]["github_actions"] is True
    assert adoption["signals"]["terraform"] is True
    assert adoption["signals"]["nginx"] is True
    assert adoption["signals"]["systemd"] is True


def test_global_first_audit_reports_legacy_and_doc_claims(tmp_path: Path) -> None:
    from ls.core.global_first_audit import _relative, audit_global_first

    source = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    (target / "ls").mkdir()
    (target / "localsetup.lock.json").write_text("{}", encoding="utf-8")
    (source / "install.ps1").write_text("retired\n", encoding="utf-8")
    (source / "ls" / "tools" / "deploy").write_text("legacy\n", encoding="utf-8")
    (source / "README.md").write_text(
        "Run python3 ls/tools/localsetup.py verify here.\n"
        "Allowed source-checkout command: python3 ls/tools/localsetup.py verify --source-root . --target-directory .\n",
        encoding="utf-8",
    )
    old_root = home / ".local" / "share" / "agents" / "skills" / "localsetup"
    old_root.mkdir(parents=True)
    package_root = home / ".local" / "share" / "localsetup" / "packages"
    (package_root / "localsetup-old").mkdir(parents=True)

    payload = audit_global_first(source, home=home, target_root=target)

    blocker_kinds = {item["kind"] for item in payload["blockers"]}
    warning_kinds = {item["kind"] for item in payload["warnings"]}
    assert payload["ok"] is False
    assert "stale_framework_source" in blocker_kinds
    assert "legacy_root_lockfile" in blocker_kinds
    assert "retired_powershell_surface" in blocker_kinds
    assert "legacy_deploy_surface" in blocker_kinds
    assert "docs_claim" in blocker_kinds
    assert "legacy_package_root" in warning_kinds
    assert any(
        observation["kind"] == "legacy_package_dirs" and observation["present"] == ["localsetup-old"]
        for observation in payload["observations"]
    )
    assert _relative(tmp_path / "outside.md", source).endswith("outside.md")


def test_diff_plan_current_compares_lockfile_to_planned_selection(tmp_path: Path) -> None:
    from ls.core.diffing import diff_plan_current

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(root, build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"]), home=home)

    diff = diff_plan_current(
        root,
        home=home,
        packs=None,
        global_packs=["dev"],
        repo_packs=["dev"],
        platform_ids=["codex", "claude-code"],
        target_root=None,
        attach_mode="symlink",
    )

    assert "ls-nodejs-nextjs-development" in diff["skills"]["added"]
    assert any(path.endswith(".claude/skills") for path in diff["adapters"]["added"])
    assert diff["has_lockfile"] is True


def test_legacy_wizard_advanced_selector_steps_remain_callable(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    state = wizard.WizardState(
        repo_root=root,
        home=tmp_path / "home",
        caller_directory=root,
        target_directory=root,
        packs=["core"],
        platforms=["codex"],
    )
    term = TerminalWizard(
        input_stream=io.StringIO("\n\n\n\n\n"),
        output_stream=io.StringIO(),
        color=False,
    )

    assert wizard._skill_group_step(term, state) == "continue"
    assert wizard._skill_individual_step(term, state) == "continue"
    assert wizard._options_step(term, state) == "continue"
    assert state.skills
    assert state.attach_mode == "symlink"
    assert state.dependency_mode == "prompt-only"
