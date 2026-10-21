import json
from pathlib import Path

import pytest

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.personal_inventory import personal_inventory
from ls.core.plan import build_install_plan
from ls.core.rollback import rollback
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_two_step_scope_conversion_round_trip(tmp_path, capsys, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context'],
        skill_scope='repo', attach_mode=mode), home)
    receipt = root / '.localsetup/lock.json'
    old = json.loads(receipt.read_text());registry = Path(old['registry_path'])
    (root / '.agents/skills/custom.txt').write_text('keep')
    prefix = ['--source-root', str(root), '--home', str(home)]
    for scope in ['both', 'personal', 'both', 'repo']:
        options = ['--target-directory', str(root), '--skill-scope', scope]
        before = receipt.read_bytes(), registry.read_bytes()
        assert cli.main(prefix + ['plan'] + options) == 0
        capsys.readouterr()
        assert before == (receipt.read_bytes(), registry.read_bytes())
        assert cli.main(prefix + ['install'] + options + ['--apply']) == 0
        capsys.readouterr()
        current = json.loads(receipt.read_text())
        assert current['skill_scope'] == scope
        assert current['platforms'] == ['cursor']
        assert current['attach_mode'] == mode
        assert verify_install(root, home, target_root=root)['ok']
        assert (root / '.agents/skills/custom.txt').read_text() == 'keep'
        assert set(current['repo_packages']) == set(old['repo_packages'])
    assert json.loads(receipt.read_text())['adapter_targets'] == old['adapter_targets']


def test_personal_only_repository_rollback_retains_independent_owner(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, platform_ids=['cursor'], skills=['ls-localsetup-project-context'],
                                       skill_scope='personal'), home)
    receipt = root / '.localsetup/lock.json';lock = json.loads(receipt.read_text())
    registry = Path(lock['registry_path']);before = json.loads(registry.read_text())
    custom = home / '.agents/skills/custom.txt';custom.write_text('keep')
    rollback(root, home, target_root=root)
    assert not receipt.exists()
    current = json.loads(registry.read_text())
    assert str(root.resolve()) not in current['targets']
    assert current['personal_owners'] == before['personal_owners']
    assert personal_inventory(root, home, ['cursor'])['ok']
    assert custom.read_text() == 'keep'
    assert (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
