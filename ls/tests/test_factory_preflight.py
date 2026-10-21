from pathlib import Path
import pytest
from ls.core.factory_preflight import factory_skill_blockers, packages
from ls.tests.test_amp_preflight import fixture_plan, skill


def fixture(tmp_path):
    source, home, target, actions = fixture_plan(tmp_path)
    actions[0].details['platforms'] = ['factory-droid']
    return source, home, target, actions


@pytest.mark.parametrize('name', ['ls-localsetup-project-context', 'LS Context', 'ls_context'])
def test_factory_preserves_nested_native_conflict_or_unknown_identity(tmp_path, name):
    source, home, target, actions = fixture(tmp_path)
    native = target / '.factory/skills/category/other-directory';skill(native, name)
    before = (native / 'SKILL.md').read_bytes()
    assert factory_skill_blockers(source, actions, home, target)
    assert (native / 'SKILL.md').read_bytes() == before


def test_recursion_stops_at_package_boundary(tmp_path):
    source, home, target, actions = fixture(tmp_path)
    native = target / '.factory/skills/outer';skill(native, 'other')
    skill(native / 'nested', 'ls-localsetup-project-context')
    assert not factory_skill_blockers(source, actions, home, target)


def test_same_target_alias_is_not_exempt_from_duplicate_check(tmp_path):
    source, home, target, actions = fixture(tmp_path)
    origin = source / 'ls/skills/ls-localsetup-project-context'
    alias = target / '.factory/skills/alias';alias.parent.mkdir(parents=True);alias.symlink_to(origin)
    assert factory_skill_blockers(source, actions, home, target)


def test_recursive_cycle_and_bounded_inventory_are_rejected(tmp_path):
    root = tmp_path / 'skills';root.mkdir();(root / 'loop').symlink_to(root)
    with pytest.raises(ValueError, match='cycle'):list(packages(root, [0]))
    (root / 'loop').unlink();(root / 'file').write_text('fixture')
    with pytest.raises(ValueError, match='4096'):list(packages(root, [4096]))


def test_package_at_adapter_root_cannot_hide_planned_children(tmp_path):
    source, home, target, actions = fixture(tmp_path)
    skill(target / '.agents/skills', 'outer')
    assert factory_skill_blockers(source, actions, home, target)


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_factory_profile_shared_owners_and_collision_repair(tmp_path, mode):
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    from ls.core.verify import verify_install
    from ls.core.personal_repair import repair_personal
    from ls.core.personal_detach import detach_personal
    from ls.tests.test_install_flow import make_temp_repo
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    clients = ['factory-droid', 'github-copilot-cli']
    config = home / '.factory/settings.local.json';config.parent.mkdir(parents=True)
    config.write_text('{"disabledSkills":["fixture"]}')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=clients, skill_scope='both', attach_mode=mode)
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    assert not factory_skill_blockers(root, plan.actions, home, root)
    conflict = home / '.factory/skills/category/conflict';skill(conflict)
    other = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['github-copilot-cli'], skill_scope='both', attach_mode=mode)
    with pytest.raises(RuntimeError, match='Factory'):apply_plan(root, other, home)
    copy = home / '.agents/skills/ls-localsetup-project-context'
    if copy.is_symlink():copy.unlink()
    else:
        import shutil
        shutil.rmtree(copy)
    result = repair_personal(root, home, ['factory-droid'], apply=True)
    assert not result['ok'] and not result['applied']
    assert (conflict / 'SKILL.md').is_file()
    # Remove only the synthetic collision; selected detach retains Copilot ownership.
    (conflict / 'SKILL.md').unlink()
    assert repair_personal(root, home, clients, apply=True)['ok']
    assert detach_personal(root, home, ['factory-droid'], apply=True)['applied']
    assert copy.is_dir()
    assert config.read_text() == '{"disabledSkills":["fixture"]}'
