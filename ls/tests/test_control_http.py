"""Loopback HTTP qualification; no external endpoint or installed listener."""
from contextlib import contextmanager
from http.server import HTTPServer
import socket
import shutil
import ssl
import subprocess
import threading
import time

import pytest

from ls.core.agent.control_http import Address, HTTP, handler
from ls.core.agent.control_http_io import MEDIA_TYPE
from ls.core.agent.control_transport import Endpoint, TransportError, pack, unpack


class Echo(Endpoint):
    def __init__(self):
        self.calls = []

    def respond(self, raw):
        kind, identifier, reply, ciphertext = unpack(raw)
        assert not reply
        self.calls.append((kind, identifier, ciphertext))
        return pack(kind, identifier, ciphertext, reply=True)


@contextmanager
def server(endpoint, tls_context=None):
    instance = HTTPServer(('127.0.0.1', 0), handler(endpoint, timeout=60))
    if tls_context is not None:
        instance.socket = tls_context.wrap_socket(instance.socket, server_side=True)
    thread = threading.Thread(target=lambda: instance.serve_forever(poll_interval=.02))
    thread.start()
    try:
        scheme = 'https' if tls_context is not None else 'http'
        yield Address(f'{scheme}://localhost:{instance.server_port}/control', '127.0.0.1')
    finally:
        instance.shutdown(); instance.server_close(); thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize('streaming', [False, True])
def test_http_modes_preserve_identical_frame_and_ignore_dns_proxy(monkeypatch, streaming):
    endpoint = Echo()
    with server(endpoint) as address:
        monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('DNS forbidden')))
        client = HTTP(address, expires=time.monotonic()+5)
        assert client.exchange('reconcile', 'a'*32, b'\x00opaque\xff', streaming=streaming) == b'\x00opaque\xff'
    assert endpoint.calls == [('reconcile', 'a'*32, b'\x00opaque\xff')]


@contextmanager
def raw_server(reply, *, drip=False, prefix=b''):
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(1)
    listener.settimeout(2)
    stop = threading.Event()
    calls = []
    def run():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                calls.append(connection.recv(4096))
                if prefix:
                    connection.sendall(prefix)
                if drip:
                    for byte in reply:
                        if stop.wait(.03):
                            break
                        connection.sendall(bytes([byte]))
                elif reply is not None:
                    connection.sendall(reply)
                else:
                    stop.wait(2)
        except OSError:
            pass
    thread = threading.Thread(target=run); thread.start()
    try:
        yield Address(f'http://localhost:{listener.getsockname()[1]}/control', '127.0.0.1'), calls
    finally:
        stop.set(); thread.join(3); listener.close()
        assert not thread.is_alive()


@pytest.mark.parametrize('reply', [
    b'HTTP/1.1 307 Redirect\r\nLocation: http://127.0.0.1:1/\r\nContent-Length: 0\r\n\r\n',
    f'HTTP/1.1 200 OK\r\nContent-Type: {MEDIA_TYPE}\r\nContent-Length: 32\r\nContent-Length: 32\r\n\r\n'.encode(),
    b'HTTP/1.1 200 OK\r\nX-Large: '+b'x'*5000+b'\r\n\r\n',
    f'HTTP/1.1 200 OK\r\nContent-Type: {MEDIA_TYPE}\r\nTransfer-Encoding: chunked\r\n\r\n200000\r\n'.encode(),
    f'HTTP/1.1 200 OK\r\nContent-Type: {MEDIA_TYPE}\r\nContent-Length: 32\r\n\r\nshort'.encode(),
], ids=['redirect', 'duplicate-length', 'large-header', 'oversized-chunk', 'truncated-body'])
def test_invalid_http_response_is_uncertain_without_redirect_or_retry(reply):
    with raw_server(reply) as (address, calls):
        client = HTTP(address, expires=time.monotonic()+2)
        with pytest.raises(TransportError) as caught:
            client.exchange('execute', 'a'*32, b'ciphertext')
        assert caught.value.outcome == 'uncertain' and client.closed
        with pytest.raises(TransportError) as repeated:
            client.exchange('execute', 'a'*32, b'ciphertext')
        assert repeated.value.outcome == 'not_dispatched'
    assert len(calls) == 1


