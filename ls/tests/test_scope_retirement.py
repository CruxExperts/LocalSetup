import json
from pathlib import Path

import pytest

from ls.core import cli, detach
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.scope_retirement import retire_repository_scope
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
@pytest.mark.parametrize('shared', [False, True])
def test_retirement_preserves_personal_state_and_recovers_failure(tmp_path, monkeypatch, mode, shared):
    source = make_temp_repo(tmp_path);home = tmp_path / 'home';home.mkdir()
    target = home if shared else source
    apply_plan(source, build_install_plan(source, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context'],
        skill_scope='both', attach_mode=mode, target_root=target), home)
    receipt = target / '.localsetup/lock.json'
    old = json.loads(receipt.read_text());registry = Path(old['registry_path'])
    owners = json.loads(registry.read_text())['personal_owners']
    custom = target / '.agents/skills/custom.txt';custom.write_text('keep')
    before = receipt.read_bytes(), registry.read_bytes()
    preview = retire_repository_scope(source, home, target)
    assert not preview['applied']
    assert before == (receipt.read_bytes(), registry.read_bytes())
    original = detach.save_json
    def fail(path, value):
        if path == receipt:
            (custom.parent / 'new-custom.txt').write_text('created during failure')
            raise OSError('receipt failure fixture')
        return original(path, value)
    monkeypatch.setattr(detach, 'save_json', fail)
    with pytest.raises(OSError, match='receipt failure fixture'):
        retire_repository_scope(source, home, target, apply=True, expected=preview)
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert custom.read_text() == 'keep'
    assert (custom.parent / 'new-custom.txt').read_text() == 'created during failure'
    assert verify_install(source, home, target_root=target)['ok']
    monkeypatch.setattr(detach, 'save_json', original)
    result = retire_repository_scope(source, home, target, apply=True, expected=preview)
    assert result['applied']
    new = json.loads(receipt.read_text())
    assert new['skill_scope'] == 'personal'
    assert new['adapter_targets'] == []
    assert new['personal_adapter_targets'] == old['personal_adapter_targets']
    assert new['adapter_transitions'] == old['adapter_transitions']
    assert json.loads(registry.read_text())['personal_owners'] == owners
    assert custom.read_text() == 'keep'
    assert (custom.parent / 'new-custom.txt').read_text() == 'created during failure'
    assert verify_install(source, home, target_root=target)['ok']


def test_retirement_cli_preview_apply_and_stale_guard(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context'],
                                      skill_scope='both'), home)
    receipt = root / '.localsetup/lock.json';before = receipt.read_bytes()
    preview = retire_repository_scope(root, home, root)
    prefix = ['--source-root', str(root), '--home', str(home)]
    options = ['--target-directory', str(root), '--skill-scope', 'personal']
    assert cli.main(prefix + ['plan'] + options) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == 'retire_repository_scope'
    assert receipt.read_bytes() == before
    receipt.write_bytes(before + b'\n')
    with pytest.raises(ValueError, match='stale_scope_retirement'):
        retire_repository_scope(root, home, root, apply=True, expected=preview)
    assert cli.main(prefix + ['install'] + options + ['--apply']) == 0
    assert json.loads(capsys.readouterr().out)['applied']
    assert verify_install(root, home, target_root=root)['ok']
