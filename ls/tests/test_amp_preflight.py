import json
from pathlib import Path

import pytest

from ls.core.amp_preflight import amp_skill_blockers
from ls.core.apply import apply_plan
from ls.core.apply_preflight import preflight_install_plan
from ls.core.models import PlanAction
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


def skill(path, name='ls-localsetup-project-context'):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: fixture\n---\nbody\n')


@pytest.fixture(autouse=True)
def default_xdg(monkeypatch):monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)


def fixture_plan(tmp_path):
    source = tmp_path / 'source';home = tmp_path / 'home';target = tmp_path / 'project'
    skill(source / 'ls/skills/ls-localsetup-project-context')
    config = source / 'ls/config';config.mkdir(parents=True)
    config.joinpath('pack.yaml').write_text((Path(__file__).resolve().parents[2] / 'ls/config/pack.yaml').read_text())
    action = PlanAction('attach_repo_path', target / '.agents/skills', {
        'platforms': ['amp-cli'], 'packages': ['ls-localsetup-project-context'], 'global_root': str(home / 'library')})
    return source, home, target, [action]


@pytest.mark.parametrize('scope,relative', [
    ('home', '.config/agents/skills'), ('home', '.agents/skills'),
    ('home', '.config/amp/skills'), ('home', '.claude/skills'),
    ('target', '.agents/skills'), ('target', '.claude/skills'),
    ('parent', '.agents/skills'),
])
def test_amp_compares_frontmatter_identity_across_local_roots(tmp_path, scope, relative):
    source, home, target, actions = fixture_plan(tmp_path)
    base = {'home': home, 'target': target, 'parent': target.parent}[scope]
    skill(base / relative / 'different-directory')
    result = amp_skill_blockers(source, actions, home, target)
    assert result and result[0]['status_code'] == 'amp_skill_precedence_conflict'
    assert (base / relative / 'different-directory/SKILL.md').is_file()


def test_amp_blocks_unknown_names_and_nondefault_xdg(tmp_path, monkeypatch):
    source, home, target, actions = fixture_plan(tmp_path)
    skill(home / '.config/agents/skills/bad')
    (home / '.config/agents/skills/bad/SKILL.md').write_text('---\nname: [invalid]\n---\n')
    assert amp_skill_blockers(source, actions, home, target)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'alternate'))
    assert 'XDG_CONFIG_HOME' in amp_skill_blockers(source, actions, home, target)[0]['reason']
    assert amp_skill_blockers(source, [], home, target) == []


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_amp_installs_shared_owners_and_rechecks_conflict_before_write(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['amp-cli', 'github-copilot-cli'], skill_scope='both', attach_mode=mode)
    skill(home / '.config/agents/skills/other-directory')
    assert not preflight_install_plan(root, plan, home)['ok']
    with pytest.raises(RuntimeError, match='Amp'):apply_plan(root, plan, home)
    assert not (root / '.localsetup/lock.json').exists()
    # Remove only this test-created conflicting fixture, preserving real-user policy.
    (home / '.config/agents/skills/other-directory/SKILL.md').unlink()
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    lock = json.loads((root / '.localsetup/lock.json').read_text())
    assert {o['client'] for o in lock['adapter_targets'][0]['owners']} == {'amp-cli', 'github-copilot-cli'}
    assert preflight_install_plan(root, plan, home)['ok']


@pytest.mark.parametrize('scope', ['personal', 'both'])
def test_amp_repair_cannot_bypass_global_collision_guard(tmp_path, scope):
    from ls.core.combined_repair import repair_combined
    from ls.core.personal_repair import repair_personal
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['amp-cli'], skill_scope=scope), home)
    receipt = root / '.localsetup/lock.json';before = receipt.read_bytes()
    (home / '.agents/skills/ls-localsetup-project-context').unlink()
    skill(home / '.config/agents/skills/conflict')
    result = repair_combined(root, home, root, apply=True) if scope == 'both' else repair_personal(root, home, ['amp-cli'], apply=True)
    assert not result['ok'] and not result['applied']
    assert any('Amp' in b for b in result['blockers'])
    assert receipt.read_bytes() == before
    assert not (home / '.agents/skills/ls-localsetup-project-context').exists()


@pytest.mark.parametrize('damaged', ['repo', 'personal'])
def test_amp_portable_repair_accepts_only_unchanged_recorded_counterpart(tmp_path, damaged):
    from ls.core.combined_repair import repair_combined
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['amp-cli'], skill_scope='both', attach_mode='portable'), home)
    base = root if damaged == 'repo' else home
    (base / '.agents/skills/ls-localsetup-project-context/SKILL.md').unlink()
    result = repair_combined(root, home, root, apply=True)
    assert result['ok'] and result['applied']
    assert verify_install(root, home, target_root=root)['ok']
    (base / '.agents/skills/ls-localsetup-project-context/SKILL.md').unlink()
    skill(home / '.config/agents/skills/custom')
    result = repair_combined(root, home, root, apply=True)
    assert not result['ok'] and not result['applied']


def test_other_client_update_rechecks_retained_amp_owner(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['amp-cli'], skill_scope='both'), home)
    skill(home / '.config/agents/skills/conflict')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['github-copilot-cli'], skill_scope='both')
    assert not preflight_install_plan(root, plan, home)['ok']
    with pytest.raises(RuntimeError, match='Amp'):apply_plan(root, plan, home)


@pytest.mark.parametrize('invalid', [
    {'personal_owners': []}, {'personal_owners': {'bad': {'owner': 'sensitive fixture'}}},
    {'targets': []}, {'targets': {'/fixture': {'adapters': ['sensitive fixture']}}},
])
def test_amp_ownership_errors_are_structured_and_sanitized(tmp_path, invalid):
    from ls.core.manifests import load_pack_config
    from ls.core.paths import expand_user_path
    source, home, target, actions = fixture_plan(tmp_path)
    registry = expand_user_path(load_pack_config(source).global_registry, home)
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'version': 2, **invalid}))
    actions[0].details['platforms'] = ['github-copilot-cli']
    result = amp_skill_blockers(source, actions, home, target)
    assert result and 'ownership could not be established' in result[0]['reason']
    assert 'sensitive fixture' not in json.dumps(result)


@pytest.mark.parametrize('marker', [[],
    {'managed_by': 'localsetup', 'mode': 'portable', 'packages': None},
    {'managed_by': 'localsetup', 'mode': 'portable', 'packages': ['ls-localsetup-project-context'], 'global_root': []},
])
def test_amp_malformed_portable_marker_is_a_blocker(tmp_path, marker):
    from ls.core.manifests import load_pack_config
    from ls.core.paths import expand_user_path
    source, home, target, actions = fixture_plan(tmp_path)
    root = home / '.config/agents/skills'
    skill(root / 'ls-localsetup-project-context')
    (root / '.localsetup-adapter.json').write_text(json.dumps(marker))
    registry = expand_user_path(load_pack_config(source).global_registry, home)
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'version': 2, 'personal_owners': {'fixture': {
        'owner': {'scope': 'personal', 'root': str(home), 'client': 'amp-cli'},
        'paths': [str(root)], 'packages': ['ls-localsetup-project-context'], 'mode': 'portable'}}}))
    result = amp_skill_blockers(source, actions, home, target)
    assert result and 'could not be established' in result[0]['reason']
