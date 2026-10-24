"""The coding worker's provider path ends at a trusted, bounded broker."""
import asyncio
import base64
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

import httpx2 as httpx
import pytest

from ls.core.agent.profiles import parse
from ls.core.agent import provider_client
from ls.core.agent.provider_client import BrokerTransport, ProviderBroker, SupervisedProvider
from ls.core.agent.resource_group import Limits


def profile():
    return parse({'base_url':'http://127.0.0.1:18181/v1/','api':'chat_completions',
                  'model':'selected-model','credential_env':'PROVIDER_KEY',
                  'timeout_seconds':2,'capabilities':['tools','streaming'],
                  'allow_loopback_http':True})


def packet(value):
    return {'url':profile().endpoint,'method':'POST',
            'body':base64.b64encode(json.dumps(value).encode()).decode()}


def test_trusted_broker_binds_model_secret_endpoint_and_sse():
    seen=[]
    def remote(request):
        seen.append(request)
        return httpx.Response(200,headers={'content-type':'text/event-stream',
                                            'set-cookie':'should-not-cross'},
                              content=b'data: {"ok":true}\n\ndata: [DONE]\n\n')
    broker=ProviderBroker(profile(),'host-only-key',128,transport=httpx.MockTransport(remote))
    try:
        opened=broker('provider.open',packet({'model':'selected-model','stream':True}))
        assert opened=={'status':200,'content_type':'text/event-stream'}
        chunks=[]
        while True:
            part=broker('provider.next',{})
            if part['eof']:break
            chunks.append(base64.b64decode(part['chunk']))
        assert b''.join(chunks)==b'data: {"ok":true}\n\ndata: [DONE]\n\n'
        assert len(seen)==1
        assert seen[0].headers['Authorization']=='Bearer host-only-key'
        assert seen[0].url==httpx.URL(profile().endpoint)
        assert json.loads(seen[0].content)['max_completion_tokens']==128
    finally:
        broker.close()


@pytest.mark.parametrize('change,error',[
    ({'method':'GET'},PermissionError),({'url':'http://127.0.0.1:18181/other'},PermissionError),
])
def test_provider_destination_and_method_rejected_before_send(change,error):
    seen=[]
    broker=ProviderBroker(profile(),'host-only-key',128,
                          transport=httpx.MockTransport(lambda request:seen.append(request)))
    with pytest.raises(error):broker('provider.open',packet({'model':'selected-model'})|change)
    assert not seen


@pytest.mark.parametrize('value',[
    {'model':'different'}, {'model':'selected-model','max_completion_tokens':129},
    {'model':'selected-model','max_output_tokens':1},
    {'model':'selected-model','temperature':0.5},
])
def test_provider_parameters_rejected_before_send(value):
    seen=[]
    broker=ProviderBroker(profile(),'host-only-key',128,
                          transport=httpx.MockTransport(lambda request:seen.append(request)))
    with pytest.raises((ValueError,PermissionError)):
        broker('provider.open',packet(value))
    assert not seen


def test_responses_api_rejects_chat_caps_and_injects_its_own_bound():
    selected=replace(profile(),api='responses')
    seen=[]
    def remote(request):
        seen.append(request)
        return httpx.Response(200,content=b'{}')
    broker=ProviderBroker(selected,'host-only-key',128,transport=httpx.MockTransport(remote))
    packet={'url':selected.endpoint,'method':'POST',
            'body':base64.b64encode(b'{"model":"selected-model","max_tokens":1}').decode()}
    with pytest.raises(ValueError,match='another API'):broker('provider.open',packet)
    assert not seen
    packet['body']=base64.b64encode(b'{"model":"selected-model"}').decode()
    broker('provider.open',packet)
    assert json.loads(seen[0].content)['max_output_tokens']==128
    broker.close()


def test_nullable_upstream_chat_cap_is_replaced_with_authorized_bound():
    seen=[]
    def remote(request):
        seen.append(request)
        return httpx.Response(200,content=b'{}')
    broker=ProviderBroker(profile(),'host-only-key',128,transport=httpx.MockTransport(remote))
    broker('provider.open',packet({'model':'selected-model','max_completion_tokens':None}))
    assert json.loads(seen[0].content)['max_completion_tokens']==128
    broker.close()


