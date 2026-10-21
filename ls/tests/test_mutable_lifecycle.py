import json
from pathlib import Path

import pytest

from ls.core.adapter_markers import ADAPTER_MARKER_JSON
from ls.core.combined_repair import repair_combined
from ls.core.detach import detach_platforms
from ls.core.mutable_packages import MutablePackageError
from ls.core.personal_detach import detach_personal
from ls.core.personal_repair import repair_personal
from ls.core.repair import run_repair
from ls.core.rollback import rollback
from ls.tests.test_mutable_ownership import install

CLIENT = 'github-copilot-cli'


def call(operation, root, home):
    if operation == 'personal-repair':return repair_personal(root, home, [CLIENT], apply=True)
    if operation == 'combined-repair':return repair_combined(root, home, root, [CLIENT], apply=True)
    if operation == 'repo-repair':return run_repair(root, home=home, target_root=root, platform_ids=[CLIENT], apply=True)
    if operation == 'personal-detach':return detach_personal(root, home, [CLIENT], apply=True)
    if operation == 'repo-detach':return detach_platforms(root, home, root, [CLIENT])
    return rollback(root, home, target_root=root)


@pytest.mark.parametrize('operation', ['personal-repair', 'combined-repair', 'repo-repair', 'personal-detach', 'repo-detach', 'rollback'])
@pytest.mark.parametrize('damage', ['marker', 'edit', 'delete'])
def test_lifecycle_refuses_drift_before_mutation(tmp_path, operation, damage):
    scope = 'personal' if operation.startswith('personal') else 'both' if operation == 'combined-repair' else 'repo'
    root, home, registry = install(tmp_path, scope)
    base = home if scope in {'personal', 'both'} else root
    adapter = base / '.agents/skills'
    marker = adapter / ADAPTER_MARKER_JSON
    if damage == 'marker':marker.unlink()
    elif damage == 'edit':(adapter / 'ls-localsetup-project-context/SKILL.md').write_text('learned')
    else:(adapter / 'ls-localsetup-project-context/SKILL.md').unlink()
    receipt = root / '.localsetup/lock.json';before = {p: p.read_bytes() for p in [receipt, registry]}
    library = Path(json.loads(receipt.read_text())['package_root'])
    library_bytes = (library / 'ls-localsetup-project-context/SKILL.md').read_bytes()
    if operation in {'repo-detach', 'rollback'}:
        with pytest.raises(MutablePackageError):call(operation, root, home)
    else:
        result = call(operation, root, home)
        assert not result['ok'] and not result['applied']
        assert any('mutable' in b.lower() for b in result['blockers'])
    for path, content in before.items():assert path.read_bytes() == content
    assert (library / 'ls-localsetup-project-context/SKILL.md').read_bytes() == library_bytes
    if damage == 'marker':assert not marker.exists()
    elif damage == 'edit':assert (adapter / 'ls-localsetup-project-context/SKILL.md').read_text() == 'learned'
    else:assert not (adapter / 'ls-localsetup-project-context/SKILL.md').exists()


@pytest.mark.parametrize('operation', ['personal-detach', 'repo-detach', 'rollback'])
def test_clean_lifecycle_preserves_custom_neighbors(tmp_path, operation):
    scope = 'personal' if operation == 'personal-detach' else 'repo'
    root, home, registry = install(tmp_path, scope)
    adapter = (home if scope == 'personal' else root) / '.agents/skills'
    (adapter / 'custom.txt').write_text('keep')
    call(operation, root, home)
    assert (adapter / 'custom.txt').read_text() == 'keep'
    assert not (adapter / 'ls-localsetup-project-context').exists()


