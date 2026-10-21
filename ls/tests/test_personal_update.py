import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.personal_update import build_recorded_personal_plan
from ls.core.personal_update import _RETIRED_WORKFLOWS, _unavailable_packages_message
from ls.tests.test_install_flow import make_temp_repo
from ls.core.plan import build_install_plan


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_recorded_personal_update_refreshes_without_reselection(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    for clients, names in [(['cursor'], ['ls-localsetup-project-context']), (['openclaw'], ['ls-git-merge-rebase'])]:
        plan = build_install_plan(root, home, skills=names, platform_ids=clients,
                                  skill_scope='personal', attach_mode=mode)
        apply_plan(root, plan, home)
    receipt = root / '.localsetup/lock.json'
    lock = json.loads(receipt.read_text())
    # Both recorded owners are the selected update target, with different selections.
    lock['platforms'] = ['cursor', 'openclaw']
    receipt.write_text(json.dumps(lock))
    registry_path = Path(lock['registry_path'])
    before = json.loads(registry_path.read_text())['personal_owners']
    (home / '.agents/skills/custom.txt').write_text('preserve')
    source = root / 'ls/skills/ls-localsetup-project-context/SKILL.md'
    source.write_text(source.read_text() + '\nFixture update text.\n')
    plan = build_recorded_personal_plan(root, home, root)
    assert json.loads(registry_path.read_text())['personal_owners'] == before
    actions = [a for a in plan.actions if a.kind == 'attach_personal_path']
    assert len({str(a.path) for a in actions}) == len(actions)
    assert all(a.details['mode'] == mode for a in actions)
    apply_plan(root, plan, home)
    assert json.loads(registry_path.read_text())['personal_owners'] == before
    assert 'Fixture update text.' in (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').read_text()
    assert (home / '.agents/skills/custom.txt').read_text() == 'preserve'
    stale = build_recorded_personal_plan(root, home, root)
    registry_path.write_text(registry_path.read_text() + '\n')
    original = receipt.read_bytes()
    with pytest.raises(RuntimeError, match='stale_recorded_plan'):
        apply_plan(root, stale, home)
    assert receipt.read_bytes() == original


def test_unavailable_recorded_packages_report_all_retirements_without_writes(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['cursor'], skill_scope='personal'), home)
    receipt = root / '.localsetup/lock.json'
    lock = json.loads(receipt.read_text());registry = Path(lock['registry_path'])
    missing = [*_RETIRED_WORKFLOWS, 'unknown-recorded-package']
    lock['global_baseline_packages'] = [*lock['global_baseline_packages'], *missing]
    receipt.write_text(json.dumps(lock))
    before = receipt.read_bytes(), registry.read_bytes()
    with pytest.raises(ValueError) as error:
        build_recorded_personal_plan(root, home, root)
    message = str(error.value)
    for retired, owner in _RETIRED_WORKFLOWS.items():
        assert f'{retired} (retired workflow; supported owning skill: {owner})' in message
    assert 'unknown-recorded-package (unavailable; no supported replacement is recorded)' in message
    assert before == (receipt.read_bytes(), registry.read_bytes())


def test_retirement_report_does_not_offer_an_absent_owner():
    message = _unavailable_packages_message({'ls-workflow-context-index-query'}, set())
    assert 'owning skill ls-context-index-search is also unavailable in this source' in message
