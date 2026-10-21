import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.core.personal_repair import repair_personal
from ls.tests.test_install_flow import make_temp_repo


pytestmark = pytest.mark.usefixtures('default_opencode_environment')


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_distinct_collision_blocks_shared_refresh_and_repair(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['opencode', 'codex'], skill_scope='both', attach_mode=mode), home)
    assert verify_install(root, home)['ok']
    duplicate = home / '.opencode/skills/duplicate/SKILL.md';duplicate.parent.mkdir(parents=True)
    duplicate.write_text('---\nname: ls-localsetup-project-context\ndescription: duplicate\n---\nkeep\n')
    before = (root / '.localsetup/lock.json').read_bytes()
    assert not verify_install(root, home)['ok']
    (root / 'ls/skills/ls-localsetup-project-context/reference.txt').write_text('new material')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['codex'])
    with pytest.raises(RuntimeError, match='opencode_skill_inventory'):
        apply_plan(root, plan, home)
    damaged = home / '.agents/skills/ls-localsetup-project-context'
    if damaged.is_symlink():damaged.unlink()
    else:
        import shutil
        shutil.rmtree(damaged)
    report = repair_personal(root, home, ['opencode'], apply=True)
    assert not report['ok']
    assert (root / '.localsetup/lock.json').read_bytes() == before
    assert duplicate.read_text().endswith('keep\n')


def test_overrides_are_bounded_and_do_not_affect_unselected_clients(tmp_path, monkeypatch):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.setenv('OPENCODE_DISABLE_EXTERNAL_SKILLS', 'true')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['opencode'])
    with pytest.raises(RuntimeError, match='opencode_skill_inventory'):apply_plan(root, plan, home)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['codex']), home)
    monkeypatch.setenv('OPENCODE_DISABLE_EXTERNAL_SKILLS', 'false')
    apply_plan(root, plan, home)
    assert verify_install(root, home)['ok']


def test_non_git_root_inventory_stops_at_selected_home(tmp_path):
    from ls.core.opencode_preflight import discovery_roots
    home = tmp_path / 'home';target = home / 'projects/target';target.mkdir(parents=True)
    roots = discovery_roots(home, target)
    assert target / '.agents/skills' in roots
    assert home / 'projects/.agents/skills' in roots
    assert home / '.agents/skills' in roots
    assert tmp_path / '.agents/skills' not in roots
    outside = tmp_path / 'outside';outside.mkdir()
    assert outside / '.agents/skills' in discovery_roots(home, outside)
    assert tmp_path / '.agents/skills' not in discovery_roots(home, outside)


@pytest.mark.parametrize('variable', ['OPENCODE_TEST_HOME', 'XDG_CONFIG_HOME'])
def test_unqualified_home_override_preserves_target(tmp_path, monkeypatch, variable):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['opencode'])
    monkeypatch.setenv(variable, str(tmp_path / 'alternate'))
    with pytest.raises(RuntimeError, match='home/configuration override'):
        apply_plan(root, plan, home)
    assert not (root / '.localsetup/lock.json').exists()
    assert not (root / '.agents/skills').exists()
