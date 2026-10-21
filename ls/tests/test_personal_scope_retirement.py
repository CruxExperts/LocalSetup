import json
from pathlib import Path

import pytest

from ls.core import cli, personal_detach
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.personal_scope_retirement import retire_personal_scope
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
@pytest.mark.parametrize('other_reference', [False, True])
def test_personal_retirement_preserves_other_references_and_recovers(tmp_path, monkeypatch, mode, other_reference):
    source = make_temp_repo(tmp_path);home = tmp_path / 'home';home.mkdir()
    # Both scopes share physical paths, exercising retained repo package union.
    target = home
    apply_plan(source, build_install_plan(source, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context'],
        skill_scope='both', attach_mode=mode, target_root=target), home)
    other = tmp_path / 'other';other.mkdir()
    if other_reference:
        apply_plan(source, build_install_plan(source, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context'],
            skill_scope='personal', attach_mode=mode, target_root=other), home)
    receipt = target / '.localsetup/lock.json'
    old = json.loads(receipt.read_text());registry = Path(old['registry_path'])
    other_before = (other / '.localsetup/lock.json').read_bytes() if other_reference else None
    before = receipt.read_bytes(), registry.read_bytes()
    custom = home / '.agents/skills/custom.txt';custom.write_text('keep')
    preview = retire_personal_scope(source, home, target)
    assert bool(preview['retained_owners']) == other_reference
    assert before == (receipt.read_bytes(), registry.read_bytes())
    original = personal_detach.save_json
    def fail(path, value):
        if path == receipt:
            (custom.parent / 'new-custom.txt').write_text('during failure')
            raise OSError('retirement receipt failure')
        return original(path, value)
    monkeypatch.setattr(personal_detach, 'save_json', fail)
    failed = retire_personal_scope(source, home, target, apply=True, expected=preview)
    assert not failed['ok'] and failed['recovery_ok']
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert (custom.parent / 'new-custom.txt').read_text() == 'during failure'
    monkeypatch.setattr(personal_detach, 'save_json', original)
    assert retire_personal_scope(source, home, target, apply=True, expected=preview)['applied']
    new = json.loads(receipt.read_text());new_registry = json.loads(registry.read_text())
    assert new['skill_scope'] == 'repo' and not new['personal_adapter_targets']
    assert new['adapter_targets'] == old['adapter_targets']
    assert new['adapter_transitions'] == old['adapter_transitions']
    assert custom.read_text() == 'keep'
    assert verify_install(source, home, target_root=target)['ok']
    assert bool(new_registry['personal_owners']) == other_reference
    if other_reference:
        assert (other / '.localsetup/lock.json').read_bytes() == other_before
        assert new_registry['personal_owners'] == json.loads(before[1])['personal_owners']
        assert verify_install(source, home, target_root=other)['ok']


def test_personal_retirement_cli_and_stale_guard(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context'], skill_scope='both'), home)
    receipt = root / '.localsetup/lock.json';before = receipt.read_bytes()
    preview = retire_personal_scope(root, home, root)
    prefix = ['--source-root', str(root), '--home', str(home)]
    options = ['--target-directory', str(root), '--skill-scope', 'repo']
    assert cli.main(prefix + ['plan'] + options) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == 'retire_personal_scope'
    assert receipt.read_bytes() == before
    receipt.write_bytes(before + b'\n')
    with pytest.raises(ValueError, match='stale_scope_retirement'):
        retire_personal_scope(root, home, root, apply=True, expected=preview)
    assert cli.main(prefix + ['update'] + options) == 0
    assert json.loads(capsys.readouterr().out)['applied']
    assert verify_install(root, home, target_root=root)['ok']


def test_retirement_mixes_retained_and_exclusive_owners(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    # Preserve historical shared plus exclusive physical-target coverage.
    from ls.tests.test_preferred_path_retention import prefer_common
    prefer_common(root, historical=True)
    other = tmp_path / 'other';other.mkdir()
    apply_plan(root, build_install_plan(root, home, platform_ids=['cursor', 'codex'],
        skills=['ls-localsetup-project-context'], skill_scope='both'), home)
    apply_plan(root, build_install_plan(root, home, platform_ids=['codex'],
        skills=['ls-localsetup-project-context'], skill_scope='personal', target_root=other), home)
    receipt = root / '.localsetup/lock.json';registry = Path(json.loads(receipt.read_text())['registry_path'])
    previous = json.loads(registry.read_text())
    other_before = (other / '.localsetup/lock.json').read_bytes()
    result = retire_personal_scope(root, home, root, apply=True)
    assert result['ok'] and len(result['retained_owners']) == len(result['owners']) == 1
    remaining = json.loads(registry.read_text())['personal_owners']
    assert {row['owner']['client'] for row in remaining.values()} == {'codex'}
    assert all(row == previous['personal_owners'][key] for key, row in remaining.items())
    assert (other / '.localsetup/lock.json').read_bytes() == other_before
    assert not (home / '.cursor/skills/ls-localsetup-project-context').exists()
    assert (home / '.agents/skills/ls-localsetup-project-context').exists()
    assert verify_install(root, home, target_root=root)['ok']
    assert verify_install(root, home, target_root=other)['ok']
