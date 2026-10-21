import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.scope_migration import build_additive_scope_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('scope', ['repo', 'personal'])
@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_add_scope_retains_recorded_requests_and_guards_stale_state(tmp_path, scope, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    initial = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'],
                                 skill_scope=scope, attach_mode=mode)
    apply_plan(root, initial, home)
    receipt = root / '.localsetup/lock.json'
    old = json.loads(receipt.read_text())
    old['adapter_transitions'] = [{'id': 'historical-fixture', 'disposition': 'removed'}]
    receipt.write_text(json.dumps(old))
    registry = Path(old['registry_path'])
    old_paths = old['adapter_targets'] if scope == 'repo' else old['personal_adapter_targets']
    custom = Path(old_paths[0]['path']) / 'custom.txt';custom.write_text('keep')
    before = receipt.read_bytes(), registry.read_bytes()
    migration = build_additive_scope_plan(root, home, root)
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert migration.rollback_metadata['scope_migration'] == {'from': scope, 'to': 'both'}
    assert not any(a.kind == 'retire_historical_adapter' for a in migration.actions)
    apply_plan(root, migration, home)
    new = json.loads(receipt.read_text())
    assert new['skill_scope'] == 'both'
    key = 'adapter_targets' if scope == 'repo' else 'personal_adapter_targets'
    assert len(new[key]) == len(old[key])
    for current, previous in zip(new[key], old[key]):
        assert all(current[k] == value for k, value in previous.items())
    assert new['adapter_transitions'] == old['adapter_transitions']
    assert new['platforms'] == ['cursor']
    assert set(new['skills']) == set(old['skills'])
    assert set(new['workflows']) == set(old['workflows'])
    assert custom.read_text() == 'keep'
    assert verify_install(root, home, target_root=root)['ok']
    # An independently planned migration cannot overwrite the now changed receipt.
    with pytest.raises(RuntimeError, match='stale_recorded_plan'):
        apply_plan(root, migration, home)


def test_add_scope_refuses_existing_personal_owner_without_writes(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    other = tmp_path / 'other';other.mkdir()
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'],
        skill_scope='personal', target_root=other), home)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'],
        skill_scope='repo'), home)
    receipt = root / '.localsetup/lock.json'
    registry = Path(json.loads(receipt.read_text())['registry_path'])
    before = receipt.read_bytes(), registry.read_bytes()
    with pytest.raises(ValueError, match='Personal owner already exists'):
        build_additive_scope_plan(root, home, root)
    assert before == (receipt.read_bytes(), registry.read_bytes())
