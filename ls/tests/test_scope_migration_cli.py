import json
from pathlib import Path

import pytest

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('scope', ['repo', 'personal'])
def test_cli_previews_and_applies_additive_scope(tmp_path, capsys, scope):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context'],
                                       skill_scope=scope), home)
    receipt = root / '.localsetup/lock.json'
    registry = Path(json.loads(receipt.read_text())['registry_path'])
    before = receipt.read_bytes(), registry.read_bytes()
    prefix = ['--source-root', str(root), '--home', str(home)]
    options = ['--target-directory', str(root), '--skill-scope', 'both', '--mode', 'portable']
    for command in ('plan', 'install'):
        assert cli.main(prefix + [command] + options) == 0
        report = json.loads(capsys.readouterr().out)
        assert report['auto_mode'] == 'additive_scope'
        assert before == (receipt.read_bytes(), registry.read_bytes())
    assert cli.main(prefix + ['update'] + options) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == 'additive_scope'
    result = json.loads(receipt.read_text())
    assert result['skill_scope'] == 'both'
    assert result['platforms'] == ['cursor']
    assert all(row['mode'] == 'portable' for key in ('adapter_targets', 'personal_adapter_targets') for row in result[key])
    assert verify_install(root, home, target_root=root)['ok']
    assert cli.main(prefix + ['update', '--target-directory', str(root), '--skill-scope', 'repo']) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == 'retire_personal_scope'
    assert json.loads(receipt.read_text())['skill_scope'] == 'repo'


def test_cli_rejects_scope_migration_with_reselection(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context']), home)
    receipt = root / '.localsetup/lock.json'
    registry = Path(json.loads(receipt.read_text())['registry_path'])
    before = receipt.read_bytes(), registry.read_bytes()
    assert cli.main(['--source-root', str(root), '--home', str(home), 'update',
        '--target-directory', str(root), '--skill-scope', 'both', '--platforms', 'cursor']) == 2
    assert 'separately' in capsys.readouterr().err
    assert before == (receipt.read_bytes(), registry.read_bytes())
