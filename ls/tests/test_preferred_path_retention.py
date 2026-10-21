"""Changing fresh write preferences must not reselect recorded adapters."""
import json
from pathlib import Path

import pytest
import yaml

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.client_registry import load_client_registry, write_platforms_projection
from ls.core.detach import detach_platforms
from ls.core.personal_detach import detach_personal
from ls.core.plan import build_install_plan
from ls.core.repair import run_repair
from ls.tests.test_install_flow import make_temp_repo

pytestmark = pytest.mark.usefixtures('default_opencode_environment')


def prefer_common(root, *, historical=False, client="cursor"):
    path = root / 'ls/config/clients.yaml';data = yaml.safe_load(path.read_text())
    family = next(f for f in data['families'] if f['id'] == client)
    row = next(v for v in family['variants'] if v['id'] == ('cursor-ide' if client == 'cursor' else client + '-cli'))
    native = ['.agents/skills', '.cursor/skills'] if client == 'cursor' else [f'.{client}/skills']
    row['compatibility']['repo_write_paths'] = native if historical else ['.agents/skills']
    personal = ['~/.config/opencode/skills'] if client == 'opencode' else ['~/' + path for path in native]
    row['compatibility']['global_write_paths'] = personal if historical else ['~/.agents/skills']
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    write_platforms_projection(root, load_client_registry(root))


@pytest.mark.parametrize('client', ['cursor', 'kilo', 'opencode'])
@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
@pytest.mark.parametrize('mode', ['symlink', 'portable'])
def test_preferred_subset_preserves_recorded_dual_adapters(tmp_path, monkeypatch, capsys, scope, mode, client):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    prefer_common(root, historical=True, client=client)
    monkeypatch.setattr(cli, '_is_global_shim_invocation', lambda: False)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'],
        platform_ids=[client], skill_scope=scope, attach_mode=mode), home)
    receipt = root / '.localsetup/lock.json';before = json.loads(receipt.read_text())
    bases = (root,) if scope == 'repo' else (home,) if scope == 'personal' else (root, home)
    adapters = []
    for base in bases:
        paths = ('.agents/skills', '.cursor/skills') if client == 'cursor' else (
            '.config/opencode/skills' if client == 'opencode' and base == home else f'.{client}/skills',)
        adapters.extend(base / rel for rel in paths)
    for adapter in adapters:(adapter / 'custom.txt').write_text('keep')
    prefer_common(root, client=client)
    prefix = ['--source-root', str(root), '--home', str(home)]
    assert cli.main(prefix + ['plan']) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == f'recorded_{scope}'
    assert json.loads(receipt.read_text()) == before
    assert cli.main(prefix + ['update']) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == f'recorded_{scope}'
    after = json.loads(receipt.read_text())
    assert after['platforms'] == [client] and after['skill_scope'] == scope
    assert after['adapter_targets'] == before['adapter_targets']
    for current, old in zip(after['personal_adapter_targets'], before['personal_adapter_targets']):
        assert all(current[key] == value for key, value in old.items())
    assert len(after['personal_adapter_targets']) == len(before['personal_adapter_targets'])
    if scope == 'repo':
        for apply in (False, True):
            report = run_repair(root, home=home, target_root=root, apply=apply)
            assert not report['ok'] and not report['applied'] and not report['actions']
            assert any('recorded-path manual recovery' in b for b in report['blockers'])
        detach_platforms(root, home, root, [client])
    elif scope == 'personal':
        assert detach_personal(root, home, [client], apply=True)['applied']
    else:
        detach_platforms(root, home, root, [client])
        assert detach_personal(root, home, [client], apply=True)['applied']
    for adapter in adapters:
        assert (adapter / 'custom.txt').read_text() == 'keep'
        assert not (adapter / 'ls-localsetup-project-context').exists()


def test_current_preferred_paths_still_allow_normal_repository_repair(tmp_path):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    prefer_common(root)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor']), home)
    package = root / '.agents/skills/ls-localsetup-project-context';package.unlink()
    report = run_repair(root, home=home, target_root=root, apply=True)
    assert report['ok'] and report['applied'] and package.is_symlink()
    assert not (root / '.cursor/skills').exists()


@pytest.mark.parametrize('with_selector', [False, True])
@pytest.mark.parametrize('path_field', ['adapter_state', 'adapter_paths'])
def test_legacy_path_fields_refuse_fresh_inference_before_mutation(tmp_path, path_field, with_selector):
    from ls.core.retained_update import retained_repository_plan
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    prefer_common(root)
    lock = root / 'localsetup.lock.json'
    legacy = {path_field: ['.cursor/skills']}
    if with_selector:legacy['tools'] = ['cursor']
    lock.write_text(json.dumps(legacy))
    before = lock.read_bytes()
    native = root / '.cursor/skills/custom.txt';native.parent.mkdir(parents=True);native.write_text('keep')
    for apply in (False, True):
        report = run_repair(root, home=home, target_root=root, apply=apply)
        assert not report['ok'] and not report['applied'] and not report['actions']
        assert any('recorded-path manual recovery' in b for b in report['blockers'])
        assert lock.read_bytes() == before and native.read_text() == 'keep'
    with pytest.raises(ValueError, match='Legacy recorded adapters'):
        retained_repository_plan(root, home, root)
    assert not (root / '.agents/skills').exists()


def test_explicit_empty_modern_ownership_does_not_fall_back_to_legacy(tmp_path):
    from ls.core.retained_update import recorded_preferred_path_clients
    root = make_temp_repo(tmp_path);prefer_common(root)
    legacy = {'platforms': ['cursor'], 'adapter_targets': [], 'adapter_paths': ['.cursor/skills']}
    assert recorded_preferred_path_clients(root, legacy, root) == []
    legacy = {'platforms': [], 'tools': ['cursor'], 'adapter_paths': ['.cursor/skills']}
    assert recorded_preferred_path_clients(root, legacy, root) == []

    legacy = {'adapter_paths': [str(root / '.cursor/skills/ls-localsetup-project-context') + '/']}
    assert recorded_preferred_path_clients(root, legacy, root) == ['cursor']


def test_generic_discovery_root_is_anchored_to_recorded_target(tmp_path):
    from ls.core.retained_update import recorded_preferred_path_clients
    root = make_temp_repo(tmp_path)
    assert recorded_preferred_path_clients(root, {'adapter_paths': ['skills/ls-localsetup-project-context']}, root) == ['openclaw']
    assert 'openclaw' not in recorded_preferred_path_clients(root, {'adapter_paths': ['.cursor/skills/ls-localsetup-project-context']}, root)
    assert recorded_preferred_path_clients(root, {'adapter_paths': [str(tmp_path / 'outside/skills/ls-localsetup-project-context')]}, root) == []
    assert recorded_preferred_path_clients(root, {'adapter_paths': ['../skills/ls-localsetup-project-context']}, root) == []
