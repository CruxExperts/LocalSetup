"""Durable bounded views, read-only paging and cancellation during paused I/O."""
import hashlib
import json
import socket
import threading
import time

import pytest

from ls.core.agent import event_buffer as events
from ls.core.agent.event_delivery import Delivery
from ls.core.agent.output_views import Observations
from ls.core.agent.run_control import listen
from ls.core.output_filters import defaults, parse_policy

RUN = 'a'*32


def view(text):
    result = parse_policy(defaults()).apply(text, sink='retained-log')
    return dict(text=result.text, filter=result.metadata())


def buffer(root, **limits):
    return events.EventBuffer(root, task='task', session='session', run=RUN, **limits)


def test_durable_rollover_cursors_bytes_and_cli_are_read_only(tmp_path, capfd):
    root = tmp_path/'events'
    store = buffer(root, max_events=2)
    for text in ('one', 'two', 'three'):store.append(view(text))
    page = store.read(max_bytes=4096)
    assert page['gap'] == {'from': 1, 'through': 1}
    assert [event['text'] for event in page['events']] == ['two', 'three']
    assert page['next_cursor'] == {'run': RUN, 'after': 3} and not page['more']
    raw = store.path.read_bytes()
    assert page['checkpoint'] == hashlib.sha256(raw).hexdigest()
    assert events.read(root, task='task', session='session', run=RUN, max_bytes=4096) == page
    from ls.core.agent.cli import main
    assert main(['events', '--state-root', str(tmp_path), '--task', 'task', '--session', 'session',
                 '--run', RUN, '--after', '2']) == 0
    assert json.loads(capfd.readouterr().out)['events'][0]['text'] == 'three'
    assert store.path.read_bytes() == raw
    with pytest.raises(ValueError):events.read(root, task='other', session='session', run=RUN)
    with pytest.raises(ValueError):store.read(after=4)
    with pytest.raises(FileExistsError):buffer(root)
    assert main(['events', '--state-root', str(tmp_path/'missing'), '--task', 'task', '--session', 'session',
                 '--run', RUN]) == 2
    assert not (tmp_path/'missing').exists()


def test_byte_and_line_limits_report_non_consuming_oversize_page(tmp_path, capfd):
    store = buffer(tmp_path/'events', max_bytes=4096)
    store.append(view('x'*1500));store.append(view('y'*1500));store.append(view('z'*1500))
    assert len(store.raw) <= 4096 and store.read()['first'] > 1
    small = store.read(max_bytes=1024)
    assert small['events'] == [] and small['more'] and small['required_bytes'] > 1024
    large = store.read(after=small['next_cursor']['after'], max_bytes=small['required_bytes'])
    assert len(large['events']) == 1 and len(events._encode(large)) <= small['required_bytes']
    from ls.core.agent.cli import main
    assert main(['events', '--state-root', str(tmp_path), '--task', 'task', '--session', 'session',
                 '--run', RUN, '--after', str(small['next_cursor']['after']),
                 '--max-bytes', str(small['required_bytes'])]) == 0
    emitted = capfd.readouterr().out.encode().removesuffix(b'\n')
    assert len(emitted) <= small['required_bytes'] and json.loads(emitted) == large
    lines = buffer(tmp_path/'lines', max_lines=3)
    lines.append(view('a\nb'));lines.append(view('c\nd'))
    assert lines.read()['first'] == 2
    before = lines.raw
    with pytest.raises(ValueError, match='line limit'):lines.append(view('a\nb\nc\nd'))
    assert lines.raw == before == lines.path.read_bytes()


def test_uncertain_publication_stops_writer_and_reader_reconciles_actual_bytes(tmp_path, monkeypatch):
    store = buffer(tmp_path/'events')
    original = events.write_private_file
    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('ack lost')
    monkeypatch.setattr(events, 'write_private_file', lost_ack)
    with pytest.raises(OSError):store.append(view('published'))
    assert store.failed and store.read()['events'] == []
    actual = events.read(tmp_path/'events', task='task', session='session', run=RUN)
    assert actual['events'][0]['text'] == 'published'
    with pytest.raises(ValueError, match='reconciliation'):store.append(view('must not replay'))


