import json

import pytest

from ls.core import cli
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
def test_public_scope_plan_does_not_select_clients_or_write_state(tmp_path, capsys, scope):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home';target = tmp_path / 'target'
    args = ['--source-root', str(root), '--home', str(home), 'plan', '--target-directory', str(target),
            '--skill-scope', scope]
    assert cli.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['rollback']['skill_scope'] == scope
    assert payload['attachment']['platforms'] == []
    assert not any(a['kind'].startswith('attach_') for a in payload['actions'])
    assert not target.exists() and not home.exists()
    assert cli.main(args + ['--platforms', 'cursor', '--skills', 'ls-localsetup-project-context']) == 0
    payload = json.loads(capsys.readouterr().out)
    kinds = {a['kind'] for a in payload['actions'] if a['kind'].startswith('attach_')}
    assert kinds == ({'attach_repo_path'} if scope == 'repo' else {'attach_personal_path'}
                     if scope == 'personal' else {'attach_repo_path', 'attach_personal_path'})


def test_public_personal_install_config_override_and_retained_update(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home';target = tmp_path / 'target'
    target.mkdir();config = tmp_path / 'config.json'
    config.write_text(json.dumps({'skill_scope': 'repo'}))
    base = ['--source-root', str(root), '--home', str(home)]
    assert cli.main(base + ['install', '--target-directory', str(target), '--config', str(config),
                            '--skill-scope', 'personal', '--platforms', 'cursor', '--skills', 'ls-localsetup-project-context', '--apply']) == 0
    capsys.readouterr()
    receipt = target / '.localsetup/lock.json'
    assert json.loads(receipt.read_text())['skill_scope'] == 'personal'
    assert (home / '.agents/skills/ls-localsetup-project-context').exists()
    assert not (target / '.cursor/skills').exists()
    assert not (target / '.agents/skills').exists()
    assert cli.main(base + ['update', '--target-directory', str(target)]) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == 'recorded_personal'
    before = receipt.read_bytes()
    assert cli.main(base + ['update', '--target-directory', str(target), '--skill-scope', 'repo']) == 2
    assert 'ownership migration' in capsys.readouterr().err
    assert receipt.read_bytes() == before
