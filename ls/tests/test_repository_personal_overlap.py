import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.registry import load_registry
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_repository_update_retains_personal_union_and_recovers(tmp_path, mode, monkeypatch):
    import ls.core.repository_overlap as overlap
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    def repo_plan(names, selected_mode=mode):
        return build_install_plan(root, home, skills=names, platform_ids=['cursor'],
                                  target_root=home, attach_mode=selected_mode)
    apply_plan(root, repo_plan(['ls-git-merge-rebase']), home)
    personal = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['openclaw'],
                                  skill_scope='personal', attach_mode=mode)
    apply_plan(root, personal, home)
    registry_path = Path(json.loads((root / '.localsetup/lock.json').read_text())['registry_path'])
    registry_bytes = registry_path.read_bytes()
    owners = load_registry(registry_path)['personal_owners']
    shared = home / '.agents/skills'
    (shared / 'custom.txt').write_text('preserve')
    update = repo_plan(['ls-write-run-tests'])
    write = overlap.write
    def fail(*args, **kwargs):
        write(*args, **kwargs)
        (shared / 'arrived.txt').write_text('preserve new neighbor')
        raise RuntimeError('injected overlap failure')
    monkeypatch.setattr(overlap, 'write', fail)
    with pytest.raises(RuntimeError, match='injected overlap failure'):
        apply_plan(root, update, home)
    assert registry_path.read_bytes() == registry_bytes
    assert (shared / 'ls-git-merge-rebase/SKILL.md').exists()
    assert (shared / 'arrived.txt').read_text() == 'preserve new neighbor'
    monkeypatch.setattr(overlap, 'write', write)
    apply_plan(root, update, home)
    assert load_registry(registry_path)['personal_owners'] == owners
    expected = set(update.rollback_metadata['repo_packages']) | set(personal.rollback_metadata['repo_packages'])
    assert set(json.loads((shared / '.localsetup-adapter.json').read_text())['packages']) == expected
    from ls.core.verify import verify_install
    verified = verify_install(root, home, target_root=home)
    assert verified['ok'], verified['issues']
    assert not (shared / 'ls-git-merge-rebase').exists()
    assert (shared / 'custom.txt').read_text() == 'preserve'
    with pytest.raises(RuntimeError, match='mode conflicts'):
        apply_plan(root, repo_plan(['ls-write-run-tests'], 'portable' if mode == 'symlink' else 'symlink'), home)

    for malformed in (None, []):
        registry = load_registry(registry_path)
        registry['personal_owners'] = malformed
        registry_path.write_text(json.dumps(registry))
        result = verify_install(root, home, target_root=home)
        assert not result['ok']
        assert any('invalid shared adapter ownership' in issue for issue in result['issues'])
