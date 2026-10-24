import base64
from contextlib import contextmanager
import json
import socket
import threading
import time

import pytest

from ls.core.agent.control_authority import Admission
from ls.core.agent.control_envelope import RemotePeer
from ls.core.agent.control_run import listen
from ls.core.agent.control_transport import Stream
from ls.core.agent.remote_control import open_receipt, seal_request
from ls.core.agent.session_owner import lease
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE


def test_live_scope_announces_encrypted_binding_and_collects_reader(tmp_path, separate_machine_env):
    env = separate_machine_env
    def peer(role, local_role, local, remote_role, remote):
        snapshot = env.snapshots[role]
        return RemotePeer(env.homes[role], env.paths[role], snapshot.store_id,
            snapshot.revision, snapshot.epoch, env.policies[role].scope, local_role, local,
            remote_role, remote, SECRET_REFERENCE, env.resolver)
    controller = peer('controller', 'owner', env.owner, 'publisher', env.publisher)
    agent = peer('origin', 'publisher', env.publisher, 'owner', env.owner)
    sessions, workspace = tmp_path/'sessions', tmp_path/'workspace'
    sessions.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    cancelled = threading.Event()
    expires = time.monotonic()+120
    with _pair() as (left, right):
        fd = left.detach()
        client = Stream(fd, fd, expires=expires)
        ready = []
        try:
            with lease(sessions, task='task', session='session', workspace=workspace,
                       expires=expires, revoked=cancelled) as owner:
                with listen(right.fileno(), Admission('agent', frozenset({'status', 'cancel'}), agent),
                            owner, 'run', cancelled=cancelled, ready=ready.append, nonce='a'*32) as binding:
                    announcement = controller.open(base64.b64decode(ready[0]['envelope'], validate=True))
                    assert announcement['kind'] == 'ready' and announcement['binding'] == binding.value()
                    assert announcement['methods'] == ['cancel', 'status']
                    from ls.core.agent.control_ready import open_ready
                    expected = {key: getattr(binding, key) for key in ('agent', 'machine', 'task', 'session')}
                    methods = frozenset({'status', 'cancel'})
                    assert open_ready(controller, ready[0], nonce='a'*32, expected=expected, methods=methods) == binding
                    with pytest.raises(PermissionError):
                        open_ready(controller, ready[0], nonce='b'*32, expected=expected, methods=methods)
                    assert 'binding' not in ready[0]
                    for index, method in enumerate(('status', 'cancel'), 1):
                        raw = json.dumps({'schema_version': 1, 'id': index, 'method': method}).encode()
                        request_id = f'{index:032x}'
                        response = client.exchange('execute', request_id,
                            seal_request(controller, binding, request_id, raw))
                        receipt = open_receipt(controller, binding, request_id, response, request=raw)
                        assert receipt['result']['status'] == ('active' if index == 1 else 'cancellation_requested')
                assert cancelled.is_set()
                assert not any(t.name == 'agent-encrypted-control' for t in threading.enumerate())
                saved = json.loads((owner.root/'controls/run/binding.json').read_text())
                assert saved == binding.value()
        finally:
            client.close()


@contextmanager
def _pair():
    left, right = socket.socketpair()
    try:
        yield left, right
    finally:
        left.close(); right.close()


@pytest.mark.parametrize('failure', ['ready', 'eof'])
def test_scope_failure_collects_reader_and_preserves_owner_lifetime(tmp_path, failure):
    from types import SimpleNamespace
    class Peer:
        scope = ('fixture',)
        def check(self): pass
        def seal(self, value): return b'opaque-fixture'
    sessions, workspace = tmp_path/'sessions', tmp_path/'workspace'
    sessions.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    cancelled = threading.Event()
    def ready(_):
        if failure == 'ready':
            raise RuntimeError('sink failed')
    with _pair() as (left, right):
        with lease(sessions, task='task', session='session', workspace=workspace,
                   expires=time.monotonic()+10, revoked=cancelled) as owner:
            with pytest.raises((RuntimeError, ConnectionError)):
                with listen(right.fileno(), SimpleNamespace(agent='agent', methods=frozenset({'status'}), peer=Peer()),
                            owner, 'run', cancelled=cancelled, ready=ready, nonce='a'*32):
                    left.close()
                    assert cancelled.wait(2)
            assert not any(t.name == 'agent-encrypted-control' for t in threading.enumerate())
