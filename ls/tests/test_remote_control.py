"""Real separate-key encrypted control, without installing a network listener."""
from dataclasses import replace
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from ls.core.agent.control_envelope import ControlBinding, RemotePeer
from ls.core.agent.control_requests import Requests
from ls.core.agent.operation_journal import Journal
from ls.core.agent.remote_control import seal_request, serve_request, open_receipt
from ls.core.agent.steering import Steering
from ls.core.openpgp.opening import EnvelopeOpenError
from ls.core.openpgp.envelope import seal_envelope
from ls.core.openpgp.contracts import LocalTrust
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE


def test_separate_key_control_identity_receipt_restart_and_revocation(tmp_path, separate_machine_env):
    env = separate_machine_env
    def peer(role, local_role, local, remote_role, remote):
        snapshot = env.snapshots[role]
        return RemotePeer(env.homes[role], env.paths[role], snapshot.store_id,
            snapshot.revision, snapshot.epoch, env.policies[role].scope,
            local_role, local, remote_role, remote, SECRET_REFERENCE, env.resolver)
    controller = peer('controller', 'owner', env.owner, 'publisher', env.publisher)
    agent = peer('origin', 'publisher', env.publisher, 'owner', env.owner)
    now = int(time.time())
    binding = ControlBinding('agent', 'machine', controller.scope, 'task', 'session',
        'run', 'operation', 'generation', now, now+600)
    root = tmp_path/'control-journal'; root.mkdir(mode=0o700)
    journal = Journal(root, task='task', session='session')
    cancelled = threading.Event()
    expires = time.monotonic()+600
    steering = Steering(cancelled, expires); steering.bind('task', 'session', 'profile')
    requests = Requests(cancelled, expires, steering=steering)
    raw = json.dumps(dict(schema_version=1, id=1, method='steer', task='task',
        session='session', profile='profile', text='private-control-fixture', disclose=True)).encode()
    request_id = 'a'*32
    ciphertext = seal_request(controller, binding, request_id, raw)
    assert b'private-control-fixture' not in ciphertext
    def serve(frame=ciphertext, selected=binding, authorize=lambda *_: True, owner=requests, identity=request_id):
        return serve_request(agent, selected, identity, frame, requests=owner,
                             journal=journal, authorize=authorize)
    with pytest.raises(PermissionError):
        serve(selected=replace(binding, machine='other-machine'))
    with pytest.raises(PermissionError):
        serve(authorize=lambda *_: False)
    with pytest.raises(PermissionError):
        serve(identity='f'*32)
    with pytest.raises(EnvelopeOpenError):
        serve(frame=ciphertext[:-1]+bytes([ciphertext[-1]^1]))
    wrong_signer = seal_envelope(b'{}', gnupg_home=env.homes['receiver'],
        signer_fingerprint=env.recipient, recipient_fingerprints=(env.publisher,),
        local_trust=LocalTrust({env.recipient}), passphrase_reference=SECRET_REFERENCE,
        secret_resolver=env.resolver)
    with pytest.raises(EnvelopeOpenError):
        serve(frame=wrong_signer)
    assert journal.inspect() == {} and steering.take() == []
    encrypted_receipt = serve()
    assert b'private-control-fixture' not in encrypted_receipt
    receipt = open_receipt(controller, binding, request_id, encrypted_receipt, request=raw)
    assert receipt['result']['status'] == 'queued' and steering.take() == ['private-control-fixture']
    with pytest.raises(PermissionError):
        open_receipt(controller, binding, request_id, encrypted_receipt,
                     request=raw.replace(b'private-control-fixture', b'changed-fixture'))
    reflected = agent.open(ciphertext)
    with pytest.raises(PermissionError, match='admitted operation'):
        open_receipt(controller, binding, request_id, agent.seal(reflected), request=raw)
    with pytest.raises(PermissionError):
        serve(selected=replace(binding, issued_at=now-20, expires_at=now-10))
    restarted = Requests(cancelled, expires, steering=steering)
    with pytest.raises(ValueError, match='already accepted'):
        serve(owner=restarted)
    assert steering.take() == [] and len(journal.inspect()) == 1

    # Cancellation applied, but a later authority loss suppresses the receipt.
    cancel_raw = b'{"schema_version":1,"id":2,"method":"cancel"}'
    cancel_cipher = seal_request(controller, binding, 'b'*32, cancel_raw)
    checks = []
    def revoke_after_effect(*_):
        checks.append(True)
        return len(checks) == 1
    with pytest.raises(PermissionError, match='after dispatch'):
        serve(cancel_cipher, authorize=revoke_after_effect,
              owner=Requests(cancelled, expires), identity='b'*32)
    assert cancelled.is_set() and len(journal.inspect()) == 2
    with pytest.raises(ValueError, match='already accepted'):
        serve(cancel_cipher, owner=Requests(cancelled, expires), identity='b'*32)
    assert all(state['outcome'] == 'completed' for state in journal.inspect().values())
    assert 'private-control-fixture' not in ''.join(p.read_text() for p in root.glob('*.json'))

    # Trust revision loss is independent of the application's admission callback.
    with sqlite3.connect(agent.trust_state_path) as db:
        db.execute('UPDATE state SET revision=revision+1 WHERE singleton=1')
    with pytest.raises(PermissionError, match='authority changed'):
        serve(owner=Requests(cancelled, expires))
    assert len(journal.inspect()) == 2


def test_binding_rejects_ambiguous_or_unbounded_identity():
    binding = ControlBinding('a', 'm', ('scope',), 't', 's', 'r', 'o', 'g', 1, 100)
    for changes in ({'scope': ['scope']}, {'agent': ''}, {'issued_at': True},
                    {'expires_at': 1}, {'expires_at': 86402}, {'machine': 'x'*129}):
        with pytest.raises(ValueError):
            replace(binding, **changes)


@pytest.mark.parametrize('result', [
    {'schema_version': 1, 'id': 1, 'status': 'queued'},
    {'schema_version': 1, 'id': True, 'status': 'active'},
    {'schema_version': 1, 'id': 1, 'status': 'active', 'unexpected': True},
])
def test_authenticated_receipt_still_requires_correct_result_schema(monkeypatch, result):
    from ls.core.agent import remote_control as protocol
    from ls.core.agent.control_envelope import encode

    binding = ControlBinding('a', 'm', ('scope',), 't', 's', 'r', 'o', 'g', 1, 100)
    raw = b'{"schema_version":1,"id":1,"method":"status"}'
    receipt = dict(operation='a'*32, request_id='b'*32, binding_sha256=binding.digest(),
        request_sha256=hashlib.sha256(raw).hexdigest(), result=result,
        result_sha256=hashlib.sha256(encode(result)).hexdigest())
    monkeypatch.setattr(protocol, '_open', lambda *_: receipt)
    with pytest.raises(PermissionError):
        protocol.open_receipt(None, binding, 'b'*32, b'authenticated-fixture', request=raw)
