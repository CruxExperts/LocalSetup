"""Durable owner-control deduplication without transport or key authority."""
import hashlib
import json
import threading
import time

import pytest

from ls.core.agent import operation_journal as journal
from ls.core.agent.control_requests import Requests

BINDING = hashlib.sha256(b'admitted fixture binding').hexdigest()


def request(sequence, method='status', **fields):
    return json.dumps(dict(schema_version=1, id=sequence, method=method, **fields)).encode()


def log(root):
    root.mkdir(mode=0o700, exist_ok=True)
    return journal.Journal(root, task='task', session='session')


def dispatch(owner, store, sequence, method='status', **fields):
    return owner.dispatch_recorded(request(sequence, method, **fields), store,
        binding_sha256=BINDING, request_id=f'{sequence:032x}')


def test_restart_and_lost_ack_never_repeat_accepted_decision(tmp_path):
    calls = []

    class Approvals:
        def decide(self, value):
            calls.append(value['decision'])

    store = log(tmp_path/'control')
    owner = Requests(threading.Event(), time.monotonic()+5, approvals=Approvals())
    receipt = dispatch(owner, store, 1, 'approve', decision='private-fixture-decision')
    assert receipt['result']['status'] == 'decision_recorded'
    recovered = log(store.root)
    new_owner = Requests(threading.Event(), time.monotonic()+5, approvals=Approvals())
    with pytest.raises(ValueError, match='already accepted'):
        dispatch(new_owner, recovered, 1, 'approve', decision='changed-decision')
    assert calls == ['private-fixture-decision']
    state = recovered.inspect()[receipt['operation']]
    assert state['outcome'] == 'completed'
    assert state['result']['evidence_sha256'] == receipt['result_sha256']
    assert 'private-fixture-decision' not in ''.join(p.read_text() for p in store.root.glob('*.json'))
    fresh = Requests(threading.Event(), time.monotonic()+5)
    assert dispatch(fresh, recovered, 2)['result']['id'] == 2


def test_uncertain_handler_blocks_effects_but_allows_cancel(tmp_path):
    cancelled = threading.Event()

    class Approvals:
        def decide(self, value):
            raise OSError('effect may have happened')

    store = log(tmp_path/'control')
    owner = Requests(cancelled, time.monotonic()+5, approvals=Approvals())
    with pytest.raises(OSError):
        dispatch(owner, store, 1, 'approve')
    recovered = log(store.root)
    blocked = Requests(cancelled, time.monotonic()+5, approvals=Approvals())
    with pytest.raises(ValueError, match='Unreconciled'):
        dispatch(blocked, recovered, 2, 'approve')
    fresh = Requests(cancelled, time.monotonic()+5)
    assert dispatch(fresh, recovered, 2, 'cancel')['result']['status'] == 'cancellation_requested'
    assert cancelled.is_set()
    states = list(recovered.inspect().values())
    assert [s['outcome'] for s in states] == ['uncertain', 'completed']


def test_binding_and_legacy_journals_cannot_be_mixed(tmp_path):
    store = log(tmp_path/'control')
    owner = Requests(threading.Event(), time.monotonic()+5)
    dispatch(owner, store, 1)
    with pytest.raises(ValueError, match='identity changed'):
        owner.dispatch_recorded(request(2), store, binding_sha256='f'*64, request_id='2'*32)
    with pytest.raises(ValueError, match='separate journal'):
        store.begin('process', dict(argv_sha256=BINDING, snapshot_sha256=BINDING))
    legacy = log(tmp_path/'legacy')
    legacy.begin('process', dict(argv_sha256=BINDING, snapshot_sha256=BINDING))
    fresh = Requests(threading.Event(), time.monotonic()+5)
    with pytest.raises(ValueError, match='separate journal'):
        dispatch(fresh, legacy, 1, 'cancel')


def test_pending_controls_reserve_each_terminal_record(tmp_path, monkeypatch):
    store = log(tmp_path/'control')
    monkeypatch.setattr(journal, 'MAX_RECORDS', 4)
    def begin(sequence):
        return store.begin('owner_control', dict(binding_sha256=BINDING,
            request_sha256=BINDING, request_id=f'{sequence:032x}', sequence=sequence,
            method='status', request_bytes=60))
    first, second = begin(1), begin(2)
    with pytest.raises(ValueError, match='full'):
        begin(3)
    store.finish(first, 'completed', evidence_sha256=BINDING)
    store.finish(second, 'completed', evidence_sha256=BINDING)
    assert all(s['outcome'] == 'completed' for s in store.inspect().values())


def test_recording_serializes_direct_dispatch_before_intent(tmp_path, monkeypatch):
    store = log(tmp_path/'control')
    cancelled = threading.Event()
    owner = Requests(cancelled, time.monotonic()+5)
    begin = store.begin

    def interleave(*args, **kwargs):
        with pytest.raises(PermissionError, match='already active'):
            owner.dispatch(request(1, 'cancel'))
        return begin(*args, **kwargs)

    monkeypatch.setattr(store, 'begin', interleave)
    receipt = dispatch(owner, store, 1)
    assert receipt['binding_sha256'] == BINDING and not cancelled.is_set()
