"""Configuration previews preserve scheduling authority and backed-up user data."""
import gzip
import json
from pathlib import Path

import pytest
import yaml

from ls.core import harness_configuration as owner
from ls.tests.codex_heartbeat_test_helpers import ROOT, load_runtime, state_root, write_config
from ls.tests.test_agent_provider import config

runtime = load_runtime()
import heartbeat_lscli
import heartbeat_review


@pytest.fixture
def setup(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    target = tmp_path / 'target'
    target.mkdir(mode=0o700)
    path = write_config(target, enabled=False, hooks={'before': [{'command': ['collect']}], 'after': []},
                        agent_profiles={'custom': {'command': ['untouched']}})
    path.parent.chmod(0o700)
    path.chmod(0o600)
    profiles = tmp_path / 'profiles.json'
    profiles.write_text(json.dumps({'schema_version': 1, 'profiles': {'provider': config()}}))
    profiles.chmod(0o600)
    monkeypatch.setattr(heartbeat_lscli, '_resolve', lambda *_: ['/qualified/lscli'])
    monkeypatch.setattr(heartbeat_review, '_resolve', lambda *_: ['/qualified/lscli'])
    from ls.core.agent import profile_credentials
    monkeypatch.setattr(profile_credentials, 'resolve', lambda *_: pytest.fail('configuration cannot resolve secrets'))
    options = dict(mode='review', provider_profile='provider', grant=tmp_path / 'grant.json',
                   context=['health.txt'], executable=tmp_path / 'lscli', profiles=profiles,
                   runtime_root=tmp_path / 'runtime')
    return target, path, options


def test_preview_apply_backup_and_idempotence_preserve_user_configuration(setup):
    target, path, options = setup
    original = path.read_bytes()
    old = yaml.safe_load(original)
    before = set(target.rglob('*'))
    preview = owner.configure(ROOT, target, **options)
    assert preview['status'] == 'planned' and preview['changed']
    assert path.read_bytes() == original and set(target.rglob('*')) == before
    assert not (target / 'health.txt').exists()  # No context or grant reads in preview.
    applied = owner.configure(ROOT, target, **options, apply=True, expected_config_sha256=preview['config_sha256'])
    backup = Path(applied['backup'])
    assert gzip.decompress(backup.read_bytes()) == original
    assert backup.stat().st_mode & 0o777 == path.stat().st_mode & 0o777 == 0o600
    value = yaml.safe_load(path.read_bytes())
    assert value['heartbeat'] == old['heartbeat'] and value['hooks'] == old['hooks']
    assert value['agent_profiles']['custom'] == old['agent_profiles']['custom']
    assert value['agent'] == {'enabled': True, 'profile': 'lscli-review'}
    assert applied['schedule_changed'] is False and not (target / 'cron').exists()
    before = set(target.rglob('*'))
    again = owner.configure(ROOT, target, **options, apply=True)
    assert again['status'] == 'unchanged' and again['backup'] is None
    assert set(target.rglob('*')) == before


@pytest.mark.parametrize('case', ['digest', 'recurring', 'boolean', 'custom', 'profile', 'context', 'missing', 'symlink', 'shared'])
def test_invalid_or_unsafe_configuration_is_preserved(setup, case):
    target, path, options = setup
    value = yaml.safe_load(path.read_bytes())
    if case == 'digest': options['expected_config_sha256'] = '0' * 64
    if case == 'recurring': value['heartbeat'].update(enabled=True, scheduled_agent=True)
    if case == 'boolean': value['heartbeat']['scheduled_agent'] = 'false'
    if case == 'custom': value['agent_profiles']['lscli-review'] = {'launcher': 'custom'}
    if case == 'profile': options['provider_profile'] = 'unknown'
    if case == 'context': options['context'] = ['../outside']
    path.write_text(yaml.safe_dump(value))
    if case == 'shared': path.chmod(0o660)
    if case == 'symlink':
        original = path.with_suffix('.original')
        path.rename(original)
        path.symlink_to(original)
    if case == 'missing': path.unlink()
    before = path.read_bytes() if path.exists() else None
    with pytest.raises((ValueError, OSError, runtime.HeartbeatError)):
        owner.configure(ROOT, target, **options, apply=True)
    assert (path.read_bytes() if path.exists() else None) == before
    assert not state_root(target).exists()


def test_custom_limits_and_collection_only_mode_survive_reconfiguration(setup):
    target, path, options = setup
    owner.configure(ROOT, target, **options, apply=True)
    value = yaml.safe_load(path.read_bytes())
    value['heartbeat'].update(enabled=True, scheduled_agent=False)
    value['agent']['timeout_seconds'] = 27
    value['agent_profiles']['lscli-review'].update(prompt='Keep this task', token_limit=128)
    path.write_text(yaml.safe_dump(value))
    options['context'] = ['other.txt']
    owner.configure(ROOT, target, **options, apply=True)
    current = yaml.safe_load(path.read_bytes())
    assert current['heartbeat'] == value['heartbeat']
    assert current['agent']['timeout_seconds'] == 27
    assert current['agent_profiles']['lscli-review']['prompt'] == 'Keep this task'
    assert current['agent_profiles']['lscli-review']['token_limit'] == 128
    current['heartbeat']['scheduled_agent'] = True
    path.write_text(yaml.safe_dump(current))
    assert owner.configure(ROOT, target, **options, apply=True)['status'] == 'unchanged'


def test_coding_requires_explicit_resources_and_validates_provider(setup):
    target, path, options = setup
    options.update(mode='coding', context=None)
    with pytest.raises(ValueError, match='resource-parent'):
        owner.configure(ROOT, target, **options)
    options['resource_parent'] = '/sys/fs/cgroup/explicit'
    options['provider_profile'] = 'absent'
    with pytest.raises(ValueError):
        owner.configure(ROOT, target, **options)
    options['provider_profile'] = 'provider'
    result = owner.configure(ROOT, target, **options)
    assert result['agent']['profile'] == 'lscli-heartbeat'
    assert result['selected_profile']['tool_limit'] == 16


def test_changed_configuration_during_backup_is_not_overwritten(setup, monkeypatch):
    target, path, options = setup
    original = path.read_bytes()
    read = owner.read
    calls = 0
    def racing_read(item):
        nonlocal calls
        calls += 1
        if calls == 3:
            path.write_bytes(original + b'\n# Concurrent edit\n')
        return read(item)
    monkeypatch.setattr(owner, 'read', racing_read)
    with pytest.raises(ValueError, match='backup retained'):
        owner.configure(ROOT, target, **options, apply=True)
    assert path.read_bytes().endswith(b'# Concurrent edit\n')
    backups = list(state_root(target).glob('configuration-backups/*.gz'))
    assert len(backups) == 1 and gzip.decompress(backups[0].read_bytes()) == original
    assert not list(path.parent.glob('.heartbeat-*'))


def test_public_cli_reaches_configuration_owner(setup, capsys):
    from ls.core.cli import main
    target, path, options = setup
    argv = ['--source-root', str(ROOT), 'harness', 'codex-heartbeat', 'configure-lscli',
            '--target-directory', str(target), '--mode', 'review', '--provider-profile', 'provider',
            '--grant', str(options['grant']), '--profiles', str(options['profiles']), '--context', 'health.txt']
    before = path.read_bytes()
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'planned'
    assert path.read_bytes() == before
