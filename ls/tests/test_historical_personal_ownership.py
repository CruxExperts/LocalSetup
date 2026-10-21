import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


def use_historical_openclaw_personal_paths(root):
    """Exercise the old dual-path ownership contract independently of fresh defaults."""
    import yaml
    from ls.core.client_registry import load_client_registry, write_platforms_projection
    path = root / 'ls/config/clients.yaml';data = yaml.safe_load(path.read_text())
    row = next(f for f in data['families'] if f['id'] == 'openclaw')['variants'][0]
    row['compatibility']['global_write_paths'] = ['~/.agents/skills', '~/.openclaw/skills']
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    write_platforms_projection(root, load_client_registry(root))


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
@pytest.mark.parametrize('existing', [False, True])
def test_historical_openclaw_path_retains_current_personal_owner(tmp_path, mode, existing):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    use_historical_openclaw_personal_paths(root)
    if existing:
        apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['openclaw'],
            skill_scope='personal', attach_mode=mode), home)
        (home / '.openclaw/skills/custom.txt').write_text('keep')
    plan = build_install_plan(root, home, skills=['ls-git-workflows'], platform_ids=['openclaw'],
        skill_scope='repo' if existing else 'both', attach_mode=mode, target_root=home)
    transition = next(a for a in plan.actions if a.kind == 'retire_historical_adapter')
    if not existing:assert transition.details['disposition'] == 'delegated-current-personal'
    apply_plan(root, plan, home)
    lock = json.loads((home / '.localsetup/lock.json').read_text())
    receipt = next(r for r in lock['adapter_transitions'] if r['from'] == str(home / '.openclaw/skills'))
    assert receipt['disposition'] == ('preserved-current-personal' if existing else 'delegated-current-personal')
    assert (home / '.openclaw/skills' / ('ls-localsetup-project-context' if existing else 'ls-git-workflows') / 'SKILL.md').exists()
    if existing:assert (home / '.openclaw/skills/custom.txt').read_text() == 'keep'
    result = verify_install(root, home, target_root=home)
    assert result['ok'], result['issues']


def test_delegated_retirement_excludes_old_repository_selection(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    use_historical_openclaw_personal_paths(root)
    apply_plan(root, build_install_plan(root, home, skills=['ls-git-workflows'], platform_ids=['openclaw'],
        target_root=home), home)
    receipt = home / '.localsetup/lock.json';lock = json.loads(receipt.read_text())
    registry_path = Path(lock['registry_path']);registry = json.loads(registry_path.read_text())
    old = home / '.agents/skills';historical = home / '.openclaw/skills'
    historical.parent.mkdir(parents=True, exist_ok=True);old.rename(historical)
    for row in lock['adapter_targets']:row['path'] = str(historical)
    lock['adapter_state'] = [str(historical)]
    for row in registry['targets'][str(home.resolve())]['adapters']:row['path'] = str(historical)
    receipt.write_text(json.dumps(lock));registry_path.write_text(json.dumps(registry))
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['openclaw'],
        skill_scope='both', target_root=home)
    for action in plan.actions:
        if action.kind == 'attach_personal_path':action.details['packages'] = ['ls-localsetup-project-context']
    apply_plan(root, plan, home)
    assert (historical / 'ls-localsetup-project-context/SKILL.md').exists()
    assert not (historical / 'ls-git-workflows').exists()
    result = verify_install(root, home, target_root=home)
    assert result['ok'], result['issues']
