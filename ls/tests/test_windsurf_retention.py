"""Retained client receipts, without qualifying fresh host installations."""
import json
from pathlib import Path

import pytest
import yaml

from ls.core.apply import apply_plan
from ls.core.client_registry import load_client_registry, platform_rows
from ls.core.detach import detach_platforms
from ls.core.personal_detach import detach_personal
from ls.core.personal_update import build_recorded_both_plan
from ls.core.plan import build_install_plan
from ls.core.verify import verify_install
from ls.tests.test_install_flow import make_temp_repo


def retained_roots(client):
    return ('.continue/skills', '~/.continue/skills') if client == 'continue-cli-legacy' else ('.agents/skills', '~/.agents/skills')


def historical_row(data, client):
    prior = dict(next(p for p in data['platforms'] if p['id'] == 'github-copilot-cli'))
    repo, personal = retained_roots(client)
    prior.update(id=client, repo_paths=[repo], global_paths=[personal], rollback_targets=[repo])
    return prior


def test_cascade_is_retained_without_fresh_projection():
    root = Path(__file__).resolve().parents[2]
    registry = load_client_registry(root)
    row = registry.variant('windsurf', 'windsurf-cascade').data
    assert row['integration']['lifecycle'] == 'retained-only'
    assert row['integration']['qualification']['host'] == 'blocked'
    assert 'windsurf-cascade' not in {p['id'] for p in platform_rows(registry)}
    with pytest.raises(ValueError, match='unknown platform selector'):
        build_install_plan(root, root / 'unused-home', platform_ids=['windsurf-cascade'])


@pytest.mark.parametrize('client', ['windsurf-cascade', 'roo-code-legacy', 'continue-cli-legacy'])
@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_historical_receipt_survives_catalog_retention(tmp_path, mode, client):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    catalog = root / 'ls/config/platforms.yaml';current = catalog.read_bytes()
    # Synthetic historical catalog supplies a real receipt; this is not host evidence.
    data = yaml.safe_load(current)
    data['platforms'].append(historical_row(data, client))
    catalog.write_text(yaml.safe_dump(data))
    preserved = [base / rel for base in (root, home) for rel in (
        '.roo/skills/custom/SKILL.md', '.roo/skills-code/custom/SKILL.md',
        '.agents/skills-code/custom/SKILL.md', '.roo/rules/custom.md',
        '.continue/config.yaml', '.continue/sessions/fixture.json',
        '.claude/skills/custom/SKILL.md', '.agents/skills/unmanaged/SKILL.md')]
    preserved.append(home / '.codeium/windsurf/skills/custom/SKILL.md')
    for native in preserved:
        native.parent.mkdir(parents=True, exist_ok=True);native.write_text('native fixture')
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=[client], skill_scope='both', attach_mode=mode), home)
    catalog.write_bytes(current)
    repo_path, personal_path = retained_roots(client)
    adapters = (root / repo_path, home / personal_path.removeprefix('~/'))
    for adapter in adapters:(adapter / 'custom.txt').write_text('keep')
    assert verify_install(root, home, target_root=root)['ok']
    apply_plan(root, build_recorded_both_plan(root, home, root), home)
    assert verify_install(root, home, target_root=root)['ok']
    lock = json.loads((root / '.localsetup/lock.json').read_text())
    assert lock['platforms'] == [client]
    assert lock['skill_scope'] == 'both'
    detach_platforms(root, home, root, [client])
    assert detach_personal(root, home, [client], apply=True)['applied']
    for adapter in adapters:
        assert (adapter / 'custom.txt').read_text() == 'keep'
        assert not (adapter / 'ls-localsetup-project-context').exists()
    assert all(native.read_text() == 'native fixture' for native in preserved)


