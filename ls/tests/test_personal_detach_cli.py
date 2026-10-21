import json
from pathlib import Path

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


def test_personal_detach_cli_plans_then_applies(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['cursor'], skill_scope='personal'), home)
    receipt = root / '.localsetup/lock.json'
    registry = Path(json.loads(receipt.read_text())['registry_path'])
    before = receipt.read_bytes(), registry.read_bytes()
    base = ['--source-root', str(root), '--home', str(home), 'detach',
            '--skill-scope', 'personal', '--platforms', 'cursor']
    for flags in ([], ['--plan']):
        assert cli.main(base + flags) == 0
        report = json.loads(capsys.readouterr().out)
        assert report['ok'] and not report['applied']
        assert before == (receipt.read_bytes(), registry.read_bytes())
    assert cli.main(base + ['--apply']) == 0
    assert json.loads(capsys.readouterr().out)['applied']
    assert not (home / '.agents/skills/ls-localsetup-project-context').exists()
    assert cli.main(base + ['--apply']) == 2
    report = json.loads(capsys.readouterr().out)
    assert not report['ok'] and 'no recorded personal owner' in report['blockers'][0]


def test_repository_detach_default_ignores_personal_install_scope(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['cursor'], skill_scope='both'), home)
    config = tmp_path / 'config.json';config.write_text(json.dumps({'skill_scope': 'personal'}))
    base = ['--source-root', str(root), '--home', str(home), 'detach', '--platforms', 'cursor', '--config', str(config)]
    assert cli.main(base + ['--plan']) == 2
    assert 'no changes made' in capsys.readouterr().err
    assert (root / '.agents/skills/ls-localsetup-project-context').exists()
    assert cli.main(base) == 0
    capsys.readouterr()
    assert not (root / '.agents/skills/ls-localsetup-project-context').exists()
    assert (home / '.agents/skills/ls-localsetup-project-context').exists()
