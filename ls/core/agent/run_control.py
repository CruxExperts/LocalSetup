"""Bounded local owner status/cancellation, independent of model tool authority."""
from contextlib import contextmanager
import os
import select
import socket
import threading
import time

from .control_requests import Requests


def validate(fd):
    if type(fd) is not int or fd < 3:
        raise ValueError('Control descriptor must be separate from standard streams')
    duplicate = os.dup(fd)
    try:
        channel = socket.socket(fileno=duplicate)
    except OSError:
        os.close(duplicate)
        raise
    with channel:
        if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_STREAM:
            raise ValueError('Control requires a local stream socket')
        channel.getpeername()


class Control:
    def __init__(self, fd, cancelled, expires, steering=None, approvals=None, events=None):
        validate(fd)
        self.channel = socket.socket(fileno=os.dup(fd))
        os.set_inheritable(fd, False)
        os.close(fd)
        self.cancelled, self.expires = cancelled, expires
        self.requests = Requests(cancelled, expires, steering, approvals, events)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, name='agent-control', daemon=True)

    def _reply(self, result):
        import json
        pending = json.dumps(result, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()+b'\n'
        deadline = min(self.expires, time.monotonic()+0.25)
        while pending:
            if self.stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError('Control reply deadline')
            if select.select([], [self.channel], [], 0.02)[1]:
                try:
                    count = self.channel.send(pending, socket.MSG_DONTWAIT | socket.MSG_NOSIGNAL)
                except BlockingIOError:
                    continue
                if count == 0:
                    raise OSError('Control socket closed')
                pending = pending[count:]

    def _serve(self):
        buffer = b''
        total = 0
        try:
            while not self.stop.is_set():
                if time.monotonic() >= self.expires:
                    return  # The run deadline owns timeout classification.
                if not select.select([self.channel], [], [], 0.02)[0]:
                    continue
                try:
                    data = self.channel.recv(4096, socket.MSG_DONTWAIT)
                except BlockingIOError:
                    continue
                if not data:
                    self.cancelled.set()
                    return
                total += len(data)
                if total > 1024*1024:
                    raise ValueError('Control byte budget')
                buffer += data
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    self._reply(self.requests.dispatch(line))
                if len(buffer) > 16384:
                    raise ValueError('Control frame budget')
        except (OSError, ValueError, TypeError, RecursionError):
            if time.monotonic() < self.expires:
                self.cancelled.set()
        finally:
            self.requests.closed = True

    def close(self):
        self.stop.set()
        self.requests.closed = True
        self.thread.join(timeout=1)
        self.channel.close()
        if self.thread.is_alive():
            raise RuntimeError('Control reader did not terminate')


@contextmanager
def listen(fd, cancelled, expires, steering=None, approvals=None, events=None):
    if fd is None:
        yield
        return
    control = Control(fd, cancelled, expires, steering, approvals, events)
    control.thread.start()
    try:
        yield
    finally:
        control.close()