@pytest.mark.parametrize('drip,cancel', [(False, False), (True, False), (False, True)])
def test_total_deadline_and_cancellation_interrupt_stalled_headers(drip, cancel):
    cancelled = threading.Event()
    with raw_server(b'HTTP/1.1 200 OK\r\n' if drip else None, drip=drip) as (address, calls):
        client = HTTP(address, expires=time.monotonic()+(2 if cancel else .15), cancelled=cancelled)
        timer = threading.Timer(.1, cancelled.set) if cancel else None
        if timer:
            timer.start()
        start = time.monotonic()
        try:
            with pytest.raises(TransportError) as caught:
                client.exchange('execute', 'a'*32, b'ciphertext')
            assert caught.value.outcome == 'uncertain' and time.monotonic()-start < 1
        finally:
            if timer:
                timer.join(1)
    assert len(calls) == 1


def test_insecure_tls_context_is_rejected_before_request_bytes():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    with raw_server(None) as (address, calls):
        address = Address(address.url.replace('http:', 'https:'), address.ip)
        client = HTTP(address, expires=time.monotonic()+2, tls_context=context)
        with pytest.raises(TransportError) as caught:
            client.exchange('execute', 'a'*32, b'ciphertext')
        assert caught.value.outcome == 'not_dispatched'
    assert calls == [b'']


def test_https_verifies_certificate_and_original_hostname(tmp_path):
    executable = shutil.which('openssl')
    if executable is None:
        pytest.skip('OpenSSL CLI is required for the disposable TLS fixture')
    tmp_path.chmod(0o700)
    key, certificate = tmp_path/'key.pem', tmp_path/'certificate.pem'
    subprocess.run([executable, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(key), '-out', str(certificate), '-days', '1', '-subj', '/CN=localhost',
        '-addext', 'subjectAltName=DNS:localhost'], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    key.chmod(0o600)
    incoming = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    incoming.load_cert_chain(certificate, key)
    outgoing = ssl.create_default_context(cafile=str(certificate))
    endpoint = Echo()
    with server(endpoint, incoming) as address:
        client = HTTP(address, expires=time.monotonic()+5, tls_context=outgoing)
        assert client.exchange('execute', 'a'*32, b'opaque') == b'opaque'
        wrong = Address(address.url.replace('localhost', 'wrong.invalid'), address.ip)
        with pytest.raises(TransportError) as caught:
            HTTP(wrong, expires=time.monotonic()+5, tls_context=outgoing).exchange('execute', 'b'*32, b'opaque')
        assert caught.value.outcome == 'not_dispatched'
    assert len(endpoint.calls) == 1


def test_total_deadline_interrupts_a_continually_arriving_body():
    body = pack('execute', 'a'*32, b'x'*100, reply=True)
    prefix = (f'HTTP/1.1 200 OK\r\nContent-Type: {MEDIA_TYPE}\r\n'
              f'Content-Length: {len(body)}\r\n\r\n').encode()
    with raw_server(body, drip=True, prefix=prefix) as (address, _calls):
        client = HTTP(address, expires=time.monotonic()+.15)
        start = time.monotonic()
        with pytest.raises(TransportError) as caught:
            client.exchange('execute', 'a'*32, b'ciphertext')
        assert caught.value.outcome == 'uncertain' and time.monotonic()-start < 1


def test_ambiguous_request_framing_never_reaches_endpoint():
    from urllib.parse import urlsplit
    endpoint = Echo()
    with server(endpoint) as address:
        with socket.create_connection((address.ip, urlsplit(address.url).port), timeout=1) as connection:
            connection.sendall((f'POST /control HTTP/1.1\r\nHost: localhost\r\n'
                f'Content-Type: {MEDIA_TYPE}\r\nContent-Length: 32\r\n'
                'Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n').encode())
            assert connection.recv(1024) == b''
    assert endpoint.calls == []
