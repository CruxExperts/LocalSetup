import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.repair import run_repair
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
@pytest.mark.parametrize('shared', [False, True])
def test_combined_doctor_repairs_once_and_recovers_both_scopes(tmp_path, monkeypatch, mode, shared):
    import ls.core.combined_repair as engine
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    # Preserve historical shared plus exclusive physical-target coverage.
    from ls.tests.test_preferred_path_retention import prefer_common
    prefer_common(root, historical=True);target = home if shared else root
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'],
        skill_scope='both', attach_mode=mode, target_root=target), home)
    receipt = target / '.localsetup/lock.json'
    registry = Path(json.loads(receipt.read_text())['registry_path'])
    before = receipt.read_bytes(), registry.read_bytes()
    paths = {home / '.agents/skills', target / '.cursor/skills'}
    for path in paths:
        (path / 'custom.txt').write_text('keep')
        if mode == 'symlink':(path / 'ls-localsetup-project-context').unlink()
        else:(path / 'ls-localsetup-project-context/SKILL.md').write_text('drift')
    report = run_repair(root, home=home, target_root=target)
    assert report['ok'] and report['actions'] and not report['applied'], report
    assert before == (receipt.read_bytes(), registry.read_bytes())
    original = engine.write_entries;seen = []
    def fail(boundary, action, *args, **kwargs):
        original(boundary, action, *args, **kwargs)
        seen.append(str(action.path))
        (action.path / 'new.txt').write_text('arrived')
        if len(seen) == 2:raise RuntimeError('injected after second physical repair')
    monkeypatch.setattr(engine, 'write_entries', fail)
    failed = run_repair(root, home=home, target_root=target, apply=True)
    assert not failed['ok'] and failed['personal']['recovery_ok'], failed
    assert len(seen) == len(set(seen)) == 2, failed.get('blockers')
    for path in paths:
        if mode == 'symlink':assert not (path / 'ls-localsetup-project-context').exists()
        else:assert (path / 'ls-localsetup-project-context/SKILL.md').read_text() == 'drift'
    monkeypatch.setattr(engine, 'write_entries', original)
    done = run_repair(root, home=home, target_root=target, apply=True)
    assert done['ok'] and done['applied'] and done['verify']['ok'], done
    assert before == (receipt.read_bytes(), registry.read_bytes())
    for path in paths:assert (path / 'custom.txt').read_text() == 'keep'
    assert verify_install(root, home, target_root=target)['ok']
    assert not run_repair(root, home=home, target_root=target)['actions']


def test_combined_repair_detects_unrecorded_entries_and_portable_links(tmp_path):
    import shutil
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'],
        skill_scope='both', attach_mode='portable'), home)
    path = root / '.agents/skills'
    package = path / 'ls-localsetup-project-context'
    (package / 'unrecorded-link').symlink_to('SKILL.md')
    report = run_repair(root, home=home)
    assert report['ok'] and any(a['path'] == str(path) for a in report['actions'])
    repaired = run_repair(root, home=home, apply=True)
    assert repaired['ok'] and repaired['applied'], repaired
    assert not (package / 'unrecorded-link').is_symlink()
    shutil.copytree(package, path / 'ls-unrecorded')
    blocked = run_repair(root, home=home, apply=True)
    assert not blocked['ok'] and not blocked['applied']
    assert any('preservation review' in b for b in blocked['blockers'])
    assert (path / 'ls-unrecorded/SKILL.md').exists()
