from pathlib import Path

import pytest
import yaml

from ls.core.client_registry import ClientRegistryError, load_client_registry
from ls.tests.test_client_registry import _copy_config


def metadata():
    return {'lifecycle': 'active', 'installation': {'method': 'manual', 'instructions': 'Use the vendor installation guide.'},
            'qualification': {'catalog': 'bounded', 'filesystem': 'not-run', 'host': 'not-run',
                              'evidence': [{'kind': 'documentation', 'reference': 'ls/docs/CLIENT_STATE.md'}]},
            'limitations': ['No host execution has been performed.']}


def write(root, change):
    path = root / 'ls/config/clients.yaml';data = yaml.safe_load(path.read_text())
    row = next(v for f in data['families'] if f['id'] == 'antigravity' for v in f['variants'] if v['id'] == 'antigravity-ide')
    row['integration'] = metadata()
    change(row, data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_registry_records_application_without_claiming_host_qualification(tmp_path):
    root = _copy_config(tmp_path)
    write(root, lambda row, data: row.update(kind='application'))
    variant = load_client_registry(root).variant('antigravity', 'antigravity-ide')
    assert variant.data['kind'] == 'application'
    assert variant.data['integration']['qualification']['host'] == 'not-run'


@pytest.mark.parametrize('surface', ['filesystem', 'host'])
def test_verified_qualification_requires_appropriate_evidence(tmp_path, surface):
    root = _copy_config(tmp_path)
    write(root, lambda row, data: row['integration']['qualification'].update({surface: 'verified'}))
    with pytest.raises(ClientRegistryError, match=f'verified {surface}'):
        load_client_registry(root)


@pytest.mark.parametrize('reference', ['.agents/state/audit.md', '../private.md', '/tmp/evidence.md', 'https://user:secret@example.com/evidence', 'state/evidence.md'])
def test_private_evidence_syntax_is_rejected(tmp_path, reference):
    root = _copy_config(tmp_path)
    write(root, lambda row, data: row['integration']['qualification']['evidence'][0].update(reference=reference))
    with pytest.raises(ClientRegistryError, match='public repository path'):
        load_client_registry(root)


def test_retained_client_cannot_project_fresh_install_surface(tmp_path):
    root = _copy_config(tmp_path)
    def change(row, data):
        supported = next(v for f in data['families'] for v in f['variants'] if 'compatibility' in v)
        supported['integration'] = metadata()
        supported['integration']['lifecycle'] = 'retained-only'
    write(root, change)
    with pytest.raises(ClientRegistryError, match='cannot project fresh-install'):
        load_client_registry(root)


def test_bounded_qualification_requires_limitation(tmp_path):
    root = _copy_config(tmp_path)
    write(root, lambda row, data: row['integration'].update(limitations=[]))
    with pytest.raises(ClientRegistryError, match='requires limitations'):
        load_client_registry(root)


def test_kimi_profile_bounds_legacy_python_without_retargeting_projection():
    root = Path(__file__).resolve().parents[2]
    registry = load_client_registry(root)
    variant = registry.variant('kimi', 'kimi-cli').data
    integration = variant['integration']
    limitations = ' '.join(integration['limitations'])

    assert variant['display_name'] == 'Kimi CLI (legacy Python)'
    assert variant['executables'] == ('kimi',)
    assert variant['config']['global']['paths'] == ('~/.kimi/config.toml',)
    assert variant['compatibility']['platform_id'] == 'kimi-cli'
    family = next(family for family in registry.families if family.family_id == 'kimi')
    assert tuple(item.variant_id for item in family.variants) == ('kimi-cli',)
    assert variant['support_status'] == 'supported'
    assert integration['lifecycle'] == 'active'
    assert integration['qualification']['catalog'] == 'bounded'
    assert integration['qualification']['filesystem'] == 'verified'
    assert integration['qualification']['host'] == 'not-run'
    assert 'active lifecycle describes LocalSetup adapter support' in limitations
    assert 'Kimi Code' in limitations and '~/.kimi-code' in limitations
