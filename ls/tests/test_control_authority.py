import copy
from contextlib import contextmanager
import json

import pytest

from ls.core.agent.control_authority import SECRET_ENV, launch_environment, selection
from ls.core.openpgp.secrets import SecretResolutionError


@pytest.fixture
def config(tmp_path):
    return {'agent': 'fixture', 'methods': ['status', 'cancel', 'reconcile'],
            'home': str(tmp_path/'keys'), 'trust_state': str(tmp_path/'trust.json'),
            'store_id': 'store', 'revision': 1, 'epoch': 0, 'scope': ['control'],
            'local_role': 'publisher', 'local_fingerprint': 'A'*40,
            'remote_role': 'owner', 'remote_fingerprint': 'B'*40,
            'passphrase': {'provider': 'env', 'name': 'GCONV_PATH'}}


def test_protected_credential_is_data_and_bound_to_exact_selection(config, tmp_path, monkeypatch):
    workspace = tmp_path/'workspace'
    monkeypatch.setenv('GCONV_PATH', 'fixture-secret')
    env = launch_environment(config, workspace)
    assert set(env) == {SECRET_ENV}
    monkeypatch.setenv(SECRET_ENV, env[SECRET_ENV])
    monkeypatch.setenv('GCONV_PATH', 'changed-ambient-value')
    selected = selection(config, workspace, protected=True)
    assert selected.peer.secret_resolver.resolve(selected.peer.passphrase_reference) == 'fixture-secret'
    assert selected.methods == frozenset({'status', 'cancel', 'reconcile'})
    changed = copy.deepcopy(config)
    changed['methods'].append('steer')
    with pytest.raises(PermissionError):
        selection(changed, workspace, protected=True)
    packet = json.loads(env[SECRET_ENV])
    packet['values']['OTHER'] = 'not-selected'
    monkeypatch.setenv(SECRET_ENV, json.dumps(packet))
    with pytest.raises(PermissionError):
        selection(config, workspace, protected=True)


def test_missing_secret_does_not_fall_back_to_ambient_environment(config, tmp_path, monkeypatch):
    monkeypatch.delenv('GCONV_PATH', raising=False)
    env = launch_environment(config, tmp_path/'workspace')
    monkeypatch.setenv(SECRET_ENV, env[SECRET_ENV])
    monkeypatch.setenv('GCONV_PATH', 'later-ambient-value')
    selected = selection(config, tmp_path/'workspace', protected=True)
    with pytest.raises(SecretResolutionError):
        selected.peer.secret_resolver.resolve(selected.peer.passphrase_reference)


@pytest.mark.parametrize('change', [
    {'methods': ['status', 'status']}, {'methods': ['execute']}, {'methods': []},
    {'scope': []}, {'epoch': True}, {'home': 'relative'},
    {'remote_fingerprint': 'A'*40}, {'extra': True},
    {'passphrase': {'provider': 'env', 'name': 'KEY', 'executable': '/bin/true'}},
    {'passphrase': {'provider': 'envman', 'name': 'KEY', 'executable': 'relative'}},
])
def test_invalid_admission_rejected(config, tmp_path, change):
    with pytest.raises((ValueError, TypeError)):
        selection(config | change, tmp_path/'workspace')


def test_workspace_cannot_supply_keys_trust_or_secret_executable(config, tmp_path):
    workspace = tmp_path/'workspace'
    for field in ('home', 'trust_state'):
        with pytest.raises(ValueError, match='separate'):
            selection(config | {field: str(workspace/field)}, workspace)
    config['passphrase'] = {'provider': 'envman', 'name': 'KEY',
                            'executable': str(workspace/'envman')}
    with pytest.raises(ValueError, match='separate'):
        selection(config, workspace)


def test_absent_admission_is_inert(tmp_path):
    assert selection(None, tmp_path) is None
    assert launch_environment(None, tmp_path) == {}


def test_cli_requires_explicit_exclusive_encrypted_channel(config, tmp_path):
    from types import SimpleNamespace
    from ls.core.agent.run_cli import _control_selection, _grant
    args = SimpleNamespace(encrypted_control_fd=8, control_fd=None, format='jsonl',
                           interactive=False, workspace=tmp_path/'workspace')
    value = {'schema_version': 1, 'read': [], 'write': [], 'disclose': [],
             'recipes': {}, 'remote_control': config}
    path = tmp_path/'grant.json'
    path.write_text(json.dumps(value)); path.chmod(0o600)
    assert _grant(path, args.workspace)[0] == value
    assert _control_selection(args, value).agent == 'fixture'
    with pytest.raises(ValueError):
        _control_selection(args, {})
    for change in ({'encrypted_control_fd': None}, {'control_fd': 9},
                   {'format': 'text'}, {'interactive': True}):
        with pytest.raises(ValueError):
            _control_selection(SimpleNamespace(**(vars(args) | change)), value)


