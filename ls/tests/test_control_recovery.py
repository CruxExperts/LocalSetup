from contextlib import contextmanager
import hashlib
import socket
import threading
import time

import pytest

from ls.core.agent.control_authority import Admission
from ls.core.agent.control_envelope import RemotePeer
from ls.core.agent.control_recovery import target
from ls.core.agent.control_run import listen
from ls.core.agent.control_transport import Stream
from ls.core.agent.remote_control import (seal_request, open_receipt, seal_reconcile,
                                         open_reconciliation, open_rejection)
from ls.core.agent.session_owner import lease
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE
from ls.tests.test_control_authority import config


def test_recovery_after_owner_exit_is_read_only(tmp_path, separate_machine_env):
    env = separate_machine_env
    def peer(role, local_role, local, remote_role, remote):
        snapshot = env.snapshots[role]
        return RemotePeer(env.homes[role], env.paths[role], snapshot.store_id,
            snapshot.revision, snapshot.epoch, env.policies[role].scope, local_role,
            local, remote_role, remote, SECRET_REFERENCE, env.resolver)
    controller = peer('controller', 'owner', env.owner, 'publisher', env.publisher)
    agent = peer('origin', 'publisher', env.publisher, 'owner', env.owner)
    sessions, workspace = tmp_path/'sessions', tmp_path/'workspace'
    sessions.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    expires = time.monotonic()+120
    raw = b'{"schema_version":1,"id":1,"method":"status"}'
    request_id = 'a'*32
    with lease(sessions, task='task', session='session', workspace=workspace,
               expires=expires) as owner:
        with _connection(expires) as (client, remote):
            with listen(remote.fileno(), Admission('agent', frozenset({'status'}), agent),
                        owner, 'run', cancelled=threading.Event(), ready=lambda _: None,
                        nonce='b'*32) as original:
                cipher = seal_request(controller, original, request_id, raw)
                receipt = client.exchange('execute', request_id, cipher)
                assert open_receipt(controller, original, request_id, receipt, request=raw)['result']['status'] == 'active'
        root = owner.root/'controls/run'
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*.json')}
    cancelled = threading.Event()
    with lease(sessions, task='task', session='session', workspace=workspace,
               expires=expires, revoked=cancelled, create=False) as successor:
        with pytest.raises(PermissionError):
            target(successor, 'run', '0'*64)
        with _connection(expires) as (client, remote):
            with listen(remote.fileno(), Admission('agent', frozenset({'reconcile'}), agent),
                        successor, 'run', cancelled=cancelled, ready=lambda _: None,
                        nonce='c'*32, recover_digest=original.digest()) as current:
                assert current.owner_generation != original.owner_generation
                assert current.operation == original.operation
                args = dict(target_binding=original, request_sha256=hashlib.sha256(raw).hexdigest(), query_id='d'*32)
                query = seal_reconcile(controller, current, request_id, **args)
                reply = client.exchange('reconcile', request_id, query)
                assert open_reconciliation(controller, current, request_id, reply, **args)['state'] == 'completed'
                cancel = b'{"schema_version":1,"id":2,"method":"cancel"}'
                cipher = seal_request(controller, current, 'e'*32, cancel)
                reply = client.exchange('execute', 'e'*32, cipher)
                open_rejection(controller, current, 'e'*32, reply, request_ciphertext=cipher)
                assert not cancelled.is_set()
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*.json')}


@contextmanager
def _connection(expires):
    left, right = socket.socketpair()
    fd = left.detach()
    client = Stream(fd, fd, expires=expires)
    try:
        yield client, right
    finally:
        client.close()
        right.close()


def test_registered_recovery_uses_selected_runtime(tmp_path, monkeypatch):
    from ls.core.agent import cli, control_recovery_cli
    captured = []
    monkeypatch.setattr(control_recovery_cli, 'launch', lambda argv, args: captured.append((argv, args)) or 0)
    args = ['control-recover', '--task', 'task', '--session', 'session', '--run', 'run',
            '--binding-sha256', 'a'*64, '--control-nonce', 'b'*32, '--grant', str(tmp_path/'grant'),
            '--workspace', str(tmp_path/'workspace'), '--encrypted-control-fd', '3']
    assert cli.main(args, default_runtime_root=tmp_path/'runtime') == 0
    assert captured[0][1].runtime_root == tmp_path/'runtime'
    assert captured[0][0][-2:] == ['--runtime-root', str(tmp_path/'runtime')]