def test_repository_repair_rechecks_under_lock_and_keeps_lock_through_apply(tmp_path, monkeypatch):
    from ls.core import apply as apply_module, repair as repair_module
    from ls.core.locking import package_root_lock, PackageRootLockTimeout
    from ls.core.paths import global_layout
    root, home, registry = install(tmp_path, 'repo')
    (root / '.localsetup/lock.json').unlink()
    original = apply_module._apply_plan_unlocked;observed = []
    def check_lock(*args, **kwargs):
        with pytest.raises(PackageRootLockTimeout):
            with package_root_lock(global_layout(home).localsetup_home, timeout=0):pass
        observed.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(apply_module, '_apply_plan_unlocked', check_lock)
    result = repair_module.run_repair(root, home=home, target_root=root, platform_ids=[CLIENT], apply=True)
    assert result['ok'] and result['applied'] and observed


def test_repository_repair_observes_owner_change_during_lock_acquisition(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from ls.core import locking
    root, home, registry = install(tmp_path, 'repo')
    original = locking.package_root_lock
    marker = root / '.agents/skills' / ADAPTER_MARKER_JSON
    @contextmanager
    def changed_before_lock(*args, **kwargs):
        marker.unlink()
        with original(*args, **kwargs) as lock:yield lock
    monkeypatch.setattr(locking, 'package_root_lock', changed_before_lock)
    before = registry.read_bytes()
    result = run_repair(root, home=home, platform_ids=[CLIENT], apply=True)
    assert not result['ok'] and not result['applied']
    assert registry.read_bytes() == before and not marker.exists()


@pytest.mark.parametrize('direction', ['repo', 'personal'])
@pytest.mark.parametrize('damage', ['marker', 'clean'])
def test_scope_retirement_preserves_mutable_ownership(tmp_path, direction, damage):
    from ls.core.scope_retirement import retire_repository_scope
    from ls.core.personal_scope_retirement import retire_personal_scope
    from ls.core.mutable_ownership import require_owned_copies
    root, home, registry = install(tmp_path, 'both')
    selected = (root if direction == 'repo' else home) / '.agents/skills'
    retained = (home if direction == 'repo' else root) / '.agents/skills'
    if damage == 'marker':(selected / ADAPTER_MARKER_JSON).unlink()
    receipt = root / '.localsetup/lock.json';before = receipt.read_bytes(), registry.read_bytes()
    retire = retire_repository_scope if direction == 'repo' else retire_personal_scope
    if damage == 'marker':
        try:
            result = retire(root, home, root, apply=True)
        except ValueError:
            pass
        else:assert not result['ok'] and not result['applied']
        assert (receipt.read_bytes(), registry.read_bytes()) == before
    else:
        result = retire(root, home, root, apply=True)
        assert result['applied']
        assert not (selected / 'ls-localsetup-project-context').exists()
        require_owned_copies(root, home, [retained], target=root)
    assert (retained / 'ls-localsetup-project-context/SKILL.md').is_file()


def test_last_personal_mutable_owner_detach_allows_fresh_symlinks(tmp_path):
    from ls.core.apply import apply_plan
    from ls.core.plan import build_install_plan
    root, home, registry = install(tmp_path, 'personal')
    adapter = home / '.agents/skills'
    (adapter / 'custom.txt').write_text('keep')
    result = detach_personal(root, home, [CLIENT], apply=True)
    assert result['ok'] and result['applied']
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['github-copilot-vscode'],
                              skill_scope='personal', attach_mode='symlink')
    apply_plan(root, plan, home)
    assert (adapter / 'ls-localsetup-project-context').is_symlink()
    assert (adapter / 'custom.txt').read_text() == 'keep'


def test_empty_baseline_remains_for_recorded_owner(tmp_path):
    from ls.core.mutable_ownership import retire_empty_baselines
    adapter = tmp_path / 'skills';adapter.mkdir()
    marker = adapter / ADAPTER_MARKER_JSON
    marker.write_text(json.dumps({'mode': 'portable', 'packages': [], 'mutable_packages': {}}))
    registry = {'personal_owners': {'owner': {'paths': [str(adapter)],
                 'mutable_paths': [str(adapter)], 'packages': [], 'mode': 'portable'}}}
    before = marker.read_bytes()
    retire_empty_baselines([adapter], registry)
    assert marker.read_bytes() == before
