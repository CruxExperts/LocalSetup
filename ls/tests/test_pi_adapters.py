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
def test_pi_common_packages_preserve_shared_owners_and_native_resources(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    clients = ['pi-cli', 'github-copilot-cli']
    for base in (root, home):
        path = base / '.agents/skills';path.mkdir(parents=True)
        (path / 'custom.txt').write_text('keep')
    native = home / '.pi/agent/settings.json';native.parent.mkdir(parents=True)
    native.write_text('{"fixture": true}')
    session = native.parent / 'sessions/fixture';session.parent.mkdir();session.write_text('keep session')
    native_flat = native.parent / 'skills/custom.md';native_flat.parent.mkdir()
    native_flat.write_text('native flat skill')
    resource = root / 'ls/skills/ls-localsetup-project-context/references/pi-fixture.txt'
    resource.parent.mkdir(exist_ok=True);resource.write_text('package resource')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=clients,
                              skill_scope='both', attach_mode=mode)
    repo = [a for a in plan.actions if a.kind == 'attach_repo_path']
    personal = [a for a in plan.actions if a.kind == 'attach_personal_path']
    assert len(repo) == len(personal) == 1
    assert set(repo[0].details['platforms']) == set(clients)
    assert {o['client'] for o in personal[0].details['owners']} == set(clients)
    apply_plan(root, plan, home)
    assert verify_install(root, home, target_root=root)['ok']
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/references/pi-fixture.txt').read_text() == 'package resource'
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

    assert native_flat.read_text() == 'native flat skill'


def test_pi_metadata_does_not_attest_host_trust_or_extension_compatibility():
    registry = load_client_registry(Path(__file__).resolve().parents[2])
    row = registry.variant('pi', 'pi-cli').data
    assert tuple(row['executables']) == ('pi',)
    assert row['integration']['qualification']['host'] == 'not-run'
    assert row['skills']['repo']['precedence_status'] == 'unverified'
    assert row['insertion']['repo']['status'] == 'unverified'
