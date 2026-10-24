"""Bounded opaque control frames over owned streams; never retry effects.

Routing fields are untrusted hints. Only the encrypted protocol can authorize
their bound request. This adapter neither starts SSH nor admits a remote host.
"""
import math
import os
import re
import selectors
import stat
import struct
import threading
import time

from .control_envelope import MAX_CIPHERTEXT
from .remote_control import serve_request, serve_reconcile

HEADER = struct.Struct('!4sBB16sI')
KINDS = {'execute': 1, 'reconcile': 2}
MAX_BYTES = 64*1024*1024
MAX_CALLS = 1024


def pack(kind, request_id, ciphertext, *, reply=False):
    if (not isinstance(kind, str) or kind not in KINDS or type(reply) is not bool
            or not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id)
            or type(ciphertext) is not bytes or not 0 < len(ciphertext) <= MAX_CIPHERTEXT):
        raise ValueError('Invalid control transport frame')
    return HEADER.pack(b'LSC1', KINDS[kind], int(reply), bytes.fromhex(request_id), len(ciphertext))+ciphertext


def header(raw):
    if type(raw) is not bytes or len(raw) != HEADER.size:
        raise ValueError('Invalid control transport header')
    magic, kind, reply, identifier, size = HEADER.unpack(raw)
    if magic != b'LSC1' or kind not in KINDS.values() or reply not in (0, 1) or not 0 < size <= MAX_CIPHERTEXT:
        raise ValueError('Invalid control transport header')
    return next(name for name, number in KINDS.items() if number == kind), identifier.hex(), bool(reply), size


def unpack(raw):
    if type(raw) is not bytes:
        raise ValueError('Invalid control transport bytes')
    kind, identifier, reply, size = header(raw[:HEADER.size])
    if len(raw) != HEADER.size+size:
        raise ValueError('Truncated or trailing control transport bytes')
    return kind, identifier, reply, raw[HEADER.size:]


class Endpoint:
    """Bind blind transports to the same authenticated control implementation."""
    def __init__(self, peer, binding, requests, journal, authorize, *, target_binding=None):
        self.peer, self.binding, self.requests = peer, binding, requests
        self.journal, self.authorize = journal, authorize
        self.target_binding = target_binding or binding

    def respond(self, raw):
        kind, identifier, reply, ciphertext = unpack(raw)
        if reply:
            raise ValueError('Endpoint requires a request frame')
        if kind == 'execute':
            result = serve_request(self.peer, self.binding, identifier, ciphertext,
                requests=self.requests, journal=self.journal, authorize=self.authorize)
        else:
            result = serve_reconcile(self.peer, self.binding, identifier, ciphertext,
                target_binding=self.target_binding, journal=self.journal, authorize=self.authorize)
        return pack(kind, identifier, result, reply=True)


class TransportError(ConnectionError):
    def __init__(self, *, dispatched):
        self.outcome = 'uncertain' if dispatched else 'not_dispatched'
        super().__init__('Control exchange ended; reconcile original identity without replay')


class Stream:
    """Take ownership of pipe/socket descriptors, with one absolute deadline.

    Descriptors are switched to nonblocking mode and closed on error or close().
    The caller owns and must collect the process providing them, if any.
    """
    def __init__(self, read_fd, write_fd, *, expires, cancelled=None):
        if (type(expires) not in (float, int) or not math.isfinite(expires)
                or expires <= time.monotonic()):
            raise ValueError('Control stream requires a future deadline')
        for fd in {read_fd, write_fd}:
            if type(fd) is not int or fd < 0:
                raise ValueError('Control stream requires owned descriptors')
            mode = os.fstat(fd).st_mode
            if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
                raise ValueError('Control stream requires pipes or sockets')
        self.read_fd, self.write_fd, self.expires = read_fd, write_fd, expires
        self.cancelled = cancelled or threading.Event()
        self.closed, self.used, self.calls = False, 0, 0
        self._busy = threading.Lock()
        for fd in {read_fd, write_fd}:
            os.set_blocking(fd, False)

    def close(self):
        if not self.closed:
            self.closed = True
            for fd in {self.read_fd, self.write_fd}:
                os.close(fd)

    def _check(self):
        if self.closed or self.cancelled.is_set() or time.monotonic() >= self.expires:
            raise ConnectionError('Control stream is closed, cancelled or expired')

    def _transfer(self, *, data=None, size=None, progress=None):
        sending = data is not None
        fd = self.write_fd if sending else self.read_fd
        pending = memoryview(data) if sending else None
        result = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE if sending else selectors.EVENT_READ)
            while (len(pending) if sending else size-len(result)):
                self._check()
                if not selector.select(min(.05, max(0, self.expires-time.monotonic()))):
                    continue
                try:
                    if sending:
                        count = os.write(fd, pending[:65536])
                        if not count:
                            raise ConnectionError('Control stream write ended')
                        if progress is not None:
                            progress[0] = True
                        pending = pending[count:]
                    else:
                        chunk = os.read(fd, min(65536, size-len(result)))
                        if not chunk:
                            raise ConnectionError('Control stream read ended')
                        result.extend(chunk)
                except BlockingIOError:
                    continue
        self._check()
        return bytes(result)

    def _send(self, raw, progress):
        if self.used+len(raw) > MAX_BYTES:
            raise ValueError('Control stream byte budget exceeded')
        self.used += len(raw)
        self._transfer(data=raw, progress=progress)

    def _receive(self):
        raw = self._transfer(size=HEADER.size)
        _, _, _, size = header(raw)
        if self.used+HEADER.size+size > MAX_BYTES:
            raise ValueError('Control stream byte budget exceeded')
        self.used += HEADER.size+size
        return raw+self._transfer(size=size)

    def exchange(self, kind, request_id, ciphertext):
        raw = pack(kind, request_id, ciphertext)
        progress = [False]
        if not self._busy.acquire(blocking=False):
            raise PermissionError('Control stream exchange already active')
        try:
            self._check()
            if self.calls >= MAX_CALLS:
                raise ValueError('Control stream call budget exceeded')
            self.calls += 1
            self._send(raw, progress)
            answer = self._receive()
            got_kind, got_id, reply, result = unpack(answer)
            if (got_kind, got_id, reply) != (kind, request_id, True):
                raise ValueError('Control response routing identity mismatch')
            return result
        except Exception:
            self.close()
            raise TransportError(dispatched=progress[0]) from None
        except BaseException:
            self.close()
            raise
        finally:
            self._busy.release()

    def serve_once(self, endpoint):
        if not isinstance(endpoint, Endpoint):
            raise ValueError('Control stream requires an authenticated endpoint')
        progress = [False]
        if not self._busy.acquire(blocking=False):
            raise PermissionError('Control stream exchange already active')
        try:
            self._check()
            if self.calls >= MAX_CALLS:
                raise ValueError('Control stream call budget exceeded')
            self.calls += 1
            raw = self._receive()
            self._check()
            progress[0] = True  # Dispatch may apply before response generation.
            response = endpoint.respond(raw)
            self._check()
            self._send(response, progress)
        except Exception:
            self.close()
            raise TransportError(dispatched=progress[0]) from None
        except BaseException:
            self.close()
            raise
        finally:
            self._busy.release()
