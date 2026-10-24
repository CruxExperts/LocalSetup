"""Pinned SDK serialization through filtered broker transport, without a network."""
import asyncio
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, sys.argv[1])
from ls.core.agent.sdk_imports import activate
finder = activate(Path(sys.argv[1])/'vendor/lscli')
from ls.core.agent.sdk_models import model
from ls.core.agent.context_filters import ContextFilter
from ls.core.agent.provider_client import BrokerTransport, ProviderBroker
from ls.core.agent.profiles import parse
from ls.core.output_filters import defaults, parse_policy
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, ToolCallPart, ToolReturnPart, TextPart, ModelMessagesTypeAdapter
from pydantic_ai.models import ModelRequestParameters
import httpx2 as httpx


async def main():
    apis = []
    for api in ('chat_completions', 'responses'):
        profile = parse(dict(base_url='https://fixture.invalid/v1/', api=api, model='fixture',
                             credential_env='KEY', timeout_seconds=5, capabilities=['tools'], allow_loopback_http=False))
        policy = defaults()
        policy['rules'] = [dict(order=10, sinks=['agent-context'], match='word', pattern='secret', action='replace', replacement='hidden')]
        filters = ContextFilter(parse_policy(policy), lambda: None, threading.Event(), lambda: None)
        calls = []
        def receive(request):
            value = json.loads(request.content)
            calls.append(value)
            assert request.headers['authorization'] == 'Bearer host-fixture'
            messages = value['messages' if api == 'chat_completions' else 'input']
            tools = [m for m in messages if m.get('role') == 'tool' or m.get('type') == 'function_call_output']
            assert len(tools) == 1
            result = json.loads(tools[0]['content' if api == 'chat_completions' else 'output'])
            assert result['content'] == 'hidden output' and result['revision'] == 'a'*64
            assert result['_localsetup_filters'][0]['action'] == 'transform'
            assert 'secret user' in json.dumps(value) and 'secret argument' in json.dumps(value)
            assert 'secret assistant' not in json.dumps(value)
            if api == 'chat_completions':
                response = dict(id='fixture', object='chat.completion', created=1, model='fixture',
                                choices=[dict(index=0, message=dict(role='assistant', content='done'), finish_reason='stop')])
            else:
                response = dict(id='fixture', object='response', created_at=1, model='fixture', status='completed',
                                output=[dict(type='message', id='message', status='completed', role='assistant',
                                             content=[dict(type='output_text', text='done', annotations=[])])])
            return httpx.Response(200, json=response)
        broker = ProviderBroker(profile, 'host-fixture', 128, transport=httpx.MockTransport(receive))
        class Channel:
            async def request_async(self, method, data):
                return broker(method, filters(data, api) if method == 'provider.open' else data)
        result = dict(content='secret output', encoding='utf-8', bytes=13, revision='a'*64, next_cursor=None)
        messages = [ModelRequest(parts=[UserPromptPart('secret user')]),
                    ModelResponse(parts=[TextPart('secret assistant'),
                                         ToolCallPart('read_file', {'path': 'secret argument'}, tool_call_id='call_fixture')]),
                    ModelRequest(parts=[ToolReturnPart('read_file', result, tool_call_id='call_fixture')])]
        original = ModelMessagesTypeAdapter.dump_json(messages)
        try:
            async with model(profile, {'KEY': 'worker-placeholder'}, finder,
                             transport=BrokerTransport(Channel(), profile)) as adapter:
                answer = await adapter.request(messages, None, ModelRequestParameters())
                assert ''.join(part.content for part in answer.parts if isinstance(part, TextPart)) == 'done'
            assert len(calls) == 1 and ModelMessagesTypeAdapter.dump_json(messages) == original
            assert result['content'] == 'secret output'
            apis.append(api)
        finally:
            broker.close()
    finder.verify_origins()
    print(json.dumps(dict(apis=apis, original_history_preserved=True)))


asyncio.run(main())
