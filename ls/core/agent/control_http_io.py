"""Bounded HTTP framing for opaque control messages, without content decoding."""
import re
import socket
import threading
import time

from .control_transport import HEADER, MAX_CIPHERTEXT

MEDIA_TYPE = 'application/vnd.localsetup.control-v1'
MAX_BODY = HEADER.size+MAX_CIPHERTEXT
MAX_WIRE = 2*1024*1024


class Deadline:
    """Interrupt blocking socket reads as well as slow, continually active peers."""
    def __init__(self, connection, expires, cancelled):
        self.connection, self.expires, self.cancelled = connection, expires, cancelled
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._watch, name='control-http-deadline', daemon=True)
        self.thread.start()

    def check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.expires:
            raise TimeoutError('Control HTTP deadline or cancellation')

    def _watch(self):
        while not self.done.wait(.025):
            if self.cancelled.is_set() or time.monotonic() >= self.expires:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    def close(self):
        self.done.set()
        self.thread.join(.2)
        if self.thread.is_alive():
            raise RuntimeError('Control HTTP deadline monitor did not stop')


class Reader:
    def __init__(self, stream):
        self.stream, self.used, self.headers = stream, 0, True

    def _record(self, raw):
        self.used += len(raw)
        if self.used > (16384 if self.headers else MAX_WIRE):
            raise ValueError('Control HTTP wire budget exceeded')
        return raw

    def readline(self, limit=-1):
        raw = self.stream.readline(min(4097, limit) if limit >= 0 else 4097)
        if len(raw) > 4096:
            raise ValueError('Control HTTP line exceeds limit')
        return self._record(raw)

    def read(self, size):
        if type(size) is not int or not 0 <= size <= MAX_BODY:
            raise ValueError('Control HTTP read exceeds limit')
        return self._record(self.stream.read(size))

    def close(self):
        self.stream.close()

    def flush(self):
        self.stream.flush()


def framing(headers):
    def one(name):
        values = headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError('Duplicate control HTTP framing header')
        return values[0].strip() if values else None
    content_type, encoding = one('Content-Type'), one('Content-Encoding')
    length, transfer = one('Content-Length'), one('Transfer-Encoding')
    if content_type != MEDIA_TYPE or encoding not in (None, 'identity'):
        raise ValueError('Unsupported control HTTP content')
    if transfer is not None:
        if transfer.lower() != 'chunked' or length is not None:
            raise ValueError('Ambiguous control HTTP transfer')
        return None
    if length is None or not re.fullmatch(r'[0-9]{1,8}', length) or not HEADER.size < int(length) <= MAX_BODY:
        raise ValueError('Invalid control HTTP content length')
    return int(length)


def read_body(headers, reader):
    size = framing(headers)
    reader.headers = False
    def exact(count):
        raw = reader.read(count)
        if len(raw) != count:
            raise EOFError('Truncated control HTTP body')
        return raw
    if size is not None:
        return exact(size)
    body = bytearray()
    while True:
        line = reader.readline(32)
        if not re.fullmatch(rb'[0-9a-fA-F]{1,8}\r\n', line):
            raise ValueError('Invalid control HTTP chunk')
        count = int(line[:-2], 16)
        if count == 0:
            if reader.readline(3) != b'\r\n':
                raise ValueError('Control HTTP trailers are unsupported')
            return bytes(body)
        if len(body)+count > MAX_BODY:
            raise ValueError('Control HTTP body exceeds limit')
        body.extend(exact(count))
        if exact(2) != b'\r\n':
            raise ValueError('Invalid control HTTP chunk boundary')


def chunks(body):
    for offset in range(0, len(body), 65536):
        part = body[offset:offset+65536]
        yield f'{len(part):x}\r\n'.encode()+part+b'\r\n'
    yield b'0\r\n\r\n'