@pytest.mark.parametrize('client', ['windsurf-cascade', 'roo-code-legacy', 'continue-cli-legacy'])
@pytest.mark.parametrize('scope', ['repo', 'personal'])
def test_retained_cli_update_preserves_recorded_client(tmp_path, monkeypatch, capsys, scope, client):
    from ls.core import cli
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.setattr(cli, '_is_global_shim_invocation', lambda: False)
    catalog = root / 'ls/config/platforms.yaml';current = catalog.read_bytes()
    data = yaml.safe_load(current)
    data['platforms'].append(historical_row(data, client))
    catalog.write_text(yaml.safe_dump(data))
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=[client], skill_scope=scope, attach_mode='portable'), home)
    catalog.write_bytes(current)
    receipt = root / '.localsetup/lock.json';before = receipt.read_bytes()
    old = json.loads(before);registry = Path(old['registry_path']);registry_before = registry.read_bytes()
    prefix = ['--source-root', str(root), '--home', str(home)]
    assert cli.main(prefix + ['plan']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['auto_mode'] == f'recorded_{scope}'
    assert receipt.read_bytes() == before and registry.read_bytes() == registry_before
    assert cli.main(prefix + ['update']) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == report['auto_mode']
    new = json.loads(receipt.read_text())
    assert new['platforms'] == old['platforms'] == [client]
    assert new['adapter_targets'] == old['adapter_targets']
    assert len(new['personal_adapter_targets']) == len(old['personal_adapter_targets'])
    for current_row, old_row in zip(new['personal_adapter_targets'], old['personal_adapter_targets']):
        assert all(current_row[k] == value for k, value in old_row.items())
    assert new['skill_scope'] == scope
    assert verify_install(root, home, target_root=root)['ok']


@pytest.mark.parametrize('client', ['windsurf-cascade', 'roo-code-legacy', 'continue-cli-legacy'])
def test_retained_repository_doctor_refuses_inference_and_preserves_receipt(tmp_path, client):
    from ls.core.repair import run_repair
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=['github-copilot-cli']), home)
    receipt = root / '.localsetup/lock.json';lock = json.loads(receipt.read_text())
    lock['platforms'] = [client]
    for row in lock['adapter_targets']:
        row['platform'] = client;row['platforms'] = [client]
        for owner in row['owners']:owner['client'] = client
    receipt.write_text(json.dumps(lock));before = receipt.read_bytes()
    registry = Path(lock['registry_path']);registry_before = registry.read_bytes()
    custom = root / '.agents/skills/custom.txt';custom.write_text('keep')
    for apply in (False, True):
        result = run_repair(root, home=home, target_root=root, apply=apply)
        assert not result['ok'] and not result['applied'] and not result['actions']
        assert result['inferred']['platforms'] == [client]
        assert any('recorded-path manual recovery' in b for b in result['blockers'])
        assert receipt.read_bytes() == before and registry.read_bytes() == registry_before
        assert custom.read_text() == 'keep'


def test_roo_is_retained_without_fresh_projection():
    root = Path(__file__).resolve().parents[2]
    registry = load_client_registry(root)
    row = registry.variant('roo-code', 'roo-code-legacy').data
    assert row['kind'] == 'ide' and not row['executables']
    assert row['integration']['lifecycle'] == 'retained-only'
    assert row['integration']['qualification']['host'] == 'not-run'
    assert row['skills']['repo']['paths'] == ('.agents/skills',)
    assert row['skills']['global']['paths'] == ('~/.agents/skills',)
    assert 'roo-code-legacy' not in {p['id'] for p in platform_rows(registry)}
    with pytest.raises(ValueError, match='unknown platform selector'):
        build_install_plan(root, root / 'unused-home', platform_ids=['roo-code-legacy'])


def test_continue_cli_is_retained_native_without_fresh_projection():
    root = Path(__file__).resolve().parents[2]
    registry = load_client_registry(root)
    row = registry.variant('continue', 'continue-cli-legacy').data
    assert row['kind'] == 'cli'
    assert row['integration']['lifecycle'] == 'retained-only'
    assert row['integration']['qualification']['host'] == 'not-run'
    assert row['skills']['repo']['paths'] == ('.continue/skills',)
    assert row['skills']['global']['paths'] == ('~/.continue/skills',)
    assert 'continue-cli-legacy' not in {p['id'] for p in platform_rows(registry)}
    with pytest.raises(ValueError, match='unknown platform selector'):
        build_install_plan(root, root / 'unused-home', platform_ids=['continue-cli-legacy'])
