import json
from pathlib import Path

import pytest

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('scope', ['personal', 'both'])
def test_recorded_mode_updates_preserve_selection_and_honor_explicit_config(tmp_path, capsys, scope):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'],
        skill_scope=scope), home)
    receipt = root / '.localsetup/lock.json'
    original = json.loads(receipt.read_text())
    registry = Path(original['registry_path'])
    owners = json.loads(registry.read_text())['personal_owners']
    args = ['--source-root', str(root), '--home', str(home), 'plan', '--target-directory', str(root)]
    before = receipt.read_bytes(), registry.read_bytes()
    assert cli.main(args + ['--mode', 'portable']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert all(a['details']['mode'] == 'portable' for a in plan['actions'] if a['kind'].startswith('attach_'))
    assert before == (receipt.read_bytes(), registry.read_bytes())
    args[4] = 'update'
    config = tmp_path / 'config.json';config.write_text(json.dumps({'attach_mode': 'portable'}))
    assert cli.main(args + ['--config', str(config)]) == 0
    capsys.readouterr()
    assert not (home / '.agents/skills/ls-localsetup-project-context').is_symlink()
    # Omitting mode retains portable; an explicit CLI mode overrides configuration.
    assert cli.main(args) == 0
    capsys.readouterr()
    assert not (home / '.agents/skills/ls-localsetup-project-context').is_symlink()
    assert cli.main(args + ['--config', str(config), '--mode', 'symlink']) == 0
    capsys.readouterr()
    assert (home / '.agents/skills/ls-localsetup-project-context').is_symlink()
    final = json.loads(receipt.read_text())
    assert final['platforms'] == original['platforms']
    assert final['repo_packages'] == original['repo_packages']
    assert final['adapter_state'] == original['adapter_state']
    assert json.loads(registry.read_text())['personal_owners'] == owners


def test_recorded_mode_refuses_conflicting_unselected_personal_owner(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    for client in ['openclaw', 'cursor']:
        apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=[client],
            skill_scope='personal'), home)
    receipt = root / '.localsetup/lock.json';lock = json.loads(receipt.read_text())
    registry = Path(lock['registry_path']);before = receipt.read_bytes(), registry.read_bytes()
    assert cli.main(['--source-root', str(root), '--home', str(home), 'update',
        '--target-directory', str(root), '--mode', 'portable']) == 2
    assert 'conflicts with another owner' in capsys.readouterr().err
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert (home / '.agents/skills/ls-localsetup-project-context').is_symlink()


def test_inferred_repository_update_honors_mode_without_changing_selection(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor']), home)
    receipt = root / '.localsetup/lock.json';original = json.loads(receipt.read_text())
    adapter = root / '.agents/skills';(adapter / 'custom.txt').write_text('keep')
    args = ['--source-root', str(root), '--home', str(home), 'plan', '--target-directory', str(root)]
    before = receipt.read_bytes()
    assert cli.main(args + ['--mode', 'portable']) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned['auto_mode'] == 'inferred_existing'
    assert all(a['details']['mode'] == 'portable' for a in planned['actions'] if a['kind'] == 'attach_repo_path')
    assert receipt.read_bytes() == before
    args[4] = 'update'
    assert cli.main(args + ['--mode', 'portable']) == 0
    capsys.readouterr()
    assert not (adapter / 'ls-localsetup-project-context').is_symlink()
    assert (adapter / 'ls-localsetup-project-context/SKILL.md').exists()
    assert cli.main(args) == 0
    capsys.readouterr()
    final = json.loads(receipt.read_text())
    assert final['attach_mode'] == 'portable'
    assert final['repo_packages'] == original['repo_packages']
    assert final['platforms'] == original['platforms']
    assert (adapter / 'custom.txt').read_text() == 'keep'
