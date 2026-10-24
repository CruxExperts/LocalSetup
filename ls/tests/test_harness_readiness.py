"""Readiness distinguishes static evidence from explicit bounded execution."""
import json
from pathlib import Path
import sys

import pytest
import yaml

from ls.core import harness_probe, harness_readiness as owner
from ls.core.harness_configuration import configure
from ls.core.agent.completion_contract import envelope
from ls.tests.codex_heartbeat_test_helpers import ROOT
from ls.tests.test_harness_configuration import setup, runtime
import heartbeat_review


@pytest.fixture
def configured(setup, monkeypatch):
    target, path, options = setup
    options['grant'].write_text(json.dumps(dict(schema_version=1, read=['health.txt'], write=[],
                                               disclose=['health.txt'], recipes={})))
    options['grant'].chmod(0o600)
    configure(ROOT, target, **options, apply=True)
    value = yaml.safe_load(path.read_text())
    value['agent']['enabled'] = False
    path.write_text(yaml.safe_dump(value))
    monkeypatch.setattr(owner.runtime_diagnostics, 'runtime', lambda *_: {
        'status': 'verified', 'dependencies': {'status': 'verified'},
        'native_sandbox': {'status': 'missing'}})
    return target, path, options


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_static_readiness_is_noncreating_without_credentials_context_or_hooks(configured, monkeypatch):
    target, path, options = configured
    monkeypatch.setattr(harness_probe, 'run', lambda *_: pytest.fail('inspection cannot probe'))
    before = snapshot(target.parent)
    result = owner.inspect(ROOT, target)
    assert result['ok'] and result['status'] == 'static_verified'
    assert result['activation'] == dict(agent_enabled=False, heartbeat_enabled=False, scheduled_agent=False)
    assert result['credential_source'] == 'env'
    assert result['probe']['status'] == 'not_run'
    assert {'context_contents', 'credentials', 'provider_execution', 'live_cron'} <= set(result['unverified'])
    assert snapshot(target.parent) == before and not (target / 'health.txt').exists()


@pytest.mark.parametrize('case,blocked', [('missing', 'configuration'), ('selection', 'selection'),
    ('runtime', 'runtime'), ('provider', 'provider_profile'), ('disclose', 'grant'), ('private', 'grant'),
    ('launcher', 'launcher'), ('context', 'grant')])
def test_static_blockers_are_actionable_and_never_probe(configured, monkeypatch, case, blocked):
    target, path, options = configured
    value = yaml.safe_load(path.read_text())
    selected = value['agent_profiles']['lscli-review']
    if case == 'selection': value['agent']['profile'] = 'unknown'
    if case == 'runtime': monkeypatch.setattr(owner.runtime_diagnostics, 'runtime', lambda *_: {'status': 'missing'})
    if case == 'provider': selected['profile'] = 'absent'
    if case == 'context': selected['context'] = ['.env']
    if case == 'launcher': selected['token_limit'] = 0
    path.write_text(yaml.safe_dump(value))
    if case == 'missing': path.unlink()
    if case == 'disclose':
        grant = json.loads(options['grant'].read_text()); grant['disclose'] = []
        options['grant'].write_text(json.dumps(grant))
    if case == 'private': options['grant'].chmod(0o644)
    monkeypatch.setattr(harness_probe, 'run', lambda *_: pytest.fail('blocked checks cannot dispatch'))
    before = snapshot(target.parent)
    result = owner.inspect(ROOT, target, probe=True)
    assert not result['ok'] and result['status'] == 'blocked'
    assert any(c['name'] == blocked and c['status'] == 'blocked' and c['next_step'] for c in result['checks'])
    assert snapshot(target.parent) == before


def test_errors_do_not_echo_profile_or_secret_diagnostics(configured, monkeypatch):
    target, _, _ = configured
    def fail(*_args):
        raise ValueError('PrivateDiagnosticCanary9')
    monkeypatch.setattr(owner.profiles, 'load', fail)
    result = owner.inspect(ROOT, target)
    assert not result['ok'] and 'PrivateDiagnosticCanary9' not in json.dumps(result)


@pytest.mark.parametrize('outcome', ['success', 'malformed', 'failure'])
def test_probe_is_one_attempt_with_private_output_discarded(configured, monkeypatch, outcome):
    target, path, options = configured
    marker = target.parent / 'attempts'
    provider = owner.profiles.load(options['profiles'], 'provider')
    response = envelope('succeeded', model=provider.model,
                        data={'summary': 'PrivateModelCanary9', 'findings': []}, attempts=1)
    if outcome == 'failure': response = envelope('unavailable')
    raw = 'PrivateMalformedCanary9' if outcome == 'malformed' else json.dumps(response)
    program = f'''import json,sys
from pathlib import Path
p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')
r=json.load(sys.stdin)
assert r['max_attempts']==1 and r['max_output_tokens']<=256 and r['deadline_seconds']<=30
assert '--context=health.txt' in sys.argv
print({raw!r})
'''
    monkeypatch.setattr(heartbeat_review, '_resolve', lambda *_: [sys.executable, '-c', program])
    before = path.read_bytes()
    result = owner.inspect(ROOT, target, probe=True)
    assert marker.read_text() == 'x' and path.read_bytes() == before
    assert result['ok'] is (outcome == 'success')
    assert result['probe']['status'] == ('passed' if outcome == 'success' else 'failed')
    assert 'Canary9' not in json.dumps(result)
    assert result['schedule_changed'] is False
    if outcome == 'success':
        assert 'credentials' not in result['unverified'] and 'live_cron' in result['unverified']
    else:
        assert result['probe']['provider_dispatch'] == 'possible'


def test_coding_probe_bounds_and_missing_native_have_no_implicit_fallback(configured, monkeypatch):
    target, path, options = configured
    options.update(mode='coding', context=None, resource_parent='/sys/fs/cgroup/explicit')
    configure(ROOT, target, **options, apply=True)
    value = yaml.safe_load(path.read_text())
    selected = {key: value[key] for key in ('agent', 'agent_profiles')}
    before = json.dumps(selected, sort_keys=True)
    command = harness_probe.plan(runtime, selected, target)
    argv = command['command']
    assert argv[argv.index('--tool-limit') + 1] == '0'
    assert argv[argv.index('--request-limit') + 1] == '1'
    assert argv[argv.index('--timeout') + 1] == '30'
    assert argv[argv.index('--token-limit') + 1] == '4096'
    assert json.dumps(selected, sort_keys=True) == before
    monkeypatch.setattr(owner, '_coding_paths', lambda *_: None)
    monkeypatch.setattr(harness_probe, 'run', lambda *_: pytest.fail('missing native must not execute or fall back'))
    result = owner.inspect(ROOT, target, probe=True)
    assert result['status'] == 'blocked' and result['mode'] == 'coding'
    assert any(c['name'] == 'native_bundle' and c['status'] == 'blocked' for c in result['checks'])


def test_readiness_cli_keeps_probe_explicit(configured, capsys):
    from ls.core.cli import main
    target, _, _ = configured
    assert main(['--source-root', str(ROOT), 'harness', 'codex-heartbeat', 'readiness',
                 '--target-directory', str(target)]) == 0
    assert json.loads(capsys.readouterr().out)['probe']['status'] == 'not_run'
