import json

import pytest

from ls.core.adapter_markers import ADAPTER_MARKER_JSON
from ls.core.apply import apply_plan
from ls.core.apply_preflight import preflight_install_plan
from ls.core.manifests import load_pack_config
from ls.core.mutable_ownership import require_owned_copies
from ls.core.mutable_packages import MutablePackageError
from ls.core.paths import expand_user_path
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


def install(tmp_path, scope='both', skill='ls-localsetup-project-context'):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(root, home, skills=[skill], platform_ids=['github-copilot-cli'],
                              skill_scope=scope, attach_mode='portable')
    for action in plan.actions:
        if action.kind in {'attach_repo_path', 'attach_personal_path'}:action.details['mutable_copy'] = True
    apply_plan(root, plan, home)
    registry = expand_user_path(load_pack_config(root).global_registry, home)
    return root, home, registry


@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
def test_apply_persists_independent_mutable_owner_designation(tmp_path, scope):
    root, home, registry = install(tmp_path, scope)
    lock = json.loads((root / '.localsetup/lock.json').read_text())
    for row in [*lock['adapter_targets'], *lock['personal_adapter_targets']]:assert row['mutable_copy'] is True
    data = json.loads(registry.read_text())
    for owner in data.get('personal_owners', {}).values():assert owner['mutable_paths'] == owner['paths']
    paths = [row['path'] for row in data['targets'][str(root)]['adapters']]
    require_owned_copies(root, home, paths, target=root)


@pytest.mark.parametrize('damage', ['marker', 'baseline', 'package', 'edit', 'selection'])
def test_missing_or_changed_copy_is_blocked_before_apply_mutations(tmp_path, damage):
    root, home, registry = install(tmp_path)
    adapter = home / '.agents/skills';marker = adapter / ADAPTER_MARKER_JSON
    if damage == 'marker':marker.unlink()
    elif damage == 'baseline':
        data = json.loads(marker.read_text());data.pop('mutable_packages');marker.write_text(json.dumps(data))
    elif damage == 'package':(adapter / 'ls-localsetup-project-context/SKILL.md').unlink()
    elif damage == 'edit':(adapter / 'ls-localsetup-project-context/SKILL.md').write_text('learned')
    else:
        data = json.loads(marker.read_text());data['packages'] = [];data['mutable_packages'] = {};marker.write_text(json.dumps(data))
    before_registry = registry.read_bytes();receipt = root / '.localsetup/lock.json';before_receipt = receipt.read_bytes()
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['github-copilot-vscode'],
                              skill_scope='both', attach_mode='portable')
    result = preflight_install_plan(root, plan, home)
    assert not result['ok']
    assert any(b['status_code'] == 'mutable_copy_preservation' for b in result['blockers'])
    with pytest.raises(RuntimeError, match='mutable_copy_preservation'):apply_plan(root, plan, home)
    assert registry.read_bytes() == before_registry and receipt.read_bytes() == before_receipt


def test_target_receipt_covers_missing_registry_and_unrelated_drift_is_not_selected(tmp_path):
    root, home, registry = install(tmp_path)
    adapter = root / '.agents/skills'
    (adapter / ADAPTER_MARKER_JSON).unlink()
    registry.unlink()
    with pytest.raises(MutablePackageError):require_owned_copies(root, home, [adapter], target=root)
    require_owned_copies(root, home, [root / 'unrelated'], target=root)


def test_cross_repository_personal_reselection_uses_current_owner_packages(tmp_path):
    root, home, registry = install(tmp_path, 'personal', skill='ls-debug-pro')
    second = tmp_path / 'second';second.mkdir()
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['github-copilot-cli'],
                              target_root=second, skill_scope='personal', attach_mode='portable')
    apply_plan(root, plan, home, target_root=second)
    assert not (home / '.agents/skills/ls-debug-pro').exists()
    assert (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert preflight_install_plan(root, plan, home, target_root=second)['ok']
    apply_plan(root, plan, home, target_root=second)
    # The first target still has historical personal selection A, not current B.
    old = json.loads((root / '.localsetup/lock.json').read_text())
    assert 'ls-debug-pro' in old['personal_adapter_targets'][0]['packages']
    require_owned_copies(root, home, [home / '.agents/skills'], target=root)
