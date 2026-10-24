"""Real signed policy binding and complete-text delivery through the run CLI."""
import argparse
import json
from pathlib import Path
import threading
import time

import pytest

from ls.core.agent import output_views as views, run_cli, coding_run
from ls.core.agent.coding_protocol import profile_digest
from ls.core.agent.profiles import Profile, wire
from ls.core.agent.run_io import Streams
from ls.core.agent.run_options import arguments, defaults
from ls.core.agent.supervisor import Outcome
from ls.core.live_configuration import ConfigurationError
from ls.core.live_configuration_cli import bound_runtime
from ls.core.openpgp.container_storage import write_private_file
from ls.core.output_filters import FilterError, defaults as filter_defaults, parse_policy
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


@pytest.fixture
def signed_filter(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    trust = env.snapshots['controller']
    binding = dict(schema='localsetup.configuration-authority', version=1,
        root=str(tmp_path/'configuration'), store_id='6'*32, signer=env.owner,
        recipients=[env.owner], gnupg_home=str(env.homes['controller']), write=True,
        secret=dict(provider='env', name=SECRET_REFERENCE.name, protected_file_root=None),
        trust=dict(path=str(env.paths['controller']), store_id=trust.store_id,
                   revision=trust.revision, epoch=trust.epoch, scope=list(trust.scope)),
        owners={'filters': {'kind': 'output-filters'}})
    path = tmp_path/'authority.json'
    write_private_file(path, json.dumps(binding).encode(), replace=False)
    monkeypatch.setenv(SECRET_REFERENCE.name, env.resolver.resolve(SECRET_REFERENCE))
    runtime, *_ = bound_runtime(path, source_root=Path(__file__).resolve().parents[2], home=tmp_path)
    policy = filter_defaults()
    policy['rules'] = [dict(order=10, sinks=['observation'], match='word',
                            pattern='secret', action='replace', replacement='hidden')]
    runtime.stage('filters', policy)
    runtime.flush(authorized=True)
    return dict(authority_file=str(path), owner='filters'), runtime, policy


def test_real_signed_startup_pin_revocation_and_unavailable_sinks(tmp_path, signed_filter, monkeypatch):
    selected, runtime, policy = signed_filter
    workspace = tmp_path/'workspace';workspace.mkdir()
    environment = views.launch_environment(selected, workspace)
    assert set(environment) == {views.ENVIRONMENT}
    cancelled = threading.Event()
    with monkeypatch.context() as protected:
        protected.setattr(views.os, 'environ', {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', **environment})
        with pytest.raises(ConfigurationError, match='configuration_binding_changed'):
            views.protected(None, workspace, lambda: None, cancelled)
        consumer = views.protected(selected, workspace, lambda: None, cancelled)
        assert views.ENVIRONMENT not in views.os.environ
        assert consumer.text('secret')['text'] == 'hidden'
    policy['rules'][0]['replacement'] = 'changed'
    runtime.stage('filters', policy);runtime.flush(authorized=True)
    with monkeypatch.context() as protected:
        protected.setattr(views.os, 'environ', {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', **environment})
        with pytest.raises(ConfigurationError, match='configuration_binding_changed'):
            views.protected(selected, workspace, lambda: None, cancelled)
    # Before the shared cadence, the running consumer retains its accepted policy.
    assert consumer.text('secret')['text'] == 'hidden'
    policy['rules'][0]['sinks'].append('retained-log')
    runtime.stage('filters', policy);runtime.flush(authorized=True)
    with pytest.raises(FilterError, match='filter_sink_unavailable'):
        views.launch_environment(selected, workspace)
    path = Path(selected['authority_file'])
    binding = json.loads(path.read_bytes());binding['write'] = False
    write_private_file(path, json.dumps(binding).encode(), replace=True)
    with pytest.raises(ConfigurationError, match='configuration_unauthorized'):
        consumer.text('secret')
    with pytest.raises(ValueError, match='separate'):
        views.selection(selected, tmp_path)


@pytest.mark.parametrize('action', ['replace', 'block', 'quarantine', 'halt'])
def test_complete_parts_terminal_actions_and_no_partial_side_channels(action):
    policy = filter_defaults()
    rule = dict(order=10, sinks=['observation'], match='word', pattern='secret', action=action)
    if action == 'replace':rule['replacement'] = 'hidden'
    policy['rules'] = [rule]
    cancelled = threading.Event()
    consumer = views.Observations(parse_policy(policy), lambda: None, cancelled)
    for event in [dict(event_kind='part_start', part=dict(part_kind='text', content='sec')),
                  dict(event_kind='part_delta', delta=dict(content_delta='ret')),
                  dict(event_kind='part_end', part=dict(part_kind='tool-call', args='secret')),
                  dict(event_kind='extension', content='secret')]:
        assert consumer.progress(event) is None
    complete = dict(event_kind='part_end', part=dict(part_kind='text', content='secret'))
    if action == 'halt':
        with pytest.raises(InterruptedError):consumer.progress(complete)
        assert cancelled.is_set()
    else:
        result = consumer.progress(complete)
        assert result['text'] == ('hidden' if action == 'replace' else None)
        assert 'secret' not in json.dumps(result)
        assert '; rules: 10]' in views.render(result)
        assert '\x1b' not in views.render(dict(result, text='\x1b[2J'))
    cancelled.set()
    def check():
        if cancelled.is_set():raise InterruptedError
    consumer.check = check
    with pytest.raises(InterruptedError):consumer.progress(complete)


@pytest.mark.parametrize('format,retain', [('jsonl', False), ('text', False), ('jsonl', True)])
def test_run_cli_filters_stream_and_final_without_mutating_protocol(tmp_path, signed_filter, monkeypatch, capfd, format, retain):
    selected, runtime, policy = signed_filter
    policy['rules'][0]['sinks'].append('agent-context')
    if retain:
        policy['rules'].append(dict(order=20, sinks=['retained-log'], match='word', pattern='secret',
                                    action='replace', replacement='recorded'))
    runtime.stage('filters', policy);runtime.flush(authorized=True)
    workspace = tmp_path/'workspace';workspace.mkdir()
    grant = tmp_path/'grant.json'
    write_private_file(grant, json.dumps(dict(schema_version=1, read=[], write=[], disclose=[], recipes={},
                                            output_filters=selected)).encode(), replace=False)
    parser = argparse.ArgumentParser();arguments(parser)
    args = defaults(parser.parse_args(['--profile', 'fixture', '--profiles', str(tmp_path/'profiles'),
        '--grant', str(grant), '--workspace', str(workspace), '--runtime-root', str(tmp_path/'runtime'),
        '--state-root', str(tmp_path/'state'), '--resource-parent', str(tmp_path/'resources'),
        '--prompt-stdin', '--format', format]+(['--retain-events'] if retain else [])))
    profile = Profile('https://example.invalid/v1/', 'chat_completions', 'fixture', 'FIXTURE_KEY',
                      1, frozenset({'tools', 'streaming'}))
    monkeypatch.setattr(run_cli, 'load', lambda *args: profile)
    for name, value in views.launch_environment(selected, workspace, retain=retain).items():monkeypatch.setenv(name, value)
    monkeypatch.setenv(run_cli._PROFILE, profile_digest(wire(profile)))
    monkeypatch.setenv(run_cli._CREDENTIAL, 'fixture-credential')
    outcome = dict(output='secret', checkpoint='a'*64, usage={})
    from ls.core.agent.event_delivery import Delivery
    delivery = Delivery()
    if retain:delivery.paused.set()
    def run(paths, payload, authority, files, recipes, *, on_event, **kwargs):
        assert kwargs['on_tick'].__self__ is kwargs['context_filter'].refresh.__self__
        kwargs['on_tick']()
        assert payload['prompt'] == 'user secret input'
        assert payload['credential'] == 'fixture-credential'
        assert files.read == files.write == files.disclose == ()
        import base64
        packet = {'messages': [{'role': 'user', 'content': payload['prompt']},
                               {'role': 'assistant', 'content': 'secret'}]}
        data = dict(url=profile.endpoint, method='POST', body=base64.b64encode(json.dumps(packet).encode()).decode())
        projected = kwargs['context_filter'](data, profile.api)
        sent = json.loads(base64.b64decode(projected['body']))
        assert sent['messages'][0] == packet['messages'][0]
        assert sent['messages'][1]['content'].startswith('hidden\n[LocalSetup context filter: ')
        assert packet['messages'][1]['content'] == 'secret'
        on_event(dict(event_kind='part_start', part=dict(part_kind='text', content='sec')))
        on_event(dict(event_kind='part_delta', delta=dict(content_delta='ret')))
        on_event(dict(event_kind='part_end', part=dict(part_kind='text', content='secret')))
        return Outcome('completed', 0, outcome)
    monkeypatch.setattr(coding_run, 'run_coding', run)
    cancelled = threading.Event()
    streams = Streams(time.monotonic()+20, cancelled)
    monkeypatch.setattr(streams, 'prompt', lambda: 'user secret input')
    assert run_cli.execute(args, streams, cancelled, events=delivery) == 0
    output = capfd.readouterr().out
    assert 'secret' not in output and 'fixture-credential' not in output
    if format == 'jsonl':
        events = [json.loads(line) for line in output.splitlines()]
        if retain:
            assert [event['type'] for event in events] == ['start', 'result']
            assert events[-1]['data']['output'] is None and events[-1]['data']['output_paused']
            reference = events[0]['data']['event_buffer']
            from ls.core.agent.cli import main
            assert main(['events', '--state-root', str(args.state_root), '--task', reference['task'],
                         '--session', reference['session'], '--run', reference['run']]) == 0
            page = json.loads(capfd.readouterr().out)
            assert [event['text'] for event in page['events']] == ['recorded', 'recorded']
            assert page['events'][-1]['checkpoint'] == 'a'*64
            assert b'secret' not in delivery.buffer.path.read_bytes()
        else:
            assert [event['type'] for event in events] == ['start', 'text_view', 'result']
            assert events[1]['data']['text'] == events[-1]['data']['output'] == 'hidden'
        assert events[-1]['data']['checkpoint'] == 'a'*64
    else:
        assert output == 'hidden\n[Filter: transform; rules: 10]\n'*2
    assert outcome == dict(output='secret', checkpoint='a'*64, usage={})
