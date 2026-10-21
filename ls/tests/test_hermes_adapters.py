import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.repair import run_repair
from ls.core.detach import detach_platforms
from ls.core.personal_detach import detach_personal
from ls.core.mutable_ownership import require_owned_copies
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
def test_hermes_profile_copy_update_repair_detach_preserves_native_state(tmp_path, monkeypatch, scope):
    monkeypatch.delenv('HERMES_HOME', raising=False)
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    native = home / '.hermes';native.mkdir(parents=True)
    config = native / 'config.yaml';config.write_text('fixture: preserve\n')
    session = native / 'sessions.db';session.write_bytes(b'native session')
    bases = [root] if scope == 'repo' else [home] if scope == 'personal' else [root, home]
    for base in bases:
        adapter = base / '.hermes/skills';adapter.mkdir(parents=True, exist_ok=True)
        (adapter / 'custom.txt').write_text('keep')
    source = root / 'ls/skills/ls-localsetup-project-context/references/hermes-fixture.txt'
    source.parent.mkdir(exist_ok=True);source.write_text('v1')
    plan = build_install_plan(root, home, platform_ids=['hermes-agent'], skills=['ls-localsetup-project-context'],
                              skill_scope=scope, attach_mode='portable')
    assert all(a.details['mutable_copy'] for a in plan.actions if a.kind.startswith('attach_'))
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    assert run_repair(root, home=home, platform_ids=['hermes-agent'])['ok']
    for base in bases:
        copy = base / '.hermes/skills/ls-localsetup-project-context/references/hermes-fixture.txt'
        assert copy.read_text() == 'v1' and copy.stat().st_nlink == 1
        assert not copy.is_symlink()
    source.write_text('v2');apply_plan(root, plan, home)
    for base in bases:
        copy = base / '.hermes/skills/ls-localsetup-project-context/references/hermes-fixture.txt'
        assert copy.read_text() == 'v2'
        copy.write_text('learned')
        result = run_repair(root, home=home, platform_ids=['hermes-agent'], apply=True)
        assert not result['ok'] and copy.read_text() == 'learned'
        copy.write_text('v2')
    require_owned_copies(root, home, [b / '.hermes/skills' for b in bases], target=root)
    if scope in {'repo', 'both'}:detach_platforms(root, home, root, ['hermes-agent'])
    if scope in {'personal', 'both'}:assert detach_personal(root, home, ['hermes-agent'], apply=True)['applied']
    for base in bases:
        assert not (base / '.hermes/skills/ls-localsetup-project-context').exists()
        assert (base / '.hermes/skills/custom.txt').read_text() == 'keep'
    assert config.read_text() == 'fixture: preserve\n' and session.read_bytes() == b'native session'


def test_hermes_rejects_default_symlink_mode_without_creating_configuration(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    with pytest.raises(ValueError, match='portable'):
        build_install_plan(root, home, platform_ids=['hermes-agent'])
    assert not home.exists()


@pytest.mark.parametrize('scope', ['personal', 'both'])
def test_hermes_recorded_update_and_repair_profile_binding(tmp_path, monkeypatch, scope):
    from ls.core.personal_update import build_recorded_personal_plan, build_recorded_both_plan
    monkeypatch.delenv('HERMES_HOME', raising=False)
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(root, home, platform_ids=['hermes-agent'], skills=['ls-localsetup-project-context'],
                              skill_scope=scope, attach_mode='portable')
    apply_plan(root, plan, home)
    build = build_recorded_personal_plan if scope == 'personal' else build_recorded_both_plan
    apply_plan(root, build(root, home, root), home)
    lock = json.loads((root / '.localsetup/lock.json').read_text())
    library = Path(lock['package_root'])
    canonical = library / 'ls-localsetup-project-context/SKILL.md'
    canonical.write_text(canonical.read_text() + '\nCanonical update fixture.\n')
    copy = home / '.hermes/skills/ls-localsetup-project-context/SKILL.md';before = copy.read_bytes()
    receipt = root / '.localsetup/lock.json';receipt_before = receipt.read_bytes()
    monkeypatch.setenv('HERMES_HOME', str(home / 'other'))
    result = run_repair(root, home=home, platform_ids=['hermes-agent'], apply=True)
    assert not result['ok'] and any('HERMES_HOME' in b for b in result['blockers'])
    assert copy.read_bytes() == before and receipt.read_bytes() == receipt_before


def test_hermes_repository_rollback_preserves_native_neighbor(tmp_path, monkeypatch):
    from ls.core.rollback import rollback
    monkeypatch.delenv('HERMES_HOME', raising=False)
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(root, home, platform_ids=['hermes-agent'], skills=['ls-localsetup-project-context'], attach_mode='portable')
    apply_plan(root, plan, home)
    neighbor = root / '.hermes/skills/custom.txt';neighbor.write_text('keep')
    rollback(root, home, target_root=root)
    assert neighbor.read_text() == 'keep'
    assert not (neighbor.parent / 'ls-localsetup-project-context').exists()