def test_rollover_does_not_refund_total_work_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(events, 'MAX_TOTAL', 2000)
    store = buffer(tmp_path/'events', max_events=1)
    store.append(view('x'*1000))
    before = store.raw
    with pytest.raises(ValueError, match='work budget'):store.append(view('y'*1000))
    assert store.path.read_bytes() == store.raw == before


def test_reject_private_state_replacement_and_corrupt_records(tmp_path):
    store = buffer(tmp_path/'events');store.append(view('one'))
    raw = store.path.read_bytes()
    store.path.chmod(0o644)
    with pytest.raises((ValueError, OSError)):events.read(tmp_path/'events', task='task', session='session', run=RUN)
    store.path.chmod(0o600)
    store.path.unlink();store.path.symlink_to(tmp_path/'foreign')
    with pytest.raises((ValueError, OSError)):store.append(view('two'))
    assert not (tmp_path/'foreign').exists()
    damaged = json.loads(raw);damaged['events'][0]['sequence'] = 2
    with pytest.raises(ValueError):events.page(events._encode(damaged), task='task', session='session', run=RUN)


def test_policy_sinks_filter_same_original_and_halt_prevents_any_publication(tmp_path):
    policy = defaults()
    policy['rules'] = [dict(order=10, sinks=['observation'], match='word', pattern='secret',
                            action='replace', replacement='visible'),
                       dict(order=20, sinks=['retained-log'], match='word', pattern='secret',
                            action='replace', replacement='retained')]
    cancelled = threading.Event()
    consumer = Observations(parse_policy(policy), lambda: None, cancelled)
    consumer.retention = buffer(tmp_path/'events')
    assert consumer.text('secret')['text'] == 'visible'
    assert consumer.retention.read()['events'][0]['text'] == 'retained'
    assert b'secret' not in consumer.retention.path.read_bytes()
    policy['rules'][1].pop('replacement');policy['rules'][1]['action'] = 'halt'
    consumer.policy = parse_policy(policy)
    before = consumer.retention.raw
    with pytest.raises(InterruptedError):consumer.text('secret')
    assert cancelled.is_set() and consumer.retention.raw == before


def test_control_pause_resume_read_and_cancel_do_not_wait_for_disk(tmp_path, monkeypatch):
    store = buffer(tmp_path/'events');store.append(view('first'))
    delivery = Delivery();delivery.bind(store)
    entered, release = threading.Event(), threading.Event()
    original = events.write_private_file
    def stalled(*args, **kwargs):
        entered.set()
        if not release.wait(3):raise TimeoutError('test stalled')
        original(*args, **kwargs)
    monkeypatch.setattr(events, 'write_private_file', stalled)
    errors = []
    def append():
        try:store.append(view('second'))
        except BaseException as error:errors.append(error)
    writer = threading.Thread(target=append);writer.start()
    owner, child = socket.socketpair();cancelled = threading.Event()
    def request(identifier, method, **extra):
        owner.sendall(events._encode(dict(schema_version=1, id=identifier, method=method, **extra))+b'\n')
        received = b''
        while b'\n' not in received:received += owner.recv(4096)
        return json.loads(received)
    try:
        assert entered.wait(1)
        with owner, listen(child.detach(), cancelled, time.monotonic()+3, events=delivery):
            owner.settimeout(0.5)
            assert request(1, 'events.pause')['events']['paused']
            page = request(2, 'events.read', after=0, limit=20, max_bytes=4096)['events']
            assert [event['text'] for event in page['events']] == ['first']
            assert request(3, 'events.status')['events']['publication_pending']
            assert not request(4, 'events.resume')['events']['paused']
            assert request(5, 'cancel')['status'] == 'cancellation_requested'
            assert cancelled.is_set() and writer.is_alive()
    finally:
        release.set();writer.join(timeout=2)
        owner.close()
    assert not writer.is_alive() and not errors
