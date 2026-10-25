from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from ls.core.agent import heartbeat_execution as execution, heartbeat_action as action
from ls.core.agent import heartbeat_budget_store as store
from ls.core.agent.run_cli import _state
from ls.core.agent.runtime_install import _write_json
from ls.core.agent.session_owner import lease
from ls.tests.test_heartbeat_action import configured
from ls.tests.test_heartbeat_accounting_cli import private
from ls.tests.test_heartbeat_budget_store import document


@pytest.fixture
def runtime(configured, monkeypatch):
    workspace, root, source, value = configured
    @contextmanager
    def selected(path, **kwargs):
        yield path/('e'*64)
    monkeypatch.setattr(execution, 'selected', selected)
    return workspace, root, source, value


def policy(runtime, *, compact=False, requests=10):
    workspace, root, source, value = runtime
    if compact:
        _state(Path(value['state_root']))
        digest = action.plan(source, workspace, root)['profile_sha256']
        with own(value, workspace) as owner:
            value['checkpoint'] = owner.save_checkpoint(b'[]', profile=digest, run_id='source', step=0, state='complete')
        value['compact'] = dict(tokens=200, seconds=40, keep_messages=0, disclose_history=True)
        private(source, value)
    plan = action.plan(source, workspace, root)
    doc = document(workspace)
    doc['policy']['budget']['requests'] = requests
    doc['authorizations'] = {value['operation']: plan['authorization']}
    state = store.initialize(root, workspace, doc, hashlib.sha256(store.files.encode(doc)).hexdigest())
    return plan, state


def own(value, workspace):
    return lease(Path(value['state_root'])/'sessions', task=value['task'], session=value['session'],
                 workspace=workspace, expires=time.monotonic()+5)


def fake_dispatch(monkeypatch, runtime, *, fail=None, mutate=False):
    workspace, root, source, value = runtime
    calls = []
    original = execution._script
    def dispatch(argv, **kwargs):
        state = store.inspect(root, workspace)
        assert state['summary']['status'] == 'reconciliation_required'
        assert state['summary']['charged']['requests'] == (3 if value['compact'] else 2)
        kind = argv[7]
        calls.append(kind)
        if kind == 'run' and value['checkpoint'] is None:
            assert '--require-new-session' in argv
        profiles = Path(argv[argv.index('--profiles')+1])
        assert json.loads(profiles.read_text())['profiles']['fixture']['model'] == 'fixture'
        if fail == kind:
            return dict(returncode=1, protocol=None, stdout_tail='must not persist', stderr_tail='secret')
        _state(Path(value['state_root']))
        profile = action.profile_digest(action.wire(action.parse(json.loads(profiles.read_text())['profiles']['fixture'])))
        with own(value, workspace) as owner:
            checkpoint = owner.save_checkpoint(b'[]', profile=profile, run_id=kind, step=0, state='complete')
            if kind == 'compact':
                result = dict(schema_version=1, source_checkpoint=value['checkpoint'], checkpoint=checkpoint,
                    profile=profile, usage=dict(requests=1, tool_calls=0, input_tokens=100, output_tokens=10))
                _write_json(owner.root/('compaction-'+checkpoint+'.json'), result)
                kwargs['receipt'].feed(json.dumps(result).encode())
            else:
                if value['compact']:
                    assert argv[argv.index('--resume')+1] != value['checkpoint']
                for seq, event, data in [(1, 'start', {k:value[k] for k in ('task','session','profile')}),
                    (2, 'result', dict(status='completed', task=value['task'], session=value['session'],
                                       checkpoint=checkpoint, output='private model output'))]:
                    kwargs['receipt'].feed((json.dumps(dict(schema_version=1, sequence=seq, type=event, data=data))+'\n').encode())
        if mutate:
            Path(value['profiles']).write_text('changed by controller')
            Path(value['grant']).write_text('changed by controller')
        return dict(returncode=0, protocol=kwargs['receipt'].finish(0), stdout_tail='', stderr_tail='')
    monkeypatch.setattr(execution, '_script', lambda name: SimpleNamespace(execute=dispatch)
                        if name == 'heartbeat_process' else original(name))
    return calls


@pytest.mark.parametrize('compact', [False, True])
def test_reserved_execution_uses_frozen_inputs_and_owner_history(runtime, monkeypatch, compact):
    plan, state = policy(runtime, compact=compact)
    calls = fake_dispatch(monkeypatch, runtime, mutate=True)
    workspace, root, source, value = runtime
    result = execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=state['head'])
    assert result['outcome'] == 'execution_completed'
    assert calls == (['compact', 'run'] if compact else ['run'])
    assert result['accounting']['summary']['status'] == 'awaiting_controller_review'
    assert result['accounting']['summary']['charged'] == plan['envelope']
    raw = Path(result['evidence']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == result['result']
    assert b'private model output' not in raw
    assert b'_tail' not in raw


def test_failed_compaction_retains_compound_budget_and_skips_run(runtime, monkeypatch):
    plan, state = policy(runtime, compact=True)
    calls = fake_dispatch(monkeypatch, runtime, fail='compact')
    workspace, root, source, _ = runtime
    result = execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=state['head'])
    assert result['outcome'] == 'failed' and calls == ['compact']
    assert result['accounting']['summary']['charged'] == plan['envelope']
    assert b'secret' not in Path(result['evidence']).read_bytes()
    with pytest.raises(ValueError):
        execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=result['accounting']['head'])
    assert calls == ['compact']


