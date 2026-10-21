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
def test_openclaw_common_packages_preserve_shared_owners_and_native_resources(tmp_path, mode):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    clients = ['openclaw', 'github-copilot-cli']
    for base in (root, home):
        path = base / '.agents/skills';path.mkdir(parents=True)
        (path / 'custom.txt').write_text('keep')
    native = home / '.openclaw/openclaw.json';native.parent.mkdir(parents=True)
    native.write_text('{"fixture": true}')
    session = native.parent / 'sessions/fixture';session.parent.mkdir();session.write_text('keep session')
    native_flat = native.parent / 'skills/custom.md';native_flat.parent.mkdir()
    native_flat.write_text('native flat skill')
    resource = root / 'ls/skills/ls-localsetup-project-context/references/openclaw-fixture.txt'
    resource.parent.mkdir(exist_ok=True);resource.write_text('package resource')
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=clients,
                              skill_scope='both', attach_mode=mode)
    repo = [a for a in plan.actions if a.kind == 'attach_repo_path']
    personal = [a for a in plan.actions if a.kind == 'attach_personal_path']
    assert len(repo) == len(personal) == 1
    assert set(repo[0].details['platforms']) == set(clients)
    assert {o['client'] for o in personal[0].details['owners']} == set(clients)
    apply_plan(root, plan, home)
    report = verify_install(root, home, target_root=root)
    assert report['ok']
    loading = next(row for row in report['native_loading'] if row['client'] == 'openclaw' and row['scope'] == 'repo')
    assert loading['status'] == ('source-contained' if mode == 'portable' else 'unsupported-project-source')
    for base in (root, home):
        assert (base / '.agents/skills/ls-localsetup-project-context/references/openclaw-fixture.txt').read_text() == 'package resource'
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



def test_default_state_predicate_and_shared_owner_refresh(tmp_path, monkeypatch):
    from ls.core.openclaw_prerequisite import openclaw_personal_root
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.delenv('OPENCLAW_STATE_DIR', raising=False)
    monkeypatch.delenv('OPENCLAW_HOME', raising=False)
    assert openclaw_personal_root(home)['ok']
    monkeypatch.setenv('OPENCLAW_STATE_DIR', str(home / '.openclaw'))
    assert openclaw_personal_root(home)['ok']
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['openclaw', 'github-copilot-cli'], skill_scope='personal'), home)
    marker = home / '.agents/skills/ls-localsetup-project-context/SKILL.md';before = marker.read_bytes()
    monkeypatch.setenv('OPENCLAW_STATE_DIR', str(home / '.openclaw-private'))
    assert not openclaw_personal_root(home)['ok']
    plan = build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['github-copilot-cli'], skill_scope='personal')
    with pytest.raises(RuntimeError, match='OpenClaw'):
        apply_plan(root, plan, home)
    assert marker.read_bytes() == before
    assert detach_personal(root, home, ['openclaw'], apply=True)['applied']
    assert marker.read_bytes() == before


def test_omitted_update_preserves_historical_personal_native_adapter(tmp_path, capsys, monkeypatch):
    import yaml
    from ls.core import cli
    from ls.core.client_registry import write_platforms_projection
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.delenv('OPENCLAW_STATE_DIR', raising=False)
    catalog = root / 'ls/config/clients.yaml';current = catalog.read_text()
    data = yaml.safe_load(current)
    row = next(f for f in data['families'] if f['id'] == 'openclaw')['variants'][0]
    row['compatibility']['global_write_paths'] = ['~/.agents/skills', '~/.openclaw/skills']
    catalog.write_text(yaml.safe_dump(data, sort_keys=False))
    write_platforms_projection(root, load_client_registry(root))
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['openclaw'], skill_scope='personal'), home)
    native = home / '.openclaw/skills';(native / 'custom.txt').write_text('keep')
    catalog.write_text(current);write_platforms_projection(root, load_client_registry(root))
    assert cli.main(['--source-root', str(root), '--home', str(home), 'update']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['auto_mode'] == 'recorded_personal'
    assert (native / 'ls-localsetup-project-context/SKILL.md').is_file()
    assert (native / 'custom.txt').read_text() == 'keep'


def test_combined_repository_repair_checks_other_target_personal_owner(tmp_path, monkeypatch):
    from ls.core.combined_repair import repair_combined
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.delenv('OPENCLAW_STATE_DIR', raising=False)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['openclaw'], skill_scope='personal'), home)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['codex', 'claude-code'], skill_scope='both', target_root=home), home)
    detach_personal(root, home, ['codex'], apply=True)
    package = home / '.agents/skills/ls-localsetup-project-context';package.unlink()
    receipt = home / '.localsetup/lock.json';before = receipt.read_bytes()
    monkeypatch.setenv('OPENCLAW_STATE_DIR', str(home / '.openclaw-private'))
    report = repair_combined(root, home, home, ['codex'], apply=True)
    assert not report['ok'] and not report['applied'], report
    assert any('OpenClaw' in message for message in report['blockers']), report
    assert not package.exists()
    assert receipt.read_bytes() == before
