"""Finite HTTP activation of a ciphertext-only private run attachment."""
from http.server import HTTPServer
import ipaddress
import json
import math
import signal
import socket
from socketserver import TCPServer
import ssl
import sys
import threading
import time

from .control_http import Address, handler
from .control_http_io import Deadline
from .control_relay import Forwarder
from .control_socket import connect
from .control_transport import MAX_BYTES, MAX_CALLS


class _Stop:
    def __init__(self, expires, cancelled):
        self.expires, self.cancelled = expires, cancelled
        self.local = threading.Event()

    def is_set(self):
        return (self.local.is_set() or time.monotonic() >= self.expires
                or (self.cancelled is not None and self.cancelled.is_set()))

    def set(self):
        self.local.set()


class _Budget:
    def __init__(self, stop):
        self.used, self.stop = 0, stop

    def consume(self, size):
        self.used += size
        if self.used > MAX_BYTES:
            self.stop.set()
            raise ValueError('HTTP relay input budget exceeded')


class RelayServer:
    """One serialized listener, one upstream connection, no restart or retry.

    Binding is explicit; construction opens a TCP listener and connects the
    selected private socket. Use as a context manager to start and collect it.
    An optional server TLS context must already contain its selected identity.
    """
    def __init__(self, selected, *, bind, port, expires, cancelled=None,
                 request_timeout=30, tls_context=None):
        if type(bind) is not str or '%' in bind:
            raise ValueError('HTTP relay bind requires a literal IP without a zone')
        address = ipaddress.ip_address(bind)
        if (type(port) is not int or not 0 <= port <= 65535
                or type(expires) not in (int, float) or not math.isfinite(expires)
                or not 0 < expires-time.monotonic() <= 3600
                or type(request_timeout) not in (int, float) or not math.isfinite(request_timeout)
                or not 0 < request_timeout <= 60
                or (tls_context is not None and (not isinstance(tls_context, ssl.SSLContext)
                    or tls_context.protocol != ssl.PROTOCOL_TLS_SERVER))):
            raise ValueError('Invalid finite HTTP relay configuration')
        self.stop = _Stop(expires, cancelled)
        self.upstream = self.server = self.thread = None
        self.failed = False
        self.closed = False
        self.accepted = 0
        self.connection_expires = expires
        self.budget = _Budget(self.stop)
        if self.stop.is_set():
            raise TimeoutError('HTTP relay owner ended before activation')
        owner = self
        class Server(HTTPServer):
            address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            request_queue_size = 1
            allow_reuse_address = False

            def server_bind(self):
                # Avoid HTTPServer's reverse DNS lookup for an explicit IP.
                TCPServer.server_bind(self)
                self.server_name, self.server_port = str(address), self.socket.getsockname()[1]

            def get_request(self):
                connection, peer = super().get_request()
                owner.accepted += 1
                watch = None
                try:
                    if owner.accepted > MAX_CALLS or owner.stop.is_set():
                        owner.stop.set()
                        raise OSError('HTTP relay connection budget ended')
                    deadline = min(expires, time.monotonic()+request_timeout)
                    owner.connection_expires = deadline
                    connection.settimeout(max(.001, deadline-time.monotonic()))
                    watch = Deadline(connection, deadline, owner.stop)
                    if tls_context is not None:
                        connection = tls_context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
                        watch.connection = connection
                        connection.do_handshake()
                    watch.check()
                    return connection, peer
                except Exception:
                    connection.close()
                    raise OSError('HTTP relay connection rejected') from None
                finally:
                    if watch is not None:
                        watch.close()

            def handle_error(self, *_):
                owner.failed = True
                owner.stop.set()  # Never print raw request or handler diagnostics.
        try:
            self.server = Server((str(address), port), None)
            self.server.timeout = .05
            self.upstream = connect(selected, expires=expires, cancelled=self.stop)
            base = handler(Forwarder(self.upstream), timeout=request_timeout, cancelled=self.stop)
            class Handler(base):
                def setup(self):
                    super().setup()
                    self.watch.expires = min(self.watch.expires, owner.connection_expires)
                    self.connection.settimeout(max(.001, self.watch.expires-time.monotonic()))
                    self.rfile.budget = owner.budget
            self.server.RequestHandlerClass = Handler
            host = f'[{address}]' if address.version == 6 else str(address)
            scheme = 'https' if tls_context is not None else 'http'
            self.address = Address(f'{scheme}://{host}:{self.server.server_port}/control', str(address))
        except BaseException:
            self.close()
            raise

    def _serve(self):
        try:
            while not self.stop.is_set() and not self.upstream.closed:
                self.server.handle_request()
            if self.upstream.closed and not self.stop.is_set():
                self.failed = True
        except BaseException:
            self.failed = True
        finally:
            self.stop.set()
            try:
                self.server.server_close()
            finally:
                self.upstream.close()

    def __enter__(self):
        if self.closed or self.thread is not None or self.stop.is_set():
            raise RuntimeError('HTTP relay cannot be restarted')
        self.thread = threading.Thread(target=self._serve, name='agent-control-http-relay')
        try:
            self.thread.start()
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        if self.closed:
            return
        self.stop.set()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join()
        try:
            if self.server is not None:
                self.server.server_close()
        finally:
            if self.upstream is not None:
                self.upstream.close()
        self.closed = True

    def __exit__(self, *_):
        self.close()


def main(args):
    cancelled = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: cancelled.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
            raise ValueError('Invalid relay lifetime')
        with RelayServer(args.socket, bind=args.bind, port=args.port,
                         expires=time.monotonic()+args.timeout, cancelled=cancelled) as server:
            print(json.dumps({'state': 'ready', 'url': server.address.url}), flush=True)
            server.thread.join()
            return 2 if server.failed else 0
    except (OSError, ValueError, RuntimeError):
        print('HTTP control relay ended; reconcile unacknowledged requests without replay.', file=sys.stderr)
        return 2
    finally:
        for sig, previous_handler in previous.items():
            signal.signal(sig, previous_handler)
