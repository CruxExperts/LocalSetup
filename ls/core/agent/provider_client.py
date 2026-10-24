"""Explicit OpenAI client and final-send endpoint, credential and identity guards."""
from __future__ import annotations

from contextlib import asynccontextmanager, ExitStack
import base64
import json
import math
import multiprocessing
import os
import socket
import ssl
import time

import httpx2 as httpx

from ..branding import user_agent
from .profiles import Profile
from .resource_group import Limits, resource_group

MAX_PROVIDER_BODY = 10 * 1024 * 1024
MAX_PROVIDER_RESPONSE = 16 * 1024 * 1024


def _wire(request, profile, credential, *, response_guard=False):
    if request.method != 'POST' or request.url != httpx.URL(profile.endpoint):
        raise ValueError('Request destination or method is outside the selected profile')
    content = request.content
    if len(content) > 16 * 1024 * 1024:
        raise ValueError('Serialized provider request exceeds 16 MiB')
    request.headers = httpx.Headers({
        'Host': request.url.netloc.decode('ascii'), 'User-Agent': user_agent(),
        'Authorization': 'Bearer ' + credential, 'Content-Type': 'application/json',
        'Accept': 'application/json', 'Content-Length': str(len(content)),
    })
    for key, value in [('OpenAI-Organization', profile.organization), ('OpenAI-Project', profile.project)]:
        if value:
            request.headers[key] = value
    if response_guard:
        request.headers['Accept-Encoding'] = 'identity'
        request.headers['X-OmniRoute-Compression'] = 'off'
        session_id = getattr(response_guard, 'session_id', None)
        if session_id is not None:
            request.headers['X-Session-Id'] = session_id


class BoundTransport(httpx.AsyncBaseTransport):
    def __init__(self, profile: Profile, credential: str, delegate: httpx.AsyncBaseTransport, response_guard=None):
        self.profile, self.credential, self.delegate = profile, credential, delegate
        self.response_guard = response_guard

    async def handle_async_request(self, request):
        _wire(request, self.profile, self.credential, response_guard=self.response_guard)
        response = await self.delegate.handle_async_request(request)
        return await self.response_guard(response) if self.response_guard else response

    async def aclose(self):
        await self.delegate.aclose()


class BoundSyncTransport(httpx.BaseTransport):
    """Trusted broker equivalent of the SDK's final-send guard."""

    def __init__(self, profile, credential, delegate):
        self.profile, self.credential, self.delegate = profile, credential, delegate

    def handle_request(self, request):
        _wire(request, self.profile, self.credential)
        return self.delegate.handle_request(request)

    def close(self):
        self.delegate.close()


class BrokerTransport(httpx.AsyncBaseTransport):
    """Worker transport: only the inherited RPC channel can send provider data."""

    def __init__(self, channel, profile):
        self.channel, self.profile = channel, profile

    async def handle_async_request(self, request):
        if request.method != 'POST' or request.url != httpx.URL(self.profile.endpoint):
            raise ValueError('Worker provider destination or method changed')
        if len(request.content) > MAX_PROVIDER_BODY:
            raise ValueError('Worker provider request exceeds limit')
        opened = await self.channel.request_async('provider.open', {
            'url': str(request.url), 'method': request.method,
            'body': base64.b64encode(request.content).decode('ascii'),
        })
        if (not isinstance(opened, dict) or set(opened) != {'status', 'content_type'}
                or type(opened['status']) is not int or not 100 <= opened['status'] <= 599
                or not isinstance(opened['content_type'], str) or len(opened['content_type']) > 256):
            raise ValueError('Invalid broker provider response')
        return httpx.Response(opened['status'], headers={'content-type': opened['content_type']},
                              stream=_BrokerStream(self.channel))


class _BrokerStream(httpx.AsyncByteStream):
    def __init__(self, channel):
        self.channel, self.closed = channel, False

    async def __aiter__(self):
        while not self.closed:
            packet = await self.channel.request_async('provider.next', {})
            if not isinstance(packet, dict) or set(packet) != {'chunk', 'eof'} or type(packet['eof']) is not bool or not isinstance(packet['chunk'], str):
                raise ValueError('Invalid broker provider chunk')
            if packet['eof']:
                self.closed = True
                return
            raw = base64.b64decode(packet['chunk'], validate=True)
            if len(raw) > 65536:
                raise ValueError('Broker provider chunk exceeds limit')
            yield raw

    async def aclose(self):
        if not self.closed:
            self.closed = True
            await self.channel.request_async('provider.close', {})