def test_exhaustion_refuses_before_dispatch_or_attempt_storage(runtime, monkeypatch):
    plan, state = policy(runtime, requests=1)
    calls = fake_dispatch(monkeypatch, runtime)
    workspace, root, source, value = runtime
    with pytest.raises(ValueError, match='exhausted'):
        execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=state['head'])
    assert not calls and not Path(value['state_root']).exists()


def test_freeze_failure_retains_uncertain_reservation_without_replay(runtime, monkeypatch):
    plan, state = policy(runtime)
    calls = fake_dispatch(monkeypatch, runtime)
    def fail(*args): raise OSError('lost storage acknowledgement')
    monkeypatch.setattr(execution, '_freeze', fail)
    workspace, root, source, _ = runtime
    with pytest.raises(OSError):
        execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=state['head'])
    current = store.inspect(root, workspace)
    assert current['summary']['status'] == 'reconciliation_required'
    assert current['summary']['charged'] == plan['envelope']
    with pytest.raises(ValueError):
        execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=current['head'])
    assert not calls


@pytest.mark.parametrize('bad', ['binding', 'task', 'history_lock'])
def test_changed_authority_or_incomplete_history_refuses_before_reservation(runtime, monkeypatch, bad):
    plan, state = policy(runtime, compact=bad == 'history_lock')
    calls = fake_dispatch(monkeypatch, runtime)
    workspace, root, source, value = runtime
    if bad == 'task':
        value['task'] = 'different'
        private(source, value)
        plan = action.plan(source, workspace, root)
    elif bad == 'history_lock':
        lock = Path(value['state_root'])/'sessions'/hashlib.sha256(value['session'].encode()).hexdigest()/'journal'/execution.LOCK_NAME
        lock.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        execution.execute(source, workspace, root, expected_binding='f'*64 if bad == 'binding' else plan['binding'],
                          expected_head=state['head'])
    assert not calls and store.inspect(root, workspace)['head'] == state['head']
    if bad == 'history_lock': assert not lock.exists()


def test_result_acknowledgement_loss_leaves_exact_evidence_and_no_replay(runtime, monkeypatch):
    plan, state = policy(runtime)
    calls = fake_dispatch(monkeypatch, runtime)
    workspace, root, source, value = runtime
    original = store.append
    def lost(root, workspace, event, head, **kwargs):
        if event['type'] == 'result': raise OSError('result acknowledgement unavailable')
        return original(root, workspace, event, head, **kwargs)
    monkeypatch.setattr(store, 'append', lost)
    with pytest.raises(OSError):
        execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=state['head'])
    evidence = Path(value['state_root'])/'heartbeat'/plan['binding']/'result.json'
    assert json.loads(evidence.read_text())['outcome'] == 'execution_completed'
    current = store.inspect(root, workspace)
    assert current['summary']['status'] == 'reconciliation_required'
    with pytest.raises((ValueError, FileExistsError)):
        execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=current['head'])
    assert calls == ['run']


def test_process_cancellation_is_terminal_and_does_not_start_continuation(runtime, monkeypatch):
    plan, state = policy(runtime, compact=True)
    original = execution._script
    calls = []
    def cancelled(argv, **kwargs):
        calls.append(argv[7])
        return dict(returncode=130, protocol=None, termination_reason='cancelled')
    monkeypatch.setattr(execution, '_script', lambda name: SimpleNamespace(execute=cancelled)
                        if name == 'heartbeat_process' else original(name))
    workspace, root, source, _ = runtime
    result = execution.execute(source, workspace, root, expected_binding=plan['binding'], expected_head=state['head'])
    assert result['outcome'] == 'cancelled' and calls == ['compact']
    assert result['accounting']['summary']['charged'] == plan['envelope']


def test_fresh_cli_rejects_explicit_context_before_prompt_or_state(runtime, monkeypatch):
    import threading
    from ls.core.agent.run_cli import execute
    workspace, root, source, value = runtime
    from ls.core.agent.run_cli import _PROFILE
    from ls.core.agent.coding_protocol import profile_digest
    from ls.core.agent.profiles import load, wire
    # Match the protected bootstrap before exercising fresh-session rejection.
    monkeypatch.setenv(_PROFILE, profile_digest(wire(load(Path(value['profiles']), value['profile']))))
    args = SimpleNamespace(profiles=Path(value['profiles']), profile=value['profile'],
        workspace=workspace, grant=Path(value['grant']), context=['src/context.md'], skill=[], image=[],
        require_new_session=True, retain_events=False)
    streams = SimpleNamespace(
        check=lambda: pytest.fail('unfiltered rejection must not initialize observations'),
        prompt=lambda: pytest.fail('must refuse before prompt'))
    with pytest.raises(ValueError, match='excludes explicit context'):
        execute(args, streams, threading.Event())
    assert not Path(value['state_root']).exists()
