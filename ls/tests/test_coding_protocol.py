import json
from types import SimpleNamespace
from pathlib import Path
import time

import pytest
from ls.core.agent.coding_protocol import CodingHandler,request,profile_digest,terminal
from ls.core.agent.resource_group import Limits


def provider_options():
    return {'provider_task':'task','provider_session':'session',
            'provider_deadline':time.monotonic()+60,
            'provider_resource_parent':Path('/sys/fs/cgroup/fixture'),
            'provider_limits':Limits()}


def payload():
    return {'schema_version':1,'run_id':'run','profile':{'base_url':'https://example.com/v1/','api':'chat_completions','model':'model',
        'credential_env':'KEY','timeout_seconds':2,'capabilities':['tools','streaming'],'allow_loopback_http':False},
        'credential':'fixture','prompt':'Edit fixture','instructions':'Use granted tools','history':None,'request_limit':8,'tool_limit':16,'token_limit':32768}


def handler():
    p=payload();checks=[];events=[]
    owner=SimpleNamespace(_checkpoint=lambda x:{'run_id':'run'},resume_checkpoint=lambda *a,**k:checks.append('checkpoint'))
    tools=SimpleNamespace(profile=profile_digest(p['profile']),run_id='run',owner=owner)
    return CodingHandler(tools,p,events.append,lambda:checks.append('authority'),**provider_options()),events,checks


def test_start_stream_checkpoint_and_process_receipt():
    h,events,checks=handler()
    with pytest.raises(ValueError,match='start'):h('stream.event',{'event':{}})
    start=h('run.start',{})
    assert 'credential' not in start and request(start,worker=True).model=='model'
    assert '"credential": "fixture"' not in json.dumps(start)
    with pytest.raises(ValueError,match='once'):h('run.start',{})
    assert h('stream.event',{'event':{'text':'hello'}})=={'accepted':1}
    result={'checkpoint':'a'*64,'output':'done','usage':{'requests':1}}
    assert h('run.finish',result)=={'checkpoint':'a'*64} and 'checkpoint' in checks
    assert terminal(json.dumps({'schema_version':1,'status':'completed','checkpoint':'a'*64}).encode(),h.finished)==result
    with pytest.raises(ValueError,match='already'):h('stream.event',{'event':{}})
    with pytest.raises(ValueError):terminal(b'{}',h.finished)


@pytest.mark.parametrize('field,value',[('credential','bad token'),('request_limit',True),('tool_limit',257),('history',4),('instructions','')])
def test_request_refuses_invalid_authority_inputs(field,value):
    with pytest.raises(ValueError):request(payload()|{field:value})


def test_profile_and_foreign_result_checkpoint_refused():
    h,_,_=handler();p=payload();p['profile']['model']='other'
    with pytest.raises(PermissionError):CodingHandler(h.tools,p,lambda x:None,lambda:None,**provider_options())
    h('run.start',{});h.tools.owner._checkpoint=lambda x:{'run_id':'other'}
    with pytest.raises(PermissionError):h('run.finish',{'checkpoint':'a'*64,'output':'done','usage':{}})
    assert h.finished is None


def test_stream_overflow_does_not_reach_sink():
    h,events,_=handler();h('run.start',{})
    with pytest.raises(ValueError,match='budget'):h('stream.event',{'event':{'text':'x'*(1024*1024)}})
    assert not events


def test_worker_request_rejects_credential_and_broker_rejects_unsolicited_provider_method():
    p=payload()
    with pytest.raises(ValueError):request(p,worker=True)
    h,_,_=handler()
    with pytest.raises(ValueError,match='start'):h('provider.next',{})
    h('run.start',{})
    with pytest.raises(ValueError,match='open'):h('provider.next',{})


@pytest.mark.parametrize('method', ['file.read', 'file.write', 'file.search', 'file.list', 'context.refresh', 'process.run'])
def test_zero_tool_budget_blocks_broker_and_approval_but_allows_checkpoint(method):
    from ls.core.agent.checkpoint_rpc import METHOD
    p = payload() | {'tool_limit': 0}
    calls = []
    class Tools:
        profile, run_id, recipes = profile_digest(p['profile']), p['run_id'], {}
        def __call__(self, name, data):
            calls.append(name)
            return {'ok': True}
    h = CodingHandler(Tools(), p, lambda _: None, lambda: None,
                      approve=lambda *_: pytest.fail('budget refusal must precede approval'), **provider_options())
    h('run.start', {})
    with pytest.raises(PermissionError, match='budget'):
        h(method, {})
    assert calls == []
    assert h(METHOD, {}) == {'ok': True} and calls == [METHOD]


def test_supervisor_tool_budget_counts_attempts_across_methods_and_failures():
    p = payload() | {'tool_limit': 2}
    calls = []
    class Tools:
        profile, run_id = profile_digest(p['profile']), p['run_id']
        def __call__(self, name, data):
            calls.append(name)
            if name == 'file.write':
                raise ValueError('Invalid tool input')
            return {'ok': True}
    h = CodingHandler(Tools(), p, lambda _: None, lambda: None, **provider_options())
    h('run.start', {})
    assert h('file.read', {}) == {'ok': True}
    with pytest.raises(ValueError):
        h('file.write', {})
    with pytest.raises(PermissionError, match='budget'):
        h('process.run', {})
    assert calls == ['file.read', 'file.write']
