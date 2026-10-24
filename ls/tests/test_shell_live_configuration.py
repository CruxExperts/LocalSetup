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
