"""External discovery is bounded, read-only, and distinct from framework trust."""
from pathlib import Path
import os

import pytest

from ls.core.skills_external import discover_external_skills, inspect_external_skill


def skill(library: Path, name="team-review", *, contract="") -> Path:
    path = library / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review the team's changes.\n{contract}---\n"
        "Run the review procedure.\n"
    )
    return path


def test_same_name_sources_have_distinct_immutable_storage(tmp_path):
    first = skill(tmp_path / "repo-a/.agents/skills")
    second = skill(tmp_path / "repo-b/.agents/skills")
    a = inspect_external_skill(first, "repo")
    b = inspect_external_skill(second, "repo")
    assert a.name == b.name and a.source_digest == b.source_digest
    assert a.package_id != b.package_id
    assert a.artifact_path(tmp_path / "packages") != b.artifact_path(tmp_path / "packages")
    assert a.artifact_path(tmp_path / "packages").name == a.name
    original = (first / "SKILL.md").read_bytes()
    (first / "notes.txt").write_text("new source revision")
    newer = inspect_external_skill(first, "repo")
    assert newer.origin_id == a.origin_id
    assert newer.package_id != a.package_id
    assert (first / "SKILL.md").read_bytes() == original


def test_discovery_preserves_originals_and_does_not_execute(tmp_path):
    target, home = tmp_path / "repo", tmp_path / "home"
    source = skill(target / ".agents/skills")
    personal = skill(home / ".agents/skills", "personal-review")
    (source / "script.py").write_text("raise RuntimeError('must never execute')")
    original = {p: p.read_bytes() for p in source.iterdir()}
    rows, findings = discover_external_skills(target, home)
    assert not findings
    assert {r.selector for r in rows} == {"repo:team-review", "personal:personal-review"}
    assert all(r.compatibility == "format-only" for r in rows)
    assert {p: p.read_bytes() for p in source.iterdir()} == original
    assert list(personal.iterdir()) == [personal / "SKILL.md"]


def test_contract_drift_is_reported_without_rewriting_source(tmp_path):
    source = skill(tmp_path, contract='metadata:\n  localsetup-contract: "99"\n')
    before = (source / "SKILL.md").read_bytes()
    row = inspect_external_skill(source, "repo")
    assert row.compatibility == "alignment-required"
    assert row.contract_version == 99
    assert (source / "SKILL.md").read_bytes() == before


