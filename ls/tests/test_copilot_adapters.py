import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.client_registry import load_client_registry
from ls.core.detach import detach_platforms
from ls.core.personal_detach import detach_personal
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_copilot_variants_share_paths_and_preserve_owners_and_native_state(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    clients = ['github-copilot-cli', 'github-copilot-vscode']
    for base in (root, home):
        path = base / '.agents/skills';path.mkdir(parents=True)
        (path / 'custom.txt').write_text('keep')
    native = home / '.copilot/settings.json';native.parent.mkdir(parents=True)
    native.write_text('{"fixture": true}')
    session = native.parent / 'session-state/fixture';session.parent.mkdir();session.write_text('keep session')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=clients,
                              skill_scope='both', attach_mode=mode)
    repo = [a for a in plan.actions if a.kind == 'attach_repo_path']
    personal = [a for a in plan.actions if a.kind == 'attach_personal_path']
    assert len(repo) == len(personal) == 1
    assert set(repo[0].details['platforms']) == set(clients)
    assert {o['client'] for o in personal[0].details['owners']} == set(clients)
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    receipt = root / '.localsetup/lock.json';lock = json.loads(receipt.read_text())
    assert {o['client'] for o in lock['adapter_targets'][0]['owners']} == set(clients)
    detach_platforms(root, home, root, [clients[0]])
    assert (root / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    assert verify_install(root, home, target_root=root)['ok']
    assert detach_personal(root, home, [clients[0]], apply=True)['applied']
    assert (home / '.agents/skills/ls-localsetup-project-context/SKILL.md').is_file()
    lock = json.loads(receipt.read_text())
    assert lock['platforms'] == [clients[1]]
    assert verify_install(root, home, target_root=root)['ok']
    for base in (root, home):assert (base / '.agents/skills/custom.txt').read_text() == 'keep'
    assert native.read_text() == '{"fixture": true}'
    assert session.read_text() == 'keep session'


def test_copilot_declarations_separate_host_and_filesystem_qualification():
    registry = load_client_registry(Path(__file__).resolve().parents[2])
    for variant_id, kind in [('github-copilot-cli', 'cli'), ('github-copilot-vscode', 'ide')]:
        row = registry.variant('github-copilot', variant_id).data
        assert row['kind'] == kind
        assert row['integration']['qualification']['host'] == 'not-run'
        assert row['skills']['repo']['precedence_status'] == 'unverified'
        assert row['config']['global']['status'] == 'unverified'