class ProviderBroker:
    """Trusted endpoint, model, credential and response lifetime for one run."""

    def __init__(self, profile, credential, token_limit, *, transport=None, check=None,
                 deadline=None, request_limit=64):
        self.profile, self.credential, self.token_limit = profile, credential, token_limit
        self.transport, self.client, self.response, self.chunks = transport, None, None, None
        self.check, self.deadline = check, deadline
        self.request_limit, self.requests = request_limit, 0
        self.bytes, self.pending = 0, b''

    def close(self):
        if self.response is not None:
            self.response.close()
            self.response = None
        if self.client is not None:
            self.client.close()
            self.client = None
        self.chunks = None
        self.pending = b''

    def __call__(self, method, data):
        if method == 'provider.close':
            if data != {} or self.response is None:
                raise ValueError('No open provider response')
            self.close()
            return {}
        if method == 'provider.next':
            if data != {} or self.chunks is None:
                raise ValueError('No open provider response')
            if self.pending:
                chunk, self.pending = self.pending[:65536], self.pending[65536:]
            else:
                try:
                    chunk = next(self.chunks)
                except StopIteration:
                    self.close()
                    return {'chunk': '', 'eof': True}
                if len(chunk)>65536:
                    chunk, self.pending = chunk[:65536], chunk[65536:]
            self.bytes += len(chunk)
            if self.bytes > MAX_PROVIDER_RESPONSE:
                self.close()
                raise ValueError('Provider response exceeds limit')
            return {'chunk': base64.b64encode(chunk).decode('ascii'), 'eof': False}
        if method != 'provider.open' or self.response is not None or not isinstance(data, dict) or set(data) != {'url', 'method', 'body'}:
            raise ValueError('Invalid provider operation')
        if self.requests >= self.request_limit:
            raise PermissionError('Provider request count exceeds grant')
        if (data['method'] != 'POST' or not isinstance(data['url'], str)
                or len(data['url']) > 4096 or httpx.URL(data['url']) != httpx.URL(self.profile.endpoint)
                or not isinstance(data['body'], str)):
            raise PermissionError('Provider destination or method is outside selected profile')
        if len(data['body']) > (MAX_PROVIDER_BODY * 4 // 3 + 4):
            raise ValueError('Provider request exceeds limit')
        body = base64.b64decode(data['body'], validate=True)
        if len(body) > MAX_PROVIDER_BODY:
            raise ValueError('Provider request exceeds limit')
        def unique(pairs):
            values = {}
            for key, value in pairs:
                if key in values:
                    raise ValueError('Duplicate provider parameter')
                values[key] = value
            return values
        packet = json.loads(body, object_pairs_hook=unique)
        if not isinstance(packet, dict) or packet.get('model') != self.profile.model:
            raise PermissionError('Provider model is outside selected profile')
        names = ('max_tokens', 'max_completion_tokens') if self.profile.api == 'chat_completions' else ('max_output_tokens',)
        if any(name in packet for name in ({'max_tokens','max_completion_tokens','max_output_tokens'}-set(names))):
            raise ValueError('Provider token parameter belongs to another API')
        present = [name for name in names if name in packet]
        if len(present)>1:
            raise ValueError('Provider token parameters are ambiguous')
        for name in present:
            value = packet[name]
            if value is None:
                packet[name] = self.token_limit
            elif type(value) is not int or not 1 <= value <= self.token_limit:
                raise ValueError('Provider token parameter exceeds grant')
        if not present:
            packet['max_completion_tokens' if self.profile.api == 'chat_completions' else 'max_output_tokens'] = self.token_limit
        if 'temperature' in packet and 'temperature' not in self.profile.capabilities:
            raise PermissionError('Provider temperature is outside selected capabilities')
        body = json.dumps(packet, separators=(',', ':'), allow_nan=False).encode()
        if len(body) > MAX_PROVIDER_BODY:
            raise ValueError('Provider request exceeds limit')
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
        delegate = self.transport if self.transport is not None else httpx.HTTPTransport(verify=context, retries=0, trust_env=False)
        timeout = self.profile.timeout_seconds
        if self.deadline is not None:
            timeout = min(timeout, max(0.001, self.deadline-time.monotonic()))
        self.client = httpx.Client(transport=BoundSyncTransport(self.profile, self.credential, delegate),
                                   trust_env=False, follow_redirects=False, timeout=timeout)
        try:
            request = self.client.build_request('POST', self.profile.endpoint, content=body)
            if self.check is not None:
                self.check()
            self.requests += 1
            self.response = self.client.send(request, stream=True)
            self.chunks = self.response.iter_bytes()
            self.bytes, self.pending = 0, b''
            return {'status': self.response.status_code, 'content_type': self.response.headers.get('content-type', 'application/json')[:256]}
        except BaseException:
            self.close()
            raise


_PROVIDER_METHODS = frozenset({'provider.open', 'provider.next', 'provider.close'})


def _serve_provider(connection, *, task, session, expires, profile, credential,
                    token_limit, request_limit, membership):
    """One trusted network child; no worker descriptor or workspace is admitted."""
    from .broker_rpc import Channel
    channel = None
    broker = ProviderBroker(profile, credential, token_limit, deadline=expires,
                            request_limit=request_limit)
    try:
        fd = membership.detach()
        try:
            value = str(os.getpid()).encode('ascii')
            if os.write(fd, value) != len(value):
                raise OSError('Provider resource membership failed')
        finally:
            os.close(fd)
        channel = Channel(connection, task=task, session=session,
                          methods=_PROVIDER_METHODS, expires=expires)
        def handle(method, data):
            try:
                return {'ok': broker(method, data)}
            except PermissionError:
                return {'error': 'permission'}
            except (ValueError, TypeError):
                return {'error': 'invalid'}
            except Exception:
                return {'error': 'failed'}
        try:
            while True:
                try:
                    channel.serve_once(handle, check=lambda: None)
                except (ConnectionError, TimeoutError):
                    break
        except BaseException:
            pass  # Never render provider or credential data to child diagnostics.
    finally:
        broker.close()
        if channel is not None:
            channel.close()
        else:
            connection.close()


class _LiveProviderGrant:
    def __init__(self, check):
        self.check = check

    def is_set(self):
        self.check()
        return False


class SupervisedProvider:
    """Poll live authority while a disposable trusted child performs provider I/O."""

    def __init__(self, profile, credential, token_limit, *, task, session,
                 check, deadline, request_limit, resource_parent, limits):
        if (not callable(check) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or deadline <= time.monotonic()
                or not isinstance(limits, Limits)):
            raise ValueError('Supervised provider requires live authority and deadline')
        self.profile, self.credential = profile, credential
        self.token_limit, self.request_limit = token_limit, request_limit
        self.requests = 0
        self.task, self.session, self.check, self.deadline = task, session, check, deadline
        self.resource_parent, self.limits = resource_parent, limits
        self.process, self.channel, self.opened = None, None, False
        self._stack = ExitStack()

    def _start(self):
        from .broker_rpc import Channel
        left, right = socket.socketpair()
        try:
            group = self._stack.enter_context(resource_group(self.resource_parent, self.limits))
            membership_fd = self._stack.enter_context(group.membership())
            membership = multiprocessing.reduction.DupFd(membership_fd)
            context = multiprocessing.get_context('spawn')
            process = context.Process(target=_serve_provider, args=(right,), kwargs={
                'task': self.task, 'session': self.session, 'expires': self.deadline,
                'profile': self.profile, 'credential': self.credential,
                'token_limit': self.token_limit, 'request_limit': self.request_limit,
                'membership': membership,
            })
            process.start()
            self.process = process
            self.channel = Channel(left, task=self.task, session=self.session,
                                   methods=_PROVIDER_METHODS, expires=self.deadline,
                                   cancelled=_LiveProviderGrant(self.check))
        except BaseException:
            left.close()
            self.close()
            raise
        finally:
            right.close()

    def close(self):
        if self.channel is not None:
            self.channel.close()
            self.channel = None
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=0.5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=0.5)
            if self.process.is_alive():
                raise RuntimeError('Provider child could not be reaped')
            self.process = None
        self._stack.close()
        self.opened = False

    def __call__(self, method, data):
        if method not in _PROVIDER_METHODS or not isinstance(data, dict):
            raise ValueError('Invalid provider operation')
        if method == 'provider.open':
            if self.opened:
                raise ValueError('Provider response is already open')
        elif not self.opened:
            raise ValueError('No open provider response')
        self.check()
        if method == 'provider.open' and self.requests >= self.request_limit:
            raise PermissionError('Provider request count exceeds grant')
        if self.channel is None:
            self._start()
        if method == 'provider.open':
            # This trusted parent outlives disposable provider children. Charge
            # before IPC and never refund if the child or response is lost.
            self.requests += 1
        try:
            packet = self.channel.request(method, data)
            if not isinstance(packet, dict) or set(packet) not in ({'ok'}, {'error'}):
                raise ValueError('Invalid trusted provider receipt')
            if 'error' in packet:
                if packet['error'] == 'permission':
                    raise PermissionError('Provider request is outside the selected grant')
                if packet['error'] == 'invalid':
                    raise ValueError('Provider request is invalid or exceeds limits')
                raise RuntimeError('Trusted provider request failed')
            result = packet['ok']
            if method == 'provider.open':
                self.opened = True
            elif method == 'provider.close' or (method == 'provider.next' and result.get('eof') is True):
                self.opened = False
            return result
        except BaseException:
            self.close()
            raise


@asynccontextmanager
async def client(profile: Profile, environment: dict[str, str], *, transport=None, response_guard=None):
    from openai import AsyncOpenAI
    credential = profile.credential(environment)
    # Explicit trust store excludes ambient proxy, CA and SDK credential settings.
    import certifi
    context = ssl.create_default_context(cafile=certifi.where())
    delegate = transport if transport is not None else httpx.AsyncHTTPTransport(verify=context, retries=0, trust_env=False)
    http = httpx.AsyncClient(transport=BoundTransport(profile, credential, delegate, response_guard),
                            trust_env=False, follow_redirects=False, timeout=profile.timeout_seconds)
    sdk = AsyncOpenAI(api_key=credential, admin_api_key='', base_url=profile.base_url, organization=profile.organization, project=profile.project,
                      webhook_secret='', max_retries=0, timeout=profile.timeout_seconds, http_client=http)
    # The pinned SDK merges OPENAI_CUSTOM_HEADERS during construction. Clear that
    # private adapter field before serialization as well as rebuilding wire headers.
    sdk._custom_headers = {}
    try:
        yield sdk
    finally:
        await sdk.close()
