from contextlib import contextmanager
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time

import pytest

from ls.core.agent.control_http import HTTP, Address
from ls.core.agent import control_http_server as module
from ls.core.agent.control_socket import Listener
from ls.core.agent.control_transport import Endpoint, pack, unpack
from ls.tests.test_control_http import tls_contexts


class Echo(Endpoint):
    def __init__(self): self.calls = []
    def respond(self, raw):
        kind, identifier, reply, body = unpack(raw)
        assert not reply
        self.calls.append(identifier)
        return pack(kind, identifier, body, reply=True)


@contextmanager
def upstream():
    with tempfile.TemporaryDirectory(prefix='lshttp-') as temporary:
        selected = Path(temporary)/'socket'
        listener = Listener(selected)
        stop = threading.Event()
        endpoint = Echo()
        def serve():
            stream = None
            try:
                stream = listener.accept(expires=time.monotonic()+10, cancelled=stop)
                while not stop.is_set(): stream.serve_once(endpoint)
            except (OSError, ValueError):
                pass  # Relay closure ends its one owned stream.
            finally:
                if stream is not None: stream.close()
        thread = threading.Thread(target=serve); thread.start()
        try:
            yield selected, endpoint
        finally:
            stop.set(); thread.join(2); listener.close()
            assert not thread.is_alive()


def test_http_owner_preserves_both_modes_and_collects_listener():
    with upstream() as (selected, endpoint):
        relay = module.RelayServer(selected, bind='127.0.0.1', port=0, expires=time.monotonic()+5)
        with relay:
            client = HTTP(relay.address, expires=time.monotonic()+3)
            for index, streaming in enumerate((False, True), 1):
                assert client.exchange('execute', f'{index:032x}', b'opaque', streaming=streaming) == b'opaque'
            port = relay.server.server_port
        assert endpoint.calls == [f'{index:032x}' for index in (1, 2)]
        assert relay.closed and not relay.thread.is_alive() and relay.upstream.closed
        with pytest.raises(OSError): socket.create_connection(('127.0.0.1', port), timeout=.2)
        with pytest.raises(RuntimeError): relay.__enter__()


def test_owned_tls_listener_uses_selected_certificate(tls_contexts):
    incoming, outgoing = tls_contexts
    with upstream() as (selected, endpoint):
        with module.RelayServer(selected, bind='127.0.0.1', port=0,
                expires=time.monotonic()+5, tls_context=incoming) as relay:
            address = Address(relay.address.url.replace('127.0.0.1', 'localhost'), '127.0.0.1')
            client = HTTP(address, expires=time.monotonic()+3, tls_context=outgoing)
            assert client.exchange('execute', 'a'*32, b'opaque', streaming=True) == b'opaque'
        assert endpoint.calls == ['a'*32]


@pytest.mark.parametrize('tls', [False, True])
def test_stalled_header_or_tls_handshake_cannot_outlive_owner(tls):
    with upstream() as (selected, endpoint):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if tls else None
        with module.RelayServer(selected, bind='127.0.0.1', port=0,
                expires=time.monotonic()+.3, request_timeout=.2, tls_context=context) as relay:
            with socket.create_connection(('127.0.0.1', relay.server.server_port), timeout=1) as client:
                if not tls: client.sendall(b'POST /control HTTP/1.1\r\nHost: ')
                assert client.recv(1024) == b''
            relay.thread.join(1)
            assert not relay.thread.is_alive()
        assert endpoint.calls == []


def test_aggregate_bad_request_budget_ends_listener(monkeypatch):
    monkeypatch.setattr(module, 'MAX_BYTES', 1000)
    with upstream() as (selected, endpoint):
        with module.RelayServer(selected, bind='127.0.0.1', port=0, expires=time.monotonic()+3) as relay:
            with socket.create_connection(('127.0.0.1', relay.server.server_port), timeout=1) as client:
                client.sendall(b'POST /control HTTP/1.1\r\nHost: '+b'x'*1100+b'\r\n\r\n')
                assert client.recv(1024) == b''
            relay.thread.join(1)
            assert not relay.thread.is_alive() and relay.budget.used > 1000
        assert endpoint.calls == []


def test_bind_failure_does_not_connect_to_run(monkeypatch):
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0)); occupied.listen(1)
        monkeypatch.setattr(module, 'connect', lambda *_a, **_k: pytest.fail('must not attach after bind failure'))
        with pytest.raises(OSError):
            module.RelayServer(Path('/private/socket'), bind='127.0.0.1',
                port=occupied.getsockname()[1], expires=time.monotonic()+3)


def test_cli_rejects_invalid_bind_without_disclosing_selected_path(capsys):
    from ls.core.agent.cli import main
    assert main(['control-http-relay', '--socket', '/private/control.sock', '--bind', 'not-an-ip', '--port', '0']) == 2
    output = capsys.readouterr()
    assert not output.out and '/private/control.sock' not in output.err and 'Traceback' not in output.err