@pytest.mark.parametrize('damage', ['none', 'digest', 'symlink', 'permissions', 'run', 'task'])
def test_historical_target_validates_private_evidence(tmp_path, damage):
    from ls.core.agent.control_envelope import ControlBinding
    from ls.core.agent.runtime_install import _write_json
    sessions, workspace = tmp_path/'sessions', tmp_path/'workspace'
    sessions.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    with lease(sessions, task='task', session='session', workspace=workspace,
               expires=time.monotonic()+10) as owner:
        root = owner.root/'controls/run'
        root.mkdir(parents=True, mode=0o700)
        root.parent.chmod(0o700)
        (root/'journal').mkdir(mode=0o700)
        binding = ControlBinding('agent', 'machine', ('scope',),
            'other' if damage == 'task' else 'task', 'session',
            'other' if damage == 'run' else 'run', 'operation', 'old-owner', 1, 2)
        path = root/'binding.json'
        _write_json(path, binding.value())
        digest = binding.digest()
        if damage == 'digest':
            digest = '0'*64
        elif damage == 'symlink':
            path.rename(root/'original.json')
            path.symlink_to(root/'original.json')
        elif damage == 'permissions':
            path.chmod(0o644)
        if damage == 'none':
            recovered, _ = target(owner, 'run', digest)
            assert recovered == binding  # Expired identity remains readable evidence.
        else:
            with pytest.raises((OSError, ValueError)):
                target(owner, 'run', digest)


def _recovery_args(tmp_path, config):
    import argparse
    import json
    from ls.core.agent.control_recovery_cli import arguments, defaults
    path = tmp_path/'grant.json'
    path.write_text(json.dumps(dict(schema_version=1, read=[], write=[], disclose=[],
                                   recipes={}, remote_control=config)))
    path.chmod(0o600)
    parser = argparse.ArgumentParser()
    arguments(parser)
    return defaults(parser.parse_args(['--task', 'task', '--session', 'session', '--run', 'run',
        '--binding-sha256', 'a'*64, '--control-nonce', 'b'*32, '--grant', str(path),
        '--workspace', str(tmp_path/'workspace'), '--runtime-root', str(tmp_path/'runtime'),
        '--state-root', str(tmp_path/'state'), '--encrypted-control-fd', '3']))


@pytest.mark.parametrize('change', ['read', 'write', 'disclose', 'recipes', 'methods', 'extra'])
def test_recovery_rejects_broader_authority(config, tmp_path, change):
    import json
    from ls.core.agent.control_recovery_cli import admission
    config['methods'] = ['reconcile']
    args = _recovery_args(tmp_path, config)
    value = json.loads(args.grant.read_text())
    if change in {'read', 'write', 'disclose'}:
        value[change] = ['allowed.txt']
    elif change == 'recipes':
        value[change] = {'fixture': {'command': ['/usr/bin/true'], 'files': ['input.txt'], 'seconds': 1}}
    elif change == 'methods':
        value['remote_control']['methods'].append('cancel')
    else:
        value['openpgp'] = None
    args.grant.write_text(json.dumps(value))
    with pytest.raises(PermissionError):
        admission(args)


def test_recovery_launch_forwards_only_selected_secret(config, tmp_path, monkeypatch):
    import os
    from ls.core.agent import control_recovery_cli, runtime_install
    from ls.core.agent.control_authority import SECRET_ENV
    config['methods'] = ['reconcile']
    args = _recovery_args(tmp_path, config)
    monkeypatch.setenv('GCONV_PATH', 'fixture-secret')
    monkeypatch.setenv('PROVIDER_KEY', 'must-not-inherit')
    @contextmanager
    def selected(root, **kwargs):
        assert root == args.runtime_root and kwargs == {'timeout': 5, 'create': False}
        yield tmp_path/'release'
    monkeypatch.setattr(runtime_install, 'selected', selected)
    class Captured(Exception): pass
    with _connection(time.monotonic()+10) as (_, remote):
        args.encrypted_control_fd = remote.fileno()
        def execute(path, argv, environment):
            assert path == tmp_path/'release/venv/bin/python'
            assert argv[1:5] == ['-I', '-B', '-m', 'ls.core.agent.control_recovery_cli']
            assert set(environment) == {'PATH', 'LANG', SECRET_ENV}
            assert os.get_inheritable(remote.fileno())
            with monkeypatch.context() as patch:
                patch.setattr(os, 'environ', environment)
                _, admitted = control_recovery_cli.admission(args, protected=True)
                assert admitted.peer.secret_resolver.resolve(admitted.peer.passphrase_reference) == 'fixture-secret'
            raise Captured
        monkeypatch.setattr(os, 'execve', execute)
        with pytest.raises(Captured):
            control_recovery_cli.launch([], args)
        assert not os.get_inheritable(remote.fileno())


def test_recovery_rejects_runtime_switch_before_session_access(config, tmp_path, monkeypatch):
    from ls.core.agent import control_recovery_cli, runtime_install
    from ls.core.agent.control_authority import SECRET_ENV, launch_environment
    config['methods'] = ['reconcile']
    args = _recovery_args(tmp_path, config)
    monkeypatch.setenv(SECRET_ENV, launch_environment(config, args.workspace)[SECRET_ENV])
    @contextmanager
    def selected(*_, **__):
        yield tmp_path/'unexpected-release'
    monkeypatch.setattr(runtime_install, 'selected', selected)
    with pytest.raises(PermissionError, match='runtime changed'):
        control_recovery_cli.execute(args, threading.Event(), expected_release=tmp_path/'release')
    assert not args.state_root.exists()