def test_escaping_resources_and_managed_copies_are_rejected(tmp_path):
    source = skill(tmp_path / "library")
    (source / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlinks"):
        inspect_external_skill(source, "repo")
    (source / "escape").unlink()
    (source / ".localsetup-managed.json").write_text("{}")
    with pytest.raises(ValueError, match="Managed package"):
        inspect_external_skill(source, "repo")


def test_size_limits_precede_source_loading(tmp_path, monkeypatch):
    from ls.core import skills_external
    source = skill(tmp_path)
    (source / "large.bin").write_bytes(b"x" * 100)
    monkeypatch.setattr(skills_external, "MAX_FILE_BYTES", 80)
    with pytest.raises(ValueError, match="bounded regular"):
        inspect_external_skill(source, "repo")


def test_crlf_preserved_and_readonly_directories_rejected(tmp_path):
    source = skill(tmp_path)
    document = source / "SKILL.md"
    original = document.read_bytes().replace(b"\n", b"\r\n")
    document.write_bytes(original)
    assert inspect_external_skill(source, "repo").name == source.name
    assert document.read_bytes() == original
    source.chmod(0o555)
    try:
        with pytest.raises(ValueError, match="writable"):
            inspect_external_skill(source, "repo")
        assert document.read_bytes() == original
    finally:
        source.chmod(0o755)


def test_qualified_selection_and_name_collision(tmp_path):
    from ls.core.selection import resolve_package_selection
    root = Path(__file__).resolve().parents[2]
    target, home = tmp_path / "repo", tmp_path / "home"
    skill(target / ".agents/skills")
    skill(home / ".agents/skills")
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_package_selection(root, preset="custom", skills=["team-review"], target_root=target, home=home)
    selected = resolve_package_selection(root, preset="custom", skills=["repo:team-review"], target_root=target, home=home)
    assert selected.skills == ["team-review"]
    assert selected.bindings["team-review"]["selector"] == "repo:team-review"
    with pytest.raises(ValueError, match="conflict"):
        resolve_package_selection(root, preset="custom", skills=["repo:team-review", "personal:team-review"], target_root=target, home=home)


def test_snapshot_preserves_source_and_detects_tampering_and_stale_plans(tmp_path):
    from ls.core.apply_packages import install_external_snapshot
    root = Path(__file__).resolve().parents[2]
    source = skill(tmp_path / "source")
    script = source / "run.sh"
    script.write_text("#!/bin/sh\nprintf 'review\\n'\n")
    script.chmod(0o755)
    empty = source / "templates/output"
    empty.mkdir(parents=True)
    empty.chmod(0o750)
    row = inspect_external_skill(source, "repo")
    library = tmp_path / "packages"
    result = install_external_snapshot(root, library, row.receipt(), replace_func=os.replace)
    assert (result / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    assert (result / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (result / "templates/output").is_dir()
    assert (result / "templates/output").stat().st_mode & 0o777 == 0o750
    assert not (source / ".localsetup-managed.json").exists()
    assert install_external_snapshot(root, library, row.receipt(), replace_func=os.replace) == result
    (result / "run.sh").write_text("tampered")
    with pytest.raises(ValueError, match="snapshot differs"):
        install_external_snapshot(root, library, row.receipt(), replace_func=os.replace)
    script.chmod(0o644)
    with pytest.raises(ValueError, match="source changed"):
        install_external_snapshot(root, library, row.receipt(), replace_func=os.replace)


def test_custom_skill_cannot_hide_selected_framework_workflow(tmp_path):
    from ls.core.selection import resolve_package_selection
    from ls.core.plan import build_install_plan
    root = Path(__file__).resolve().parents[2]
    target, home = tmp_path / "repo", tmp_path / "home"
    name = "ls-wf-git-repair-hygiene"
    skill(target / ".agents/skills", name)
    with pytest.raises(ValueError, match="conflict"):
        resolve_package_selection(root, preset="custom", skills=[f"repo:{name}"],
                                  workflows=[name], target_root=target, home=home)
    with pytest.raises(ValueError, match="share visible name"):
        build_install_plan(root, home, global_preset="custom", global_workflows=[name],
                           repo_preset="custom", repo_skills=[f"repo:{name}"],
                           platform_ids=["codex"], target_root=target)


@pytest.mark.parametrize("mode", ["symlink", "portable"])
def test_external_skill_install_and_verify(tmp_path, mode):
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    from ls.core.verify import verify_install
    root = Path(__file__).resolve().parents[2]
    target, home = tmp_path / "repo", tmp_path / "home"
    source = skill(target / ".agents/skills")
    before = (source / "SKILL.md").read_bytes()
    plan = build_install_plan(root, home, preset="custom", skills=["repo:team-review"],
                              target_root=target, platform_ids=["codex"], attach_mode=mode)
    apply_plan(root, plan, home, target_root=target)
    report = verify_install(root, home, target_root=target)
    assert report["ok"], report["issues"]
    assert (source / "SKILL.md").read_bytes() == before
    assert not (source / ".localsetup-managed.json").exists()
    from ls.core.path_contract import resolve_package_path
    from ls.core.manifests import load_pack_config
    from ls.core.paths import expand_user_path
    resolved = resolve_package_path(home, "repo:team-review", "SKILL.md", source_root=root,
                                    target_root=target,
                                    package_root=expand_user_path(load_pack_config(root).global_root, home))
    assert resolved != source / "SKILL.md"
    assert resolved.read_bytes() == before


@pytest.mark.parametrize("scope", ["repo", "both"])
def test_recorded_update_and_rollback_preserve_custom_original(tmp_path, scope):
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    from ls.core.personal_update import _build_recorded_plan
    from ls.core.rollback import rollback
    from ls.core.verify import verify_install
    root = Path(__file__).resolve().parents[2]
    target, home = tmp_path / "repo", tmp_path / "home"
    source = skill(target / ".agents/skills")
    plan = build_install_plan(root, home, preset="custom", skills=["repo:team-review"],
                              target_root=target, platform_ids=["codex"], skill_scope=scope)
    apply_plan(root, plan, home, target_root=target)
    original = source / "SKILL.md"
    original.write_text(original.read_text() + "\nUpdated custom procedure.\n")
    updated = original.read_bytes()
    plan = _build_recorded_plan(root, home, target, scope)
    apply_plan(root, plan, home, target_root=target)
    assert verify_install(root, home, target_root=target)["ok"]
    rollback(root, home, target_root=target)
    assert original.read_bytes() == updated
    assert not (source / ".localsetup-managed.json").exists()


@pytest.mark.parametrize("mode", ["symlink", "portable"])
@pytest.mark.parametrize("scope", ["personal", "both"])
def test_personal_custom_source_install_and_update(tmp_path, mode, scope):
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    from ls.core.personal_update import _build_recorded_plan
    from ls.core.verify import verify_install
    root = Path(__file__).resolve().parents[2]
    target, home = tmp_path / "repo", tmp_path / "home"
    target.mkdir()
    source = skill(home / ".agents/skills")
    plan = build_install_plan(root, home, preset="custom", skills=["personal:team-review"],
                              target_root=target, platform_ids=["codex"], skill_scope=scope, attach_mode=mode)
    apply_plan(root, plan, home, target_root=target)
    assert verify_install(root, home, target_root=target)["ok"]
    original = source / "SKILL.md"
    original.write_text(original.read_text() + "\nNew personal procedure.\n")
    plan = _build_recorded_plan(root, home, target, scope)
    apply_plan(root, plan, home, target_root=target)
    report = verify_install(root, home, target_root=target)
    assert report["ok"], report["issues"]


def test_portable_custom_projection_edits_block_update(tmp_path):
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    from ls.core.personal_update import _build_recorded_plan
    from ls.core.verify import verify_install
    root = Path(__file__).resolve().parents[2]
    target, home = tmp_path / "repo", tmp_path / "home"
    source = skill(target / ".agents/skills")
    plan = build_install_plan(root, home, preset="custom", skills=["repo:team-review"],
                              target_root=target, platform_ids=["claude-code"], attach_mode="portable")
    apply_plan(root, plan, home, target_root=target)
    action = next(a for a in plan.actions if a.kind == "attach_repo_path" and "team-review" in a.details.get("packages", []))
    projected = action.path / "team-review/SKILL.md"
    assert projected != source / "SKILL.md"
    projected.write_text(projected.read_text() + "\nLocal editing must survive.\n")
    edited = projected.read_bytes()
    assert not verify_install(root, home, target_root=target)["ok"]
    with pytest.raises(ValueError, match="Repair"):
        _build_recorded_plan(root, home, target, "repo")
    assert projected.read_bytes() == edited


@pytest.mark.parametrize("scope", ["repo", "personal"])
def test_custom_scope_migration_retains_origin(tmp_path, scope):
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    from ls.core.scope_migration import build_additive_scope_plan
    from ls.core.verify import verify_install
    root = Path(__file__).resolve().parents[2]
    target, home = tmp_path / "repo", tmp_path / "home"
    target.mkdir()
    source = skill((target if scope == "repo" else home) / ".agents/skills")
    plan = build_install_plan(root, home, preset="custom", skills=[f"{scope}:team-review"],
                              target_root=target, platform_ids=["codex"], skill_scope=scope)
    apply_plan(root, plan, home, target_root=target)
    migrated = build_additive_scope_plan(root, home, target)
    apply_plan(root, migrated, home, target_root=target)
    report = verify_install(root, home, target_root=target)
    assert report["ok"], report["issues"]
    assert not (source / ".localsetup-managed.json").exists()


def test_contract_upgrade_rebuilds_snapshot_without_changing_original(tmp_path, monkeypatch):
    from ls.core import skills_external
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    from ls.core.personal_update import _build_recorded_plan
    from ls.core.verify import verify_install
    root = Path(__file__).resolve().parents[2]
    target, home = tmp_path / "repo", tmp_path / "home"
    source = skill(target / ".agents/skills")
    before = (source / "SKILL.md").read_bytes()
    plan = build_install_plan(root, home, preset="custom", skills=["repo:team-review"],
                              target_root=target, platform_ids=["codex"])
    apply_plan(root, plan, home, target_root=target)
    old_path = Path(plan.rollback_metadata["package_bindings"]["team-review"]["artifact_path"])
    monkeypatch.setattr(skills_external, "CONTRACT_VERSION", 2)
    assert not verify_install(root, home, target_root=target)["ok"]
    refreshed = _build_recorded_plan(root, home, target, "repo")
    apply_plan(root, refreshed, home, target_root=target)
    report = verify_install(root, home, target_root=target)
    assert report["ok"], report["issues"]
    assert old_path.exists()
    assert (source / "SKILL.md").read_bytes() == before
