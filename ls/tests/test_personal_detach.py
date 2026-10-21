import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.personal_detach import detach_personal
from ls.core.personal_inventory import personal_inventory
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_personal_detach_reconciles_receipts_and_recovers_failure(tmp_path, mode, monkeypatch):
    import ls.core.personal_detach as engine
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    # Preserve historical shared plus exclusive physical-target coverage.
    from ls.tests.test_preferred_path_retention import prefer_common
    prefer_common(root, historical=True)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['cursor', 'openclaw'], skill_scope='personal', attach_mode=mode), home)
    receipt = root / '.localsetup/lock.json'
    registry = Path(json.loads(receipt.read_text())['registry_path'])
    before = receipt.read_bytes(), registry.read_bytes()
    shared = home / '.agents/skills'
    (shared / 'custom.txt').write_text('keep')
    report = detach_personal(root, home, ['cursor'])
    assert report['ok'] and not report['applied'] and report['receipts'] == [str(receipt)]
    assert before == (receipt.read_bytes(), registry.read_bytes())
    save = engine.save_json
    def fail(path, payload):
        save(path, payload)
        if path == registry:
            (shared / 'new.txt').write_text('new neighbor')
            raise RuntimeError('injected after registry write')
    monkeypatch.setattr(engine, 'save_json', fail)
    result = detach_personal(root, home, ['cursor'], apply=True)
    assert not result['ok'] and result['recovery_ok'], result
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert (home / '.cursor/skills/ls-localsetup-project-context/SKILL.md').exists()
    assert (shared / 'new.txt').read_text() == 'new neighbor'
    monkeypatch.setattr(engine, 'save_json', save)
    result = detach_personal(root, home, ['cursor'], apply=True)
    assert result['ok'] and result['applied'], result
    lock = json.loads(receipt.read_text())
    assert lock['platforms'] == ['openclaw']
    assert not (home / '.cursor/skills/ls-localsetup-project-context').exists()
    assert (shared / 'ls-localsetup-project-context/SKILL.md').exists()
    assert (shared / 'custom.txt').read_text() == 'keep'
    remaining = personal_inventory(root, home, expected=lock['personal_adapter_targets'])
    assert remaining['ok'], remaining
    assert {row['owner']['client'] for row in remaining['owners']} == {'openclaw'}
    assert detach_personal(root, home, ['openclaw'], apply=True)['ok']
    assert not personal_inventory(root, home)['owners']
    assert not (shared / 'ls-localsetup-project-context').exists()
    assert all(Path(p).exists() for p in lock['installed_skills'])


def test_personal_detach_empty_unknown_and_missing_receipt(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    assert detach_personal(root, home, [], apply=True)['ok']
    assert not home.exists()
    assert not detach_personal(root, home, ['cursor'], apply=True)['ok']
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['cursor'], skill_scope='personal'), home)
    receipt = root / '.localsetup/lock.json'
    receipt.unlink()
    result = detach_personal(root, home, ['cursor'], apply=True)
    assert not result['ok'] and 'receipt' in result['blockers'][0]
    assert (home / '.agents/skills/ls-localsetup-project-context').exists()


def test_personal_detach_preserves_legacy_repository_membership(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['cursor'], skill_scope='both'), home)
    receipt = root / '.localsetup/lock.json'
    lock = json.loads(receipt.read_text())
    registry_path = Path(lock['registry_path'])
    registry = json.loads(registry_path.read_text())
    for row in lock['adapter_targets']:row.pop('owners')
    for row in registry['targets'][str(root.resolve())]['adapters']:
        if any(o['scope'] == 'repo' for o in row.get('owners', [])):row.pop('owners')
    receipt.write_text(json.dumps(lock));registry_path.write_text(json.dumps(registry))
    result = detach_personal(root, home, ['cursor'], apply=True)
    assert result['ok'] and result['applied'], result
    updated = json.loads(receipt.read_text())
    assert updated['skill_scope'] == 'repo' and updated['platforms'] == ['cursor']
    assert updated['adapter_targets'] == lock['adapter_targets']
    assert (root / '.agents/skills/ls-localsetup-project-context/SKILL.md').exists()
