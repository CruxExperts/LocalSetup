import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_both_scopes_write_shared_path_once_and_keep_receipts(tmp_path, mode, monkeypatch):
    import ls.core.personal_adapter as writer
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context', 'ls-git-workflows'], platform_ids=['cursor'],
                              target_root=home, skill_scope='both', attach_mode=mode)
    for action in plan.actions:
        if action.kind == 'attach_repo_path':action.details['packages'] = ['ls-localsetup-project-context']
        if action.kind == 'attach_personal_path':action.details['packages'] = ['ls-git-workflows']
    written = [];write = writer.write
    def record(*args, **kwargs):
        written.append(str(args[2].path))
        return write(*args, **kwargs)
    monkeypatch.setattr(writer, 'write', record)
    apply_plan(root, plan, home)
    shared = home / '.agents/skills'
    assert written.count(str(shared)) == 1
    lock = json.loads((home / '.localsetup/lock.json').read_text())
    assert all(row['packages'] == ['ls-localsetup-project-context'] for row in lock['adapter_targets'])
    assert all(row['packages'] == ['ls-git-workflows'] for row in lock['personal_adapter_targets'])
    assert set(json.loads((shared / '.localsetup-adapter.json').read_text())['packages']) == {'ls-localsetup-project-context', 'ls-git-workflows'}
    verified = verify_install(root, home, target_root=home)
    assert verified['ok'], verified['issues']
    before = (home / '.localsetup/lock.json').read_bytes()
    paired = next(a for a in plan.actions if a.kind == 'attach_personal_path' and a.path == shared)
    paired.details['mode'] = 'portable' if mode == 'symlink' else 'symlink'
    with pytest.raises(RuntimeError, match='same mode and package library'):
        apply_plan(root, plan, home)
    assert (home / '.localsetup/lock.json').read_bytes() == before
