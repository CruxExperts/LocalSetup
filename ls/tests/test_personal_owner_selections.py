import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.installation_ownership import InstallationOwner
from ls.core.personal_registry import owner_key, personal_selections
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


def test_shared_personal_action_retains_distinct_selections(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    from ls.tests.test_historical_personal_ownership import use_historical_openclaw_personal_paths
    use_historical_openclaw_personal_paths(root)
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor', 'openclaw'], skill_scope='personal')
    keys = {c: owner_key(InstallationOwner('personal', str(home.resolve()), c)) for c in ['cursor', 'openclaw']}
    requested = set(plan.rollback_metadata['repo_packages'])
    for action in plan.actions:
        if action.kind != 'attach_personal_path':continue
        mapping = {owner_key(InstallationOwner(**o)): sorted(requested) if o['client'] == 'cursor' else []
                   for o in action.details['owners']}
        action.details['owner_packages'] = mapping
        action.details['packages'] = sorted(set().union(*(set(v) for v in mapping.values())))
    apply_plan(root, plan, home)
    lock = json.loads((root / '.localsetup/lock.json').read_text())
    registry = json.loads(Path(lock['registry_path']).read_text())
    assert set(registry['personal_owners'][keys['cursor']]['packages']) == requested
    assert registry['personal_owners'][keys['openclaw']]['packages'] == []
    assert all('owner_packages' in row for row in lock['personal_adapter_targets'])
    assert (home / '.agents/skills/ls-localsetup-project-context').exists()
    assert not (home / '.openclaw/skills/ls-localsetup-project-context').exists()
    for name in requested:
        assert keys['cursor'] in registry['packages'][name]['refs']
        assert keys['openclaw'] not in registry['packages'][name]['refs']
    # A mismatched explicit union must fail preflight, before any installed state changes.
    receipt = (root / '.localsetup/lock.json').read_bytes()
    action = next(a for a in plan.actions if a.kind == 'attach_personal_path')
    original = action.details['owner_packages']
    action.details['owner_packages'] = {}
    with pytest.raises(RuntimeError, match="install preflight failed"):apply_plan(root, plan, home)
    assert (root / '.localsetup/lock.json').read_bytes() == receipt
    action.details['owner_packages'] = original
    shared = next(a for a in plan.actions if a.kind == 'attach_personal_path' and len(a.details['owners']) == 2)
    shared.details['owner_packages'][keys['openclaw']] = sorted(requested)
    with pytest.raises(RuntimeError, match='differs across adapter paths'):
        apply_plan(root, plan, home)
    assert (root / '.localsetup/lock.json').read_bytes() == receipt


def test_personal_selection_map_rejects_invalid_shapes(tmp_path):
    owner = InstallationOwner('personal', str(tmp_path), 'fixture')
    base = {'owners': [owner.wire()], 'packages': ['ls-one']}
    assert personal_selections(base) == {owner_key(owner): {'ls-one'}}
    for value in [None, [], {}, {owner_key(owner): ['../outside']}, {owner_key(owner): []}]:
        with pytest.raises(ValueError):personal_selections(base | {'owner_packages': value})