def test_cli_launch_forwards_only_bound_secret_and_selected_descriptor(config, tmp_path, monkeypatch):
    import argparse
    import os
    from contextlib import contextmanager
    from ls.core.agent import run_cli, runtime_install
    from ls.core.agent.profiles import Profile
    from ls.core.agent.run_options import arguments
    path = tmp_path/'grant.json'
    path.write_text(json.dumps({'schema_version': 1, 'read': [], 'write': [],
                               'disclose': [], 'recipes': {}, 'remote_control': config}))
    path.chmod(0o600)
    profile = Profile('https://example.invalid/v1/', 'chat_completions', 'fixture', 'PROVIDER_KEY', 1, frozenset())
    monkeypatch.setattr(run_cli, 'load', lambda *_: profile)
    monkeypatch.setenv('PROVIDER_KEY', 'provider-fixture')
    monkeypatch.setenv('GCONV_PATH', 'selected-secret')
    @contextmanager
    def selected(*_, **__): yield tmp_path/'release'
    monkeypatch.setattr(runtime_install, 'selected', selected)
    class Captured(Exception): pass
    with _sockets() as (left, right):
        parser = argparse.ArgumentParser(); arguments(parser)
        args = parser.parse_args(['--profile', 'fixture', '--grant', str(path),
            '--workspace', str(tmp_path/'workspace'), '--resource-parent', str(tmp_path/'resource'),
            '--prompt-stdin', '--format', 'jsonl', '--encrypted-control-fd', str(right.fileno())])
        def execute(_path, _argv, environment):
            assert os.get_inheritable(right.fileno())
            assert set(environment) == {'PATH', 'LANG', run_cli._CREDENTIAL, run_cli._PROFILE, SECRET_ENV}
            with monkeypatch.context() as patch:
                patch.setattr(os, 'environ', environment)
                admission = selection(config, args.workspace, protected=True)
                assert admission.peer.secret_resolver.resolve(admission.peer.passphrase_reference) == 'selected-secret'
            raise Captured
        monkeypatch.setattr(os, 'execve', execute)
        with pytest.raises(Captured):
            run_cli.launch([], args)
        assert not os.get_inheritable(right.fileno())


@contextmanager
def _sockets():
    import socket
    left, right = socket.socketpair()
    try:
        yield left, right
    finally:
        left.close(); right.close()


def test_execute_connects_admitted_scope_to_coding_owner(config, tmp_path, monkeypatch, capfd):
    import argparse
    import threading
    import time
    from types import SimpleNamespace
    from ls.core.agent import run_cli, coding_run, control_run
    from ls.core.agent.profiles import Profile, wire
    from ls.core.agent.coding_protocol import profile_digest
    from ls.core.agent.run_options import arguments, defaults
    from ls.core.agent.run_io import Streams
    workspace = tmp_path/'workspace'; workspace.mkdir()
    path = tmp_path/'grant.json'
    path.write_text(json.dumps({'schema_version': 1, 'read': [], 'write': [],
                               'disclose': [], 'recipes': {}, 'remote_control': config}))
    path.chmod(0o600)
    parser = argparse.ArgumentParser(); arguments(parser)
    args = defaults(parser.parse_args(['--profile', 'fixture', '--profiles', str(tmp_path/'profiles'),
        '--grant', str(path), '--workspace', str(workspace), '--runtime-root', str(tmp_path/'runtime'),
        '--state-root', str(tmp_path/'state'), '--resource-parent', str(tmp_path/'resources'),
        '--prompt-stdin', '--format', 'jsonl', '--encrypted-control-fd', '8']))
    profile = Profile('https://example.invalid/v1/', 'chat_completions', 'fixture', 'KEY', 1, frozenset({'tools', 'streaming'}))
    monkeypatch.setattr(run_cli, 'load', lambda *_: profile)
    monkeypatch.setenv(run_cli._PROFILE, profile_digest(wire(profile)))
    monkeypatch.setenv(run_cli._CREDENTIAL, 'fixture')
    monkeypatch.setenv(SECRET_ENV, launch_environment(config, workspace)[SECRET_ENV])
    cancelled = threading.Event()
    owner = object()
    seen = []
    @contextmanager
    def listen(fd, admission, held, run_id, **kwargs):
        assert fd == 8 and admission.agent == 'fixture' and held is owner
        assert kwargs['cancelled'] is cancelled
        kwargs['ready']({'encoding': 'base64', 'envelope': 'fixture'})
        seen.append(run_id)
        yield
    def run(_paths, payload, _authority, _files, _recipes, **kwargs):
        with kwargs['control_scope'](owner, payload['run_id']):
            assert seen == [payload['run_id']]
        return SimpleNamespace(status='completed', data=None)
    monkeypatch.setattr(control_run, 'listen', listen)
    monkeypatch.setattr(coding_run, 'run_coding', run)
    streams = Streams(time.monotonic()+10, cancelled)
    monkeypatch.setattr(streams, 'prompt', lambda: 'fixture prompt')
    assert run_cli.execute(args, streams, cancelled) == 0
    output = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    assert [item['type'] for item in output] == ['start', 'control_ready', 'result']