def test_revoked_final_send_and_request_count_are_enforced():
    seen=[]
    def remote(request):
        seen.append(request)
        return httpx.Response(200,content=b'{}')
    def revoked():
        raise PermissionError('revoked')
    broker=ProviderBroker(profile(),'host-only-key',128,
                          transport=httpx.MockTransport(remote),check=revoked)
    with pytest.raises(PermissionError,match='revoked'):
        broker('provider.open',packet({'model':'selected-model'}))
    assert not seen
    broker=ProviderBroker(profile(),'host-only-key',128,
                          transport=httpx.MockTransport(remote),request_limit=1)
    broker('provider.open',packet({'model':'selected-model'}))
    while not broker('provider.next',{})['eof']:
        pass
    with pytest.raises(PermissionError,match='count'):
        broker('provider.open',packet({'model':'selected-model'}))
    assert len(seen)==1


def test_worker_transport_forwards_body_only_and_streams_without_host_key():
    class Channel:
        def __init__(self):self.calls=[]
        async def request_async(self,method,data):
            self.calls.append((method,data))
            if method=='provider.open':return {'status':200,'content_type':'text/event-stream'}
            if method=='provider.next':
                return {'chunk':base64.b64encode(b'data: [DONE]\n\n').decode(),'eof':False} if len(self.calls)==2 else {'chunk':'','eof':True}
            raise AssertionError(method)
    async def read():
        channel=Channel();transport=BrokerTransport(channel,profile())
        request=httpx.Request('POST',profile().endpoint,
                              headers={'authorization':'Bearer synthetic-key','x-host-secret':'bad'},
                              content=b'{"model":"selected-model"}')
        response=await transport.handle_async_request(request)
        chunks=[chunk async for chunk in response.aiter_bytes()]
        assert b''.join(chunks)==b'data: [DONE]\n\n'
        assert channel.calls[0][0]=='provider.open'
        assert channel.calls[0][1]['url']==profile().endpoint
        assert channel.calls[0][1]['method']=='POST'
        assert json.loads(base64.b64decode(channel.calls[0][1]['body']))=={'model':'selected-model'}
        assert 'synthetic-key' not in str(channel.calls) and 'x-host-secret' not in str(channel.calls)
    asyncio.run(read())


@contextmanager
def loopback_stream(mode):
    entered, first, release = threading.Event(), threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            entered.set()
            if mode == 'headers':
                release.wait(5)
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                if mode != 'read':
                    event=b'data: hello\n\n'
                    self.wfile.write(f'{len(event):X}\r\n'.encode()+event+b'\r\n')
                    self.wfile.flush()
                    first.set()
                if mode == 'drip':
                    for _ in range(30):
                        if release.wait(.04):break
                        event=b'data: more\n\n'
                        self.wfile.write(f'{len(event):X}\r\n'.encode()+event+b'\r\n')
                        self.wfile.flush()
                else:
                    release.wait(5)
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            except OSError:
                pass  # The supervised child may close its connection on revocation.
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield server.server_port, entered, first, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def supervised_resources(monkeypatch):
    @contextmanager
    def resource(*args):
        read_fd, write_fd = os.pipe()
        class Group:
            @contextmanager
            def membership(self):
                yield write_fd
        try:
            yield Group()
        finally:
            os.close(read_fd)
            os.close(write_fd)
    monkeypatch.setattr(provider_client,'resource_group',resource)


def supervised(port, revoked, *, seconds=3):
    selected=replace(profile(),base_url=f'http://127.0.0.1:{port}/v1/')
    def check():
        if revoked.is_set():raise PermissionError('revoked')
    return SupervisedProvider(selected,'host-only-key',128,task='task',session='session',
                              check=check,deadline=time.monotonic()+seconds,request_limit=4,
                              resource_parent=Path('/sys/fs/cgroup/test'),limits=Limits())


