import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.detach import detach_platforms
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_shared_detach_preserves_personal_receipts_and_failure_neighbors(tmp_path, mode, monkeypatch):
    import ls.core.detach as detach
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context', 'ls-git-workflows'], platform_ids=['cursor'],
                              target_root=home, skill_scope='both', attach_mode=mode)
    for action in plan.actions:
        if action.kind == 'attach_repo_path':action.details['packages'] = ['ls-git-workflows']
        if action.kind == 'attach_personal_path':action.details['packages'] = ['ls-localsetup-project-context']
    apply_plan(root, plan, home)
    receipt = home / '.localsetup/lock.json';lock = json.loads(receipt.read_text())
    registry = Path(lock['registry_path']);before = (receipt.read_bytes(), registry.read_bytes())
    personal = json.loads(registry.read_text())['personal_owners']
    shared = home / '.agents/skills';(shared / 'custom.txt').write_text('preserve')
    save = detach.save_json
    def fail(path, value):
        if path == registry:
            (shared / 'arrived.txt').write_text('preserve new neighbor')
            raise RuntimeError('injected receipt failure')
        return save(path, value)
    monkeypatch.setattr(detach, 'save_json', fail)
    with pytest.raises(RuntimeError, match='injected receipt failure'):
        detach_platforms(root, home, home, ['cursor'])
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert (shared / 'ls-git-workflows/SKILL.md').exists()
    assert (shared / 'arrived.txt').read_text() == 'preserve new neighbor'
    monkeypatch.setattr(detach, 'save_json', save)
    import ls.core.apply_journal as journals
    def fail_cleanup(journal):
        # Delete a real package backup before failing, as a partially successful cleanup can.
        entry = next(item for item in journal['touched'] if Path(item['path']).name == 'ls-git-workflows')
        journals.remove_path(Path(entry['backup']))
        raise OSError('injected cleanup failure')
    monkeypatch.setattr(journals, 'cleanup_backups', fail_cleanup)
    result = detach_platforms(root, home, home, ['cursor'])
    assert any('detach committed' in warning for warning in result['warnings'])
    assert json.loads(Path(result['journal']).read_text())['status'] == 'committed'
    assert str(shared / 'ls-git-workflows') in result['removed']
    assert not (shared / 'ls-git-workflows').exists()
    assert (shared / 'ls-localsetup-project-context/SKILL.md').exists()
    assert (shared / 'custom.txt').read_text() == 'preserve'
    updated = json.loads(receipt.read_text())
    assert updated['skill_scope'] == 'personal' and updated['platforms'] == ['cursor']
    assert updated['adapter_targets'] == []
    assert updated['personal_adapter_targets'] == lock['personal_adapter_targets']
    assert json.loads(registry.read_text())['personal_owners'] == personal
    report = verify_install(root, home, target_root=home)
    assert report['ok'], report['issues']
