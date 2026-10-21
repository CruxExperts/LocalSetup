import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.config import InstallConfig
from ls.core.plan import build_install_plan
from ls.core.repair import run_repair
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_doctor_repairs_recorded_personal_target_without_repo_inference(tmp_path, mode, monkeypatch):
    import ls.core.repair as repair
    from ls.core.cli_install_support import _auto_default_context
    root = make_temp_repo(tmp_path)
    home = tmp_path / 'home'
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'],
                              skill_scope='personal', attach_mode=mode)
    apply_plan(root, plan, home)
    receipt = root / '.localsetup/lock.json'
    registry = Path(json.loads(receipt.read_text())['registry_path'])
    before = (receipt.read_bytes(), registry.read_bytes())
    package = home / '.agents/skills/ls-localsetup-project-context'
    if mode == 'symlink':
        package.unlink()
    else:
        (package / 'SKILL.md').write_text('drift')
    (home / '.agents/skills/custom.txt').write_text('preserve')
    def forbidden(*args, **kwargs):
        raise AssertionError('personal repair must not infer repository adapters')
    monkeypatch.setattr(repair, '_infer_platforms', forbidden)
    for repair_mode in ['report-only', 'migration-plan']:
        report = run_repair(root, home=home, apply=True, repair_mode=repair_mode)
        assert report['ok'] and report['actions'] and not report['applied']
        assert report['skill_scope'] == 'personal'
        assert report['inferred']['platforms'] == ['cursor']
        assert report['inferred']['repo_packages'] == []
    blocked = run_repair(root, home=home, apply=True, repair_mode='invalid')
    assert not blocked['ok'] and not blocked['applied']
    empty = run_repair(root, home=home, platform_ids=[], apply=True)
    assert empty['ok'] and not empty['actions'] and not empty['applied']
    applied = run_repair(root, home=home, apply=True)
    assert applied['ok'] and applied['applied'] and applied['verify']['ok']
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert (home / '.agents/skills/custom.txt').read_text() == 'preserve'
    context = _auto_default_context(root, home, InstallConfig(target_directory=str(root)), root)
    assert context['plan'] is not None and context['mode'] == 'recorded_personal'
    assert context['repair']['skill_scope'] == 'personal'
    assert not context['repair']['actions']
    lock = json.loads(receipt.read_text());lock['skill_scope'] = 'both'
    receipt.write_text(json.dumps(lock))
    both = run_repair(root, home=home, apply=True)
    assert both['ok'] and not both['applied']
    assert not both['actions']
