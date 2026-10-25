"""Signed terminal tuning keeps authority and uncertain native effects separate."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ls.core import live_configuration as config
from ls.core.live_configuration_cli import _owners
from ls.core.live_configuration_runtime import ConfigurationRuntime
from ls.core.terminal_ops import command_live_configuration as live
from ls.core.terminal_ops.command_service import _digest
from ls.core.terminal_ops.command_state import ShellCommandError, atomic_json, read_json
from ls.tests.test_live_configuration import storage_authority


@pytest.fixture
def terminal(tmp_path, storage_authority, monkeypatch):
    authority, _signatures = storage_authority
    root = Path(__file__).resolve().parents[2]
    values, validators = _owners({'terminal': {'kind': 'terminal-runtime'}}, root, tmp_path)
    store = config.ConfigurationStore(tmp_path/'store', '1'*32, authority)
    first = store.commit(values, expected_digest=None, authorized=True, cadence_seconds=1)
    clock = [0.0]
    runtime = ConfigurationRuntime(store, validators, actor='2'*64, machine='3'*64,
                                   operation='4'*64, clock=lambda: clock[0])
    binding = {'authority_file': str(tmp_path/'authority'), 'binding_digest': '5'*64,
               'owner': 'terminal', 'home': str(tmp_path)}
    directory = tmp_path/'session'
    directory.mkdir(mode=0o700)
    session = dict(state='ready', layout_mode='canvas-v3', runtime_settings=values['terminal'],
        runtime_digest=_digest(values['terminal']), configuration_binding=binding,
        endpoint='fixture', status_window_id='@0', native_layout={'desired_generation': 1})
    atomic_json(directory/'session.json', session)
    calls = []
    native = {}
    def apply(record):
        calls.append(record['runtime_settings'])
        colors = record['runtime_settings']['theme']
        native.update({'pane-border-style': f"fg={colors['text']},bg={colors['toolbar']}",
            'pane-active-border-style': f"fg={colors['selected']},bg={colors['toolbar']}"})
    service = SimpleNamespace(source_root=root,
        _require_family_root_live=lambda s: None, _verify=lambda s: {'state': 'ready'},
        _strict_ops=lambda *a: [], _native_inventory=lambda *a, **kw: None,
        backend=SimpleNamespace(apply_native_control_theme=apply,
                                _checked=lambda endpoint, argv: native[argv[-1]]))
    monkeypatch.setattr(live, '_runtime', lambda *args: runtime)
    return SimpleNamespace(service=service, directory=directory, runtime=runtime, binding=binding,
        values=values, calls=calls, native=native, clock=clock, first=first,
        read=lambda: read_json(directory/'session.json'))


def test_live_consumer_applies_signed_revision_on_shared_cadence_and_rejects_protected_changes(terminal):
    t = terminal
    consumer = live.ConfigurationConsumer()
    consumer.poll(t.service, t.directory, t.read())
    assert len(t.calls) == 1 and t.read()['configuration_applied']['digest'] == t.first.digest
    changed = deepcopy(t.values)
    changed['terminal']['layout']['native_canvas']['quiet_seconds'] = 11
    changed['terminal']['layout']['expanded_min_rows'] = 12
    second = t.runtime.store.commit(changed, expected_digest=t.first.digest, authorized=True, cadence_seconds=1)
    consumer.poll(t.service, t.directory, t.read())
    assert len(t.calls) == 1
    t.clock[0] = 1.1
    consumer.poll(t.service, t.directory, t.read())
    assert len(t.calls) == 2
    assert t.read()['runtime_settings']['layout']['expanded_min_rows'] == 12
    assert t.read()['configuration_applied']['digest'] == second.digest
    assert t.read()['ui_redraw_generation'] == 2
    changed['terminal']['layout']['canvas_columns'] = 140
    t.runtime.store.commit(changed, expected_digest=second.digest, authorized=True, cadence_seconds=1)
    t.clock[0] = 2.2
    consumer.poll(t.service, t.directory, t.read())
    assert len(t.calls) == 2 and t.read()['configuration_applied']['digest'] == second.digest
    assert t.read()['configuration_error'] == 'configuration_native_restart_required'


def test_unknown_native_write_is_not_replayed_and_can_be_reconciled(terminal):
    t = terminal
    apply = t.service.backend.apply_native_control_theme
    def lost(record):
        apply(record)
        raise ShellCommandError('native_ack_lost', 'Fixture acknowledgement lost', effect='unknown')
    t.service.backend.apply_native_control_theme = lost
    with pytest.raises(ShellCommandError):
        live._apply(t.service, t.directory, t.binding, t.runtime)
    assert t.read().get('configuration_applied') is None
    assert t.read()['configuration_pending']['digest'] == t.first.digest
    with pytest.raises(config.ConfigurationError, match='configuration_native_pending'):
        live._apply(t.service, t.directory, t.binding, t.runtime)
    assert len(t.calls) == 1
    assert live._apply(t.service, t.directory, t.binding, t.runtime, reconcile=True)['state'] == 'applied'
    assert len(t.calls) == 1 and 'configuration_pending' not in t.read()


def test_reconcile_exact_pending_candidate_after_newer_revision_without_replaying(terminal):
    t = terminal
    def lost(record):
        raise ShellCommandError('native_ack_lost', 'Fixture acknowledgement lost', effect='unknown')
    t.service.backend.apply_native_control_theme = lost
    with pytest.raises(ShellCommandError):
        live._apply(t.service, t.directory, t.binding, t.runtime)
    t.native.update({'pane-border-style': 'wrong', 'pane-active-border-style': 'wrong'})
    with pytest.raises(config.ConfigurationError, match='configuration_native_pending'):
        live._apply(t.service, t.directory, t.binding, t.runtime, reconcile=True)
    changed = deepcopy(t.values)
    changed['terminal']['layout']['native_canvas']['quiet_seconds'] = 11
    t.runtime.store.commit(changed, expected_digest=t.first.digest, authorized=True)
    with pytest.raises(config.ConfigurationError, match='configuration_native_pending'):
        live._apply(t.service, t.directory, t.binding, t.runtime, reconcile=True)
    assert t.read()['configuration_pending']['digest'] == t.first.digest
    assert t.read().get('configuration_applied') is None
    colors = t.values['terminal']['theme']
    t.native.update({'pane-border-style': f"fg={colors['text']},bg={colors['toolbar']}",
        'pane-active-border-style': f"fg={colors['selected']},bg={colors['toolbar']}"})
    reconciled = live._apply(t.service, t.directory, t.binding, t.runtime, reconcile=True)
    assert reconciled['digest'] == t.first.digest and not t.calls
    assert 'configuration_pending' not in t.read()


@pytest.mark.parametrize('seconds', [0, 301, True])
def test_native_timing_is_bounded_and_signed_owner_requires_complete_settings(terminal, seconds):
    t = terminal
    value = deepcopy(t.values['terminal'])
    value['layout']['native_canvas']['quiet_seconds'] = seconds
    with pytest.raises(config.ConfigurationError):
        t.runtime.stage('terminal', value)
    with pytest.raises(config.ConfigurationError):
        t.runtime.stage('terminal', {})


def _presentation_service(t):
    t.service._load = lambda handle: (t.directory, t.read(), None)
    t.service._authorize = lambda *args: None
    return t.service


@pytest.mark.parametrize('bound,writable', [(False, False), (True, False), (True, True)])
def test_presentation_default_reuses_signed_store_or_explains_session_scope(terminal, monkeypatch, bound, writable):
    import json
    t = terminal
    service = _presentation_service(t)
    if not bound:
        session = t.read()
        session.pop('configuration_binding')
        atomic_json(t.directory/'session.json', session)
    monkeypatch.setattr(live, 'read_private_file', lambda *a, **kw: json.dumps({'write': writable}).encode())
    result = live.apply_presentation(service, 'fixture', changes={'inspection_enabled': False},
                                     owner={}, make_default=True)
    assert t.read()['runtime_settings']['inspection_enabled'] is False
    if writable and bound:
        assert result['default_state'] == 'saved'
        assert t.runtime.value('terminal')['inspection_enabled'] is False
    else:
        assert result['default_state'] == 'session_only'
        assert 'session only' in result['default_reason']
        assert t.runtime.value('terminal')['inspection_enabled'] is True


def test_session_presentation_unknown_write_requires_readback_without_replay(terminal):
    t = terminal
    service = _presentation_service(t)
    previous = t.read()['runtime_digest']
    apply = service.backend.apply_native_control_theme
    def lost(record):
        apply(record)
        raise ShellCommandError('native_ack_lost', 'Fixture acknowledgement lost', effect='unknown')
    service.backend.apply_native_control_theme = lost
    with pytest.raises(ShellCommandError):
        live.apply_presentation(service, 'fixture', changes={'inspection_enabled': False}, owner={})
    assert t.read()['runtime_digest'] == previous
    with pytest.raises(config.ConfigurationError, match='configuration_native_pending'):
        live.apply_presentation(service, 'fixture', changes={'inspection_enabled': False}, owner={})
    live.apply_presentation(service, 'fixture', changes={'inspection_enabled': False}, owner={}, reconcile=True)
    assert len(t.calls) == 1
    assert 'presentation_pending' not in t.read()


@pytest.mark.parametrize('bound,writable', [(False, False), (True, False), (True, True)])
def test_primary_viewer_defaults_require_bound_write_authority(terminal, monkeypatch, bound, writable):
    import json
    t = terminal
    service = _presentation_service(t)
    clients = []
    service._native_primary_client = lambda directory, session, pid: (clients.append(pid) or True)
    if not bound:
        session = t.read()
        session.pop('configuration_binding')
        atomic_json(t.directory/'session.json', session)
    monkeypatch.setattr(live, 'read_private_file', lambda *args, **kw: json.dumps({'write': writable}).encode())
    result = live.apply_presentation(service, 'fixture', changes={'status_detail': 'minimal'},
                                     client_pid=123, make_default=True)
    assert clients == [123, 123]
    assert t.read()['runtime_settings']['status_detail'] == 'minimal'
    assert result['default_state'] == ('saved' if bound and writable else 'session_only')
    assert t.runtime.value('terminal')['status_detail'] == ('minimal' if bound and writable else 'standard')


@pytest.mark.parametrize('qualification', [(False,), (True, False)])
@pytest.mark.parametrize('make_default', [False, True])
def test_presentation_rejects_unqualified_or_replaced_viewer(terminal, monkeypatch, qualification, make_default):
    t = terminal
    service = _presentation_service(t)
    results = iter(qualification)
    service._native_primary_client = lambda *args: next(results)
    before = t.read()
    before_digest = t.runtime.store.load().digest
    monkeypatch.setattr(live, 'read_private_file', lambda *args, **kw: b'{"write":true}')
    with pytest.raises(config.ConfigurationError, match='configuration_unauthorized'):
        live.apply_presentation(service, 'fixture', changes={'status_detail': 'minimal'}, client_pid=123, make_default=make_default)
    assert t.runtime.store.load().digest == before_digest
    assert t.read() == before and not t.calls


def test_signed_paths_wait_for_uncertain_session_presentation(terminal, monkeypatch):
    t = terminal
    service = _presentation_service(t)
    original_apply = service.backend.apply_native_control_theme
    def lost(record):
        original_apply(record)
        raise ShellCommandError('native_ack_lost', 'Fixture acknowledgement lost', effect='unknown')
    service.backend.apply_native_control_theme = lost
    with pytest.raises(ShellCommandError):
        live.apply_presentation(service, 'fixture', changes={'inspection_enabled': False}, owner={})
    before = t.read()
    signed_before = t.runtime.store.load().digest
    monkeypatch.setattr(live, 'read_private_file', lambda *a, **kw: b'{"write":true}')
    for reconcile in (False, True):
        with pytest.raises(config.ConfigurationError, match='configuration_native_pending'):
            live._apply(service, t.directory, t.binding, t.runtime, reconcile=reconcile)
    with pytest.raises(config.ConfigurationError, match='configuration_native_pending'):
        live.apply_presentation(service, 'fixture', changes={'status_detail': 'minimal'}, owner={}, make_default=True)
    assert t.read() == before and len(t.calls) == 1
    assert t.runtime.store.load().digest == signed_before
    live.apply_presentation(service, 'fixture', changes={'inspection_enabled': False}, owner={}, reconcile=True)
    assert len(t.calls) == 1 and 'presentation_pending' not in t.read()


def test_viewer_preference_waits_for_metadata_lock_instead_of_dropping_choice(terminal):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from ls.core.terminal_ops.command_state import session_lock
    t = terminal
    service = _presentation_service(t)
    entered = Event()
    def qualify(*args):
        entered.set()
        return True
    service._native_primary_client = qualify
    with ThreadPoolExecutor(max_workers=1) as executor:
        with session_lock(t.directory):
            future = executor.submit(live.apply_presentation, service, 'fixture',
                changes={'status_detail': 'minimal'}, client_pid=123)
            assert entered.wait(timeout=2)
            assert not future.done()
        assert future.result(timeout=2)['state'] == 'applied'
    assert t.read()['runtime_settings']['status_detail'] == 'minimal'
    assert len(t.calls) == 1


def test_old_signed_shortcut_shape_applies_without_spurious_restart(terminal):
    t = terminal
    values = deepcopy(t.values)
    values['terminal'].pop('native_shortcuts')
    saved = t.runtime.store.commit(values, expected_digest=t.first.digest, authorized=True)
    result = live._apply(t.service, t.directory, t.binding, t.runtime)
    assert result['digest'] == saved.digest
    remapped = deepcopy(values)
    remapped['terminal']['native_shortcuts'] = {'menu': 'F8', 'focus_next': 'Tab',
        'focus_previous': 'BTab', 'activate': 'Enter', 'previous': 'Up', 'next': 'Down'}
    t.runtime.store.commit(remapped, expected_digest=saved.digest, authorized=True)
    with pytest.raises(config.ConfigurationError, match='configuration_native_restart_required'):
        live._apply(t.service, t.directory, t.binding, t.runtime)
    assert len(t.calls) == 1
