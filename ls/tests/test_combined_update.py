import json
from pathlib import Path

import pytest

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.personal_update import build_recorded_both_plan
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_recorded_both_update_retains_selections_and_rejects_stale_plan(tmp_path, capsys, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context', 'ls-git-merge-rebase'],
        platform_ids=['cursor'], skill_scope='both', attach_mode=mode)
    for action in plan.actions:
        if action.kind == 'attach_personal_path':action.details['packages'] = ['ls-localsetup-project-context']
    apply_plan(root, plan, home)
    receipt = root / '.localsetup/lock.json'
    lock = json.loads(receipt.read_text());registry = Path(lock['registry_path'])
    # Historical receipts survive refresh without dispatching their operations.
    lock['adapter_transitions'] = [{'id': 'historical-fixture', 'disposition': 'removed', 'removed': ['old-path']}]
    receipt.write_text(json.dumps(lock))
    before = receipt.read_bytes(), registry.read_bytes()
    owners = json.loads(registry.read_text())['personal_owners']
    source = root / 'ls/skills/ls-localsetup-project-context/SKILL.md'
    source.write_text(source.read_text() + '\nCombined update fixture.\n')
    (root / '.agents/skills/custom.txt').write_text('keep')
    args = ['--source-root', str(root), '--home', str(home), 'plan', '--target-directory', str(root)]
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['auto_mode'] == 'recorded_both'
    assert before == (receipt.read_bytes(), registry.read_bytes())
    update = build_recorded_both_plan(root, home, root)
    assert not any(a.kind == 'retire_historical_adapter' for a in update.actions)
    args[4] = 'update'
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == 'recorded_both'
    refreshed = json.loads(receipt.read_text())
    assert refreshed['skill_scope'] == 'both'
    assert refreshed['adapter_targets'] == lock['adapter_targets']
    assert refreshed['adapter_state'] == lock['adapter_state']
    assert refreshed['adapter_transitions'] == lock['adapter_transitions']
    assert json.loads(registry.read_text())['personal_owners'] == owners
    assert 'Combined update fixture.' in (root / '.agents/skills/ls-localsetup-project-context/SKILL.md').read_text()
    assert 'Combined update fixture.' in (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').read_text()
    assert not (home / '.agents/skills/ls-git-merge-rebase').exists()
    assert (root / '.agents/skills/custom.txt').read_text() == 'keep'
    stale = build_recorded_both_plan(root, home, root)
    registry.write_text(registry.read_text() + '\n')
    before = receipt.read_bytes()
    with pytest.raises(RuntimeError, match='stale_recorded_plan'):apply_plan(root, stale, home)
    assert receipt.read_bytes() == before
