"""Provider text projections preserve inputs and durable evidence boundaries."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading

import httpx2 as httpx
import pytest

from ls.core.agent.context_filters import ContextFilter
from ls.core.agent.coding_protocol import CodingHandler, profile_digest
from ls.core.agent.provider_client import ProviderBroker
from ls.core.agent.profiles import parse
from ls.core.output_filters import FilterError, defaults, parse_policy
from ls.tests.test_coding_protocol import payload, provider_options


def filtering(action='replace', **options):
    value = defaults()
    rule = dict(order=10, sinks=['agent-context'], match='word', pattern='secret', action=action)
    if action == 'replace':
        rule['replacement'] = 'hidden'
    value['rules'] = [rule]
    cancelled = options.pop('cancelled', threading.Event())
    return ContextFilter(parse_policy(value), options.pop('check', lambda: None),
                         cancelled, options.pop('authority', lambda: None))


def packet(api, text='secret output'):
    page = dict(content=text, encoding='utf-8', bytes=len(text.encode()), revision='a'*64, next_cursor='secret-cursor')
    user = dict(role='user', content='secret user instruction')
    if api == 'chat_completions':
        return dict(model='model', messages=[user,
            dict(role='assistant', content='secret assistant', tool_calls=[dict(id='secret-call', type='function',
                 function=dict(name='read_file', arguments='{"path":"secret input"}'))]),
            dict(role='tool', tool_call_id='secret-call', content=json.dumps(page))])
    return dict(model='model', input=[user,
        dict(type='message', role='assistant', id='secret-message', status='completed',
             content=[dict(type='output_text', text='secret assistant', annotations=[])]),
        dict(type='function_call', call_id='secret-call', name='read_file', arguments='{"path":"secret input"}'),
        dict(type='function_call_output', call_id='secret-call', output=json.dumps(page))])


def wire(value, profile):
    return dict(url=profile.endpoint, method='POST', body=base64.b64encode(json.dumps(value).encode()).decode())


def body(data):
    return json.loads(base64.b64decode(data['body']))


@pytest.mark.parametrize('api', ['chat_completions', 'responses'])
@pytest.mark.parametrize('action', ['replace', 'block', 'quarantine', 'halt'])
def test_trusted_handler_projects_final_send_without_changing_evidence(api, action):
    p = payload()
    p['profile']['api'] = api
    profile = parse(p['profile'])
    from types import SimpleNamespace
    tools = SimpleNamespace(profile=profile_digest(p['profile']), run_id=p['run_id'])
    cancelled, sent = threading.Event(), []
    context = filtering(action, cancelled=cancelled)
    handler = CodingHandler(tools, p, lambda _: None, lambda: None,
                            context_filter=context, **provider_options())
    handler.provider = ProviderBroker(profile, 'fixture', 128, transport=httpx.MockTransport(
        lambda request: (sent.append(json.loads(request.content)) or httpx.Response(200, content=b'{}'))))
    original = packet(api)
    data = wire(original, profile)
    saved = deepcopy(data)
    try:
        handler('run.start', {})
        if action == 'halt':
            with pytest.raises(InterruptedError):
                handler('provider.open', data)
            assert cancelled.is_set() and not sent
            return
        handler('provider.open', data)
        assert len(sent) == 1 and data == saved and original == packet(api)
        messages = sent[0]['messages' if api == 'chat_completions' else 'input']
        assert messages[0] == original['messages' if api == 'chat_completions' else 'input'][0]
        page = json.loads(messages[-1]['content' if api == 'chat_completions' else 'output'])
        assert page['content'] == ('hidden output' if action == 'replace' else '[Output withheld by filter.]')
        assert page['revision'] == 'a'*64 and page['next_cursor'] == 'secret-cursor'
        assert page['bytes'] == len('secret output')
        assert page['_localsetup_filters'][0]['action'] == ('transform' if action == 'replace' else action)
        call = messages[1]['tool_calls'][0]['function'] if api == 'chat_completions' else messages[2]
        assert call['arguments'] == '{"path":"secret input"}'
        assistant = messages[1]['content'] if api == 'chat_completions' else messages[1]['content'][0]['text']
        assert 'secret assistant' not in assistant and 'context filter:' in assistant
        assert handler.payload == p and handler.finished is None
    finally:
        handler.provider.close()


@pytest.mark.parametrize('result', [
    dict(operation='a'*32, status='completed', returncode=0, output=dict(stdout='secret', stderr='secret')),
    dict(directory='.', resources=[dict(path='AGENTS.md', content='secret', sha256='b'*64)], missing=[]),
    dict(files=[dict(path='a', sha256='b'*64)], matches=[dict(path='a', line=1, text='secret', text_truncated=False)], truncated=False),
])
def test_known_tool_text_leaves_are_filtered_on_a_copy(result):
    profile = parse(payload()['profile'])
    p = packet('chat_completions')
    p['messages'][-1]['content'] = json.dumps(result)
    observed = json.loads(body(filtering()(wire(p, profile), profile.api))['messages'][-1]['content'])
    assert 'secret' not in json.dumps(observed)
    assert 'secret' in json.dumps(result)
    assert observed['_localsetup_filters']


@pytest.mark.parametrize('change', ['binary', 'opaque', 'unknown_role', 'extension', 'duplicate', 'previous', 'annotations'])
def test_unsupported_output_refused_before_dispatch(change):
    profile = parse(payload()['profile'])
    value = packet(profile.api)
    if change == 'binary':
        page = json.loads(value['messages'][-1]['content']);page['encoding'] = 'base64'
        value['messages'][-1]['content'] = json.dumps(page)
    elif change == 'opaque':value['messages'][-1]['content'] = '{"unknown":"secret"}'
    elif change == 'unknown_role':value['messages'][-1]['role'] = 'extension'
    elif change == 'extension':value['messages'][1]['audio'] = {'data': 'opaque'}
    elif change == 'duplicate':value['messages'][-1]['content'] = '{"content":"a","content":"b"}'
    elif change == 'previous':value['previous_response_id'] = 'old'
    elif change == 'annotations':value['messages'][1]['content'] = [dict(type='text', text='a', annotations=[{'text':'secret'}])]
    with pytest.raises(FilterError, match='unsupported'):
        filtering()(wire(value, profile), profile.api)


def test_context_work_lines_and_authority_are_bounded(monkeypatch):
    from ls.core.agent import context_filters
    profile = parse(payload()['profile'])
    data = wire(packet(profile.api), profile)
    consumer = filtering()
    monkeypatch.setattr(context_filters, 'MAX_WORK', 35)
    consumer(data, profile.api)
    with pytest.raises(FilterError, match='limit'):
        consumer(data, profile.api)
    monkeypatch.setattr(context_filters, 'MAX_LINES', 1)
    with pytest.raises(FilterError, match='limit'):
        filtering()(data, profile.api)
    def denied():raise PermissionError('revoked')
    with pytest.raises(PermissionError, match='revoked'):
        filtering(authority=denied)(data, profile.api)


@pytest.mark.parametrize('budget', ['MAX_TEXT', 'MAX_UNITS'])
def test_context_projection_budget_failures_deliver_no_partial_view(monkeypatch, budget):
    from ls.core.agent import context_filters
    profile = parse(payload()['profile'])
    data = wire(packet(profile.api), profile)
    saved = deepcopy(data)
    monkeypatch.setattr(context_filters, budget, 1)
    with pytest.raises(FilterError, match='limit'):
        filtering()(data, profile.api)
    assert data == saved


def test_context_cancellation_and_elapsed_bound(monkeypatch):
    from ls.core.agent import context_filters
    profile = parse(payload()['profile'])
    data = wire(packet(profile.api), profile)
    cancel = threading.Event();cancel.set()
    with pytest.raises(InterruptedError):
        filtering(cancelled=cancel)(data, profile.api)
    ticks = iter((0, 2))
    monkeypatch.setattr(context_filters.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(FilterError, match='timeout'):
        filtering()(data, profile.api)


@pytest.mark.parametrize('item', [
    dict(type='reasoning', encrypted_content='opaque', summary=[]),
    dict(type='compaction', encrypted_content='opaque'),
    dict(type='function_call_output', call_id='call', output=[dict(type='input_text', text='secret')]),
])
def test_opaque_or_unsupported_responses_context_is_refused(item):
    profile = parse(dict(payload()['profile'], api='responses'))
    value = packet(profile.api);value['input'].append(item)
    with pytest.raises(FilterError, match='unsupported'):
        filtering()(wire(value, profile), profile.api)


def test_context_projection_preserves_pinned_sdk_history_and_final_wire():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-I', '-B', str(root/'ls/tests/context_filter_sdk_fixture.py'), str(root)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'apis': ['chat_completions', 'responses'], 'original_history_preserved': True}
