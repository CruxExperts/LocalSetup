"""Read-only reconnects preserve observations and stop at owner boundaries."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ls.core.terminal_ops import command_activity_cli as cli
from ls.core.terminal_ops.command_activity import Activity, observation
from ls.core.terminal_ops.command_activity_codex import CodexObserver


def test_partial_snapshot_failure_preserves_older_pending_completions():
    observer = CodexObserver('root', {}, '.')
    pending = {('root', 'turn', key): None for key in ('old', 'later')}
    observer.pending, observer.cursors = dict(pending), {'root': 'tail'}
    activity = Activity()
    activity.accept({'thread': 'root', 'id': 'old', 'kind': 'shell', 'name': 'shell', 'state': 'Running'})
    def request(method, params):
        if method == 'thread/list': return {'data': []}
        if method == 'thread/items/list':
            return {'data': [], 'nextCursor': 'advanced'} if params.get('cursor') == 'tail' else {
                'data': [], 'backwardsCursor': 'fresh'}
        key = params['itemIds'][0]
        if key == 'later': raise TimeoutError('transport dropped after earlier completion')
        return {'data': [{'turnId': 'turn', 'item': {'id': key, 'type': 'commandExecution', 'status': 'completed'}}]}
    observer.request = request
    with pytest.raises(TimeoutError): observer.snapshot()
    assert observer.pending == pending and observer.cursors == {'root': 'tail'}
    fresh = CodexObserver('root', {}, '.')
    fresh.pending = observer.pending
    def recovered(method, params):
        if method != 'thread/items/read': return request(method, params)
        return {'data': [{'turnId': 'turn', 'item': {'id': params['itemIds'][0],
                         'type': 'commandExecution', 'status': 'completed'}}]}
    fresh.request = recovered
    _labels, entries, _gap = fresh.snapshot()
    for thread, entry in entries: activity.accept(observation(thread, entry))
    assert activity.items['root', 'old']['state'] == 'Completed'
    assert not fresh.pending


@pytest.fixture
def harness(tmp_path, monkeypatch):
    session = {'handle': 'family', 'state': 'ready', 'owner': {'pid': 1},
               'runtime_settings': {}}
    writes, delays = [], []
    live = [True]
    monkeypatch.setattr(cli, 'read_json', lambda *_a, **_kw: deepcopy(session))
    monkeypatch.setattr(cli, 'atomic_json', lambda _p, value: writes.append(deepcopy(value)))
    monkeypatch.setattr(cli, 'owner_liveness', lambda _owner: 'live' if live[0] else 'dead')
    monkeypatch.setattr(cli, 'color_capable', lambda *_a: False)
    monkeypatch.setattr(cli.time, 'sleep', delays.append)
    monkeypatch.setattr('ls.core.process_identity.set_process_name', lambda: None)
    return SimpleNamespace(session=session, writes=writes, delays=delays, live=live,
        run=lambda: cli.main(['--session-record', str(tmp_path/'session.json'),
                              '--thread', 'root', '--workspace', str(tmp_path)]))


def test_reconnect_keeps_pending_items_and_deduplicates_retained_history(harness, monkeypatch, capsys):
    instances = []
    item = {'turnId': 'turn', 'item': {'id': 'old', 'type': 'commandExecution',
            'status': 'inProgress', 'command': 'PRIVATE'}}
    class Observer:
        def __init__(self, *_args):
            self.calls = 0
            self.closed = False
            instances.append(self)
        def connect(self): pass
        def close(self): self.closed = True
        def snapshot(self):
            self.calls += 1
            if len(instances) == 1:
                self.pending[('root', 'turn', 'old')] = None
                if self.calls == 2: raise RuntimeError('PRIVATE')
                return {'root': 'root'}, [('root', item)], False
            assert ('root', 'turn', 'old') in self.pending
            harness.session['state'] = 'closed'
            completed = deepcopy(item)
            completed['item']['status'] = 'completed'
            return {'root': 'root'}, [('root', item), ('root', completed)], False
    monkeypatch.setattr(cli, 'CodexObserver', Observer)
    assert harness.run() == 0
    output = capsys.readouterr().out
    assert output.count('shell | Running') == output.count('shell | Completed') == 1
    assert 'PRIVATE' not in output
    assert len(instances) == 2 and all(item.closed for item in instances)
    assert any(item['status'] == 'reconnecting' for item in harness.writes)
    assert harness.writes[-1]['status'] == 'finished'
    assert harness.writes[-1]['history_gap'] is True


@pytest.mark.parametrize('attempts,expected_delays', [(0, []), (3, [1, 2, 4])])
def test_reconnect_has_configured_finite_budget(harness, monkeypatch, capsys, attempts, expected_delays):
    harness.session['runtime_settings']['client_activity_reconnect_attempts'] = attempts
    instances = []
    class Observer:
        def __init__(self, *_args): instances.append(self)
        def connect(self): pass
        def snapshot(self): raise RuntimeError('PRIVATE')
        def close(self): pass
    monkeypatch.setattr(cli, 'CodexObserver', Observer)
    assert harness.run() == 3
    assert len(instances) == attempts + 1
    assert harness.delays == expected_delays
    assert harness.writes[-1]['status'] == 'unavailable'
    assert 'PRIVATE' not in str(harness.writes) + capsys.readouterr().out


@pytest.mark.parametrize('failure', [PermissionError('PRIVATE'), ValueError('PRIVATE')])
def test_authority_or_validation_failure_never_reconnects(harness, monkeypatch, failure):
    calls = []
    class Observer:
        def __init__(self, *_args): calls.append('new')
        def connect(self): raise failure
        def close(self): calls.append('closed')
    monkeypatch.setattr(cli, 'CodexObserver', Observer)
    assert harness.run() == 3
    assert calls == ['new', 'closed'] and not harness.delays


@pytest.mark.parametrize('boundary', ['owner_exit', 'owner_changed', 'disabled', 'closed'])
def test_backoff_rechecks_owner_and_session_before_another_connection(harness, monkeypatch, boundary):
    instances = []
    class Observer:
        def __init__(self, *_args): instances.append(self)
        def connect(self): pass
        def snapshot(self): raise RuntimeError('unavailable')
        def close(self): pass
    def revoke(_seconds):
        if boundary == 'owner_exit': harness.live[0] = False
        elif boundary == 'owner_changed': harness.session['owner'] = {'pid': 2}
        elif boundary == 'disabled': harness.session['runtime_settings']['client_activity_enabled'] = False
        else: harness.session['state'] = 'closed'
    monkeypatch.setattr(cli, 'CodexObserver', Observer)
    monkeypatch.setattr(cli.time, 'sleep', revoke)
    assert harness.run() == 0
    assert len(instances) == 1 and harness.writes[-1]['status'] == 'finished'