@pytest.mark.parametrize('mode',['headers','read'])
def test_supervised_provider_cancels_pending_network_and_reaps_child(supervised_resources,mode):
    with loopback_stream(mode) as (port,entered,first,release):
        revoked=threading.Event()
        broker=supervised(port,revoked)
        errors=[]
        def pending(call):
            try:call()
            except BaseException as exc:errors.append(exc)
        try:
            if mode=='read':
                broker('provider.open',{'url':broker.profile.endpoint,'method':'POST',
                    'body':base64.b64encode(b'{"model":"selected-model"}').decode()})
                call=lambda:broker('provider.next',{})
            else:
                call=lambda:broker('provider.open',{'url':broker.profile.endpoint,'method':'POST',
                    'body':base64.b64encode(b'{"model":"selected-model"}').decode()})
            thread=threading.Thread(target=pending,args=(call,))
            thread.start()
            assert entered.wait(2)
            time.sleep(.05)
            revoked.set()
            thread.join(timeout=1)
            assert not thread.is_alive()
            assert len(errors)==1 and isinstance(errors[0],PermissionError)
            assert broker.process is None and broker.channel is None
        finally:
            release.set()
            broker.close()


def test_supervised_provider_delivers_first_sse_before_eof(supervised_resources):
    with loopback_stream('first') as (port,entered,first,release):
        broker=supervised(port,threading.Event())
        packet={'url':broker.profile.endpoint,'method':'POST',
                'body':base64.b64encode(b'{"model":"selected-model"}').decode()}
        try:
            broker('provider.open',packet)
            assert first.wait(2)
            parts=[]
            thread=threading.Thread(target=lambda:parts.append(broker('provider.next',{})))
            thread.start();thread.join(timeout=.75)
            assert not thread.is_alive() and len(parts)==1
            assert base64.b64decode(parts[0]['chunk'])==b'data: hello\n\n'
        finally:
            release.set()
            broker.close()


def test_supervised_provider_request_budget_survives_child_restart():
    broker=SupervisedProvider(profile(),'host-only-key',128,task='task',session='session',
                              check=lambda:None,deadline=time.monotonic()+3,request_limit=2,
                              resource_parent=Path('/sys/fs/cgroup/test'),limits=Limits())
    starts=[]
    calls=[]

    class Channel:
        def __init__(self, fail):
            self.fail_next=fail
            self.requests=0

        def request(self, method, data):
            calls.append(method)
            if method=='provider.open':
                if self.requests>=2:
                    return {'error':'permission'}
                self.requests+=1
                return {'ok':{'status':200,'content_type':'application/json'}}
            if method=='provider.next':
                if self.fail_next:
                    self.fail_next=False
                    raise ConnectionError('simulated child loss')
                return {'ok':{'chunk':'','eof':True}}
            raise AssertionError(method)

        def close(self):
            pass

    def start():
        starts.append(True)
        broker.channel=Channel(fail=len(starts)==1)

    broker._start=start
    request={'url':broker.profile.endpoint,'method':'POST','body':'e30='}
    broker('provider.open',request)
    with pytest.raises(ConnectionError,match='simulated child loss'):
        broker('provider.next',{})
    assert broker.requests==1 and broker.channel is None

    broker('provider.open',request)
    assert broker('provider.next',{})=={'chunk':'','eof':True}
    with pytest.raises(PermissionError,match='count'):
        broker('provider.open',request)
    assert broker.requests==2
    assert len(starts)==2
    assert calls==['provider.open','provider.next','provider.open','provider.next']
    broker.close()


def test_supervised_provider_absolute_deadline_stops_slow_drip(supervised_resources):
    with loopback_stream('drip') as (port,entered,first,release):
        broker=supervised(port,threading.Event(),seconds=.6)
        packet={'url':broker.profile.endpoint,'method':'POST',
                'body':base64.b64encode(b'{"model":"selected-model"}').decode()}
        try:
            broker('provider.open',packet)
            assert first.wait(2)
            with pytest.raises(TimeoutError):
                while True:broker('provider.next',{})
            assert broker.process is None and broker.channel is None
        finally:
            release.set()
            broker.close()
