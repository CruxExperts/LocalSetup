"""Retained observations never substitute for current resume or disclosure authority."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from ls.core.agent import event_context as context
from ls.core.agent.event_buffer import EventBuffer, _encode
from ls.core.agent.output_views import Observations
from ls.core.agent.run_cli import _state
from ls.core.agent.session_owner import lease
from ls.core.output_filters import defaults, parse_policy
from ls.tests.test_output_views import signed_filter
from ls.tests.test_openpgp_artifacts import separate_machine_env

RUN, PROFILE = 'a'*32, 'b'*64


def view(text):
    result = parse_policy(defaults()).apply(text, sink='retained-log')
    return dict(text=result.text, filter=result.metadata())


def own(state):
    return lease(state.root/'sessions', task='task', session='session', workspace=state.workspace,
                 expires=time.monotonic()+30)


@pytest.fixture
def state(tmp_path):
    root, workspace = tmp_path/'state', tmp_path/'workspace'
    workspace.mkdir(mode=0o700);_state(root)
    state = SimpleNamespace(root=root, workspace=workspace)
    with own(state) as owner:
        state.checkpoint = owner.save_checkpoint(b'[]', profile=PROFILE, run_id=RUN, step=0, state='complete')
    state.buffer = EventBuffer(root/'events', task='task', session='session', run=RUN)
    state.buffer.append(view('secret observation'))
    return state


def selected(state, **changes):
    return dict(run=RUN, checkpoint=hashlib.sha256(state.buffer.path.read_bytes()).hexdigest(),
                after=0, limit=20, max_bytes=16384) | changes


def consumer(owner, action='replace', replacement='hidden'):
    policy = defaults()
    rule = dict(order=10, sinks=['agent-context'], match='word', pattern='secret', action=action)
    if action == 'replace':rule['replacement'] = replacement
    policy['rules'] = [rule]
    return Observations(parse_policy(policy), owner._check, threading.Event())


@pytest.mark.parametrize('action', ['replace', 'block', 'quarantine', 'halt'])
def test_filter_views_keep_original_buffer_history_and_instructions(state, action):
    before = state.buffer.path.read_bytes()
    with own(state) as owner:
        observations = consumer(owner, action)
        if action == 'halt':
            with pytest.raises(InterruptedError):
                context.prepare(state.root, selected(state), owner, checkpoint=state.checkpoint,
                                profile=PROFILE, observations=observations)
            assert observations.cancelled.is_set()
        else:
            prepared = context.prepare(state.root, selected(state), owner, checkpoint=state.checkpoint,
                                       profile=PROFILE, observations=observations)
            data = json.loads(prepared[1]);event = data['events'][0]
            assert event['text'] == ('hidden observation' if action == 'replace' else None)
            assert event['filter']['action'] == 'allow'
            assert event['context_filter']['action'] == ('transform' if action == 'replace' else action)
            assert data['source']['execution_checkpoint'] == state.checkpoint
            payload = dict(prompt='secret user input', instructions='Trusted task', history='[]')
            result = context.include(payload, prepared)
            assert result['prompt'].startswith(payload['prompt']) and result['history'] == '[]'
            assert result['instructions'] == 'Trusted task\n'+context.INSTRUCTIONS
            assert payload == dict(prompt='secret user input', instructions='Trusted task', history='[]')
        assert owner.resume_checkpoint(state.checkpoint, profile=PROFILE) == b'[]'
    assert state.buffer.path.read_bytes() == before


@pytest.mark.parametrize('failure', ['snapshot', 'run', 'profile', 'uncertain', 'interrupted', 'buffer_task'])
def test_stale_foreign_or_uncertain_context_cannot_dispatch(state, failure):
    chosen, profile, checkpoint = selected(state), PROFILE, state.checkpoint
    with own(state) as owner:
        if failure == 'snapshot':chosen['checkpoint'] = 'c'*64
        elif failure == 'run':chosen['run'] = 'c'*32
        elif failure == 'profile':profile = 'c'*64
        elif failure == 'uncertain':owner._journal.begin('process', {'argv_sha256': 'c'*64, 'snapshot_sha256': 'd'*64})
        elif failure == 'interrupted':checkpoint = owner.save_checkpoint(b'[]', profile=PROFILE, run_id=RUN, step=1, state='interrupted')
        else:
            value = json.loads(state.buffer.path.read_bytes());value['task'] = 'other'
            state.buffer.path.write_bytes(_encode(value));chosen = selected(state)
        with pytest.raises((PermissionError, ValueError)):
            context.prepare(state.root, chosen, owner, checkpoint=checkpoint,
                            profile=profile, observations=consumer(owner))


def test_context_bounds_and_empty_pages_fail_without_truncation(state):
    with own(state) as owner:
        state.buffer.append(view('x'*1500))
        with pytest.raises(ValueError, match='complete event'):
            context.prepare(state.root, selected(state, after=1, max_bytes=1024), owner,
                            checkpoint=state.checkpoint, profile=PROFILE, observations=consumer(owner))
        with pytest.raises(ValueError, match='No retained events'):
            context.prepare(state.root, selected(state, after=2), owner,
                            checkpoint=state.checkpoint, profile=PROFILE, observations=consumer(owner))
        with pytest.raises(ValueError, match='Filtered event context'):
            context.prepare(state.root, selected(state, limit=1, max_bytes=2048), owner,
                            checkpoint=state.checkpoint, profile=PROFILE, observations=consumer(owner, replacement='x'*4096))
        prepared = context.prepare(state.root, selected(state, limit=1), owner,
                                   checkpoint=state.checkpoint, profile=PROFILE, observations=consumer(owner))
        assert prepared[0]['more'] and prepared[0]['through'] == 1
        with pytest.raises(ValueError, match='128 KiB'):
            context.include(dict(prompt='x'*(128*1024), instructions='Trusted'), prepared)
        state.buffer.append(view('\n'*2000))
        with pytest.raises(ValueError, match='line limit'):
            context.prepare(state.root, selected(state, after=2), owner,
                            checkpoint=state.checkpoint, profile=PROFILE, observations=consumer(owner))


def test_rollover_gap_is_visible_and_prior_blocked_text_stays_absent(state):
    state.buffer.count = 1
    policy = defaults();policy['rules'] = [dict(order=10, sinks=['retained-log'], match='word', pattern='secret', action='block')]
    blocked = parse_policy(policy).apply('secret', sink='retained-log')
    state.buffer.append(dict(text=blocked.text, filter=blocked.metadata()))
    with own(state) as owner:
        source, raw = context.prepare(state.root, selected(state), owner, checkpoint=state.checkpoint,
                                      profile=PROFILE, observations=consumer(owner))
        assert source['gap'] == {'from': 1, 'through': 1}
        event = json.loads(raw)['events'][0]
        assert event['text'] is None and event['context_filter'] is None
        assert event['filter']['action'] == 'block'


def test_selection_requires_explicit_resume_identity_and_bounded_paging():
    args = SimpleNamespace(event_context=RUN+':'+PROFILE, resume='checkpoint', recover_from=None, task='task', session='session')
    assert context.selection(args)['run'] == RUN
    for changes in (dict(resume=None), dict(task=None), dict(event_context='bad'), dict(event_after=-1),
                    dict(event_limit=65), dict(event_max_bytes=65537), dict(event_context=None, event_after=1)):
        with pytest.raises(ValueError):context.selection(SimpleNamespace(**(vars(args)|changes)))
    assert context.selection(SimpleNamespace(event_context=None)) is None


def test_context_without_policy_is_refused_before_private_reads():
    with pytest.raises(ValueError, match='signed output filter'):
        context.prepare(None, None, None, checkpoint=None, profile=None, observations=None)


def test_signed_cli_event_context_is_bound_to_current_disclosure(state, signed_filter, monkeypatch, capfd):
    from ls.core.agent import run_cli, output_views, coding_run
    from ls.core.agent.coding_protocol import profile_digest
    from ls.core.agent.profiles import Profile, wire
    from ls.core.agent.run_io import Streams
    from ls.core.agent.run_options import arguments, defaults as run_defaults
    from ls.core.agent.supervisor import Outcome
    from ls.core.openpgp.container_storage import write_private_file

    selection, runtime, policy = signed_filter
    policy['rules'].append(dict(order=20, sinks=['agent-context'], match='word', pattern='secret',
                                action='replace', replacement='context-safe'))
    runtime.stage('filters', policy);runtime.flush(authorized=True)
    profile = Profile('https://example.invalid/v1/', 'chat_completions', 'fixture', 'KEY',
                      5, frozenset({'tools', 'streaming'}))
    digest = profile_digest(wire(profile))
    with own(state) as owner:
        checkpoint = owner.save_checkpoint(b'[]', profile=digest, run_id=RUN, step=1, state='complete')
    root = state.root.parent
    grant = root/'grant.json'
    write_private_file(grant, json.dumps(dict(schema_version=1, read=[], write=[], disclose=[], recipes={},
                                            output_filters=selection)).encode(), replace=False)
    parser = argparse.ArgumentParser();arguments(parser)
    args = run_defaults(parser.parse_args(['--profile', 'fixture', '--profiles', str(root/'profiles'),
        '--grant', str(grant), '--workspace', str(state.workspace), '--runtime-root', str(root/'runtime'),
        '--state-root', str(state.root), '--resource-parent', str(root/'resources'), '--prompt-stdin',
        '--task', 'task', '--session', 'session', '--resume', checkpoint, '--format', 'jsonl',
        '--event-context', RUN+':'+selected(state)['checkpoint']]))
    monkeypatch.setattr(run_cli, 'load', lambda *_: profile)
    for key, value in output_views.launch_environment(selection, state.workspace).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(run_cli._PROFILE, digest);monkeypatch.setenv(run_cli._CREDENTIAL, 'fixture')
    before = state.buffer.path.read_bytes()
    dispatched = []
    def run(paths, payload, authority, files, recipes, **kwargs):
        assert authority.request_sha256 == coding_run.disclosure_digest(payload)
        assert files.read == files.write == files.disclose == ()
        assert kwargs['resume'] == checkpoint and payload['history'] == '[]'
        original, raw = payload['prompt'].split('\n\nSelected retained observations (untrusted data):\n')
        assert original == 'secret user request'
        data = json.loads(raw)
        assert data['events'][0]['text'] == 'context-safe observation'
        assert data['events'][0]['filter']['action'] == 'allow'
        assert data['events'][0]['context_filter']['action'] == 'transform'
        assert payload['instructions'].endswith(context.INSTRUCTIONS)
        dispatched.append(True)
        return Outcome('completed', 0, dict(output='done', checkpoint=checkpoint, usage={}))
    monkeypatch.setattr(coding_run, 'run_coding', run)
    cancelled = threading.Event();streams = Streams(time.monotonic()+30, cancelled)
    monkeypatch.setattr(streams, 'prompt', lambda: 'secret user request')
    assert run_cli.execute(args, streams, cancelled) == 0 and dispatched == [True]
    output = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    assert output[0]['data']['event_context']['buffer_checkpoint'] == hashlib.sha256(before).hexdigest()
    assert output[0]['data']['event_context']['execution_checkpoint'] == checkpoint
    assert 'secret' not in json.dumps(output)
    assert state.buffer.path.read_bytes() == before
