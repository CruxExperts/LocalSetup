"""Explicit-address HTTP adapters for the shared encrypted control endpoint."""
from dataclasses import dataclass
import http.client
from http.server import BaseHTTPRequestHandler
import ipaddress
import math
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from .control_http_io import Deadline, MEDIA_TYPE, MAX_WIRE, Reader, chunks, read_body
from .control_transport import Endpoint, MAX_BYTES, MAX_CALLS, TransportError, pack, unpack


@dataclass(frozen=True)
class Address:
    url: str
    ip: str

    def __post_init__(self):
        if (not isinstance(self.url, str) or len(self.url) > 2048
                or not self.url.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in self.url)
                or not isinstance(self.ip, str) or '%' in self.ip):
            raise ValueError('Invalid control HTTP address')
        value = urlsplit(self.url)
        if (value.scheme not in {'http', 'https'} or not value.hostname
                or value.username is not None or value.password is not None
                or value.query or value.fragment or (value.port is not None and not 1 <= value.port <= 65535)):
            raise ValueError('Control HTTP requires an explicit endpoint without credentials')
        ipaddress.ip_address(self.ip)


class HTTP:
    """No ambient proxy, DNS, redirects or retries; each call owns its connection."""
    def __init__(self, address, *, expires, cancelled=None, tls_context=None):
        if (not isinstance(address, Address) or type(expires) not in (int, float)
                or not math.isfinite(expires) or expires <= time.monotonic()):
            raise ValueError('Control HTTP requires an address and future deadline')
        self.address, self.expires = address, expires
        self.cancelled, self.tls_context = cancelled or threading.Event(), tls_context
        self.closed, self.calls, self.used = False, 0, 0
        self._busy = threading.Lock()

    def exchange(self, kind, request_id, ciphertext, *, streaming=False):
        raw = pack(kind, request_id, ciphertext)
        if type(streaming) is not bool:
            raise ValueError('Invalid HTTP streaming mode')
        if not self._busy.acquire(blocking=False):
            raise PermissionError('Control HTTP exchange already active')
        connection = watch = response = None
        dispatched = False
        try:
            if self.closed or self.cancelled.is_set() or time.monotonic() >= self.expires:
                raise TimeoutError('Control HTTP is closed, cancelled or expired')
            url = urlsplit(self.address.url)
            parts = list(chunks(raw)) if streaming else [raw]
            transfer = 'Transfer-Encoding: chunked' if streaming else f'Content-Length: {len(raw)}'
            head = (f'POST {url.path or "/"} HTTP/1.1\r\nHost: {url.netloc}\r\n'
                    f'Content-Type: {MEDIA_TYPE}\r\nAccept: {MEDIA_TYPE}\r\n'
                    f'{transfer}\r\nConnection: close\r\n\r\n').encode('ascii')
            planned = len(head)+sum(map(len, parts))
            if self.calls >= MAX_CALLS or self.used+planned+MAX_WIRE > MAX_BYTES:
                raise ValueError('Control HTTP exchange budget exceeded')
            self.calls += 1
            self.used += planned
            address = ipaddress.ip_address(self.address.ip)
            connection = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET)
            watch = Deadline(connection, self.expires, self.cancelled)
            watch.check()
            connection.settimeout(max(.001, self.expires-time.monotonic()))
            connection.connect((str(address), url.port or (443 if url.scheme == 'https' else 80)))
            if url.scheme == 'https':
                context = self.tls_context or ssl.create_default_context()
                if not isinstance(context, ssl.SSLContext) or not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                    raise ValueError('Control HTTPS requires certificate and hostname verification')
                connection = context.wrap_socket(connection, server_hostname=url.hostname, do_handshake_on_connect=False)
                watch.connection = connection
                connection.do_handshake()
            for part in [head, *parts]:
                watch.check()
                dispatched = True
                connection.sendall(part)
            response = http.client.HTTPResponse(connection, method='POST')
            reader = Reader(response.fp)
            response.fp = reader
            response.begin()
            watch.check()
            if response.status != 200:
                raise ValueError('Control HTTP response is not successful')
            answer = read_body(response.headers, reader)
            self.used += reader.used
            watch.check()
            got_kind, got_id, reply, result = unpack(answer)
            if (got_kind, got_id, reply) != (kind, request_id, True):
                raise ValueError('Control HTTP response routing mismatch')
            return result
        except Exception:
            self.closed = True
            raise TransportError(dispatched=dispatched) from None
        except BaseException:
            self.closed = True
            raise
        finally:
            try:
                if watch is not None:
                    watch.close()
            finally:
                try:
                    if response is not None:
                        response.close()
                finally:
                    try:
                        if connection is not None:
                            connection.close()
                    finally:
                        self._busy.release()


def handler(endpoint, *, path='/control', timeout=60, cancelled=None):
    """Build a handler for a caller-owned server; do not bind or install one."""
    if (not isinstance(endpoint, Endpoint) or not isinstance(path, str) or not path.startswith('/')
            or not path.isascii() or len(path) > 2048 or any(ord(c) <= 32 for c in path)
            or type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 3600):
        raise ValueError('Invalid control HTTP handler configuration')
    stop = cancelled or threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            self.watch = Deadline(self.connection, time.monotonic()+timeout, stop)
            self.connection.settimeout(timeout)
            self.rfile = Reader(self.rfile)

        def handle(self):
            try:
                super().handle()
            except Exception:
                self.close_connection = True

        def do_POST(self):
            self.close_connection = True
            if self.path != path:
                self.send_error(404)
                return
            body = read_body(self.headers, self.rfile)
            self.watch.check()
            response = endpoint.respond(body)
            self.watch.check()
            streaming = self.headers.get('Transfer-Encoding', '').lower() == 'chunked'
            self.send_response(200)
            self.send_header('Content-Type', MEDIA_TYPE)
            self.send_header('Connection', 'close')
            self.send_header('Transfer-Encoding' if streaming else 'Content-Length',
                             'chunked' if streaming else str(len(response)))
            self.end_headers()
            for part in chunks(response) if streaming else (response,):
                self.watch.check()
                self.wfile.write(part)

        def finish(self):
            try:
                super().finish()
            finally:
                self.watch.close()

        def log_message(self, *_args):
            pass

    return Handler
