"""Bounded local owner status/cancellation, independent of model tool authority."""
from contextlib import contextmanager
import os
import select
import socket
import threading
import time

from .broker_rpc import _decode


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
        self.steering = steering
        self.approvals = approvals
        self.events = events
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, name='agent-control', daemon=True)

    def _reply(self, identifier, accepted=False, decided=False, events=None):
        import json
        status = 'cancellation_requested' if self.cancelled.is_set() else 'active'
        if accepted:
            status = 'queued'
        if decided:
            status = 'decision_recorded'
        result = {'schema_version': 1, 'id': identifier, 'status': status}
        if events is not None:
            result['events'] = events
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
        total = previous = 0
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
                    if len(line) > 16384:
                        raise ValueError('Control frame budget')
                    value = _decode(line)
                    if (not isinstance(value, dict) or not {'schema_version', 'id', 'method'} <= set(value)
                            or type(value['schema_version']) is not int or value['schema_version'] != 1
                            or type(value['id']) is not int or value['id'] != previous+1
                            or value['id'] > 1024 or value['method'] not in ('status', 'cancel', 'steer', 'approve',
                                'events.status', 'events.pause', 'events.resume', 'events.read')):
                        raise ValueError('Invalid control request')
                    events = None
                    if value['method'].startswith('events.'):
                        if self.events is None:
                            raise ValueError('Event retention is unavailable')
                        events = self.events.request(value)
                    elif value['method'] == 'steer':
                        if self.steering is None:
                            raise ValueError('Steering is unavailable')
                        self.steering.accept(value)
                    elif value['method'] == 'approve':
                        if self.approvals is None:
                            raise ValueError('Approvals are unavailable')
                        self.approvals.decide(value)
                    elif set(value) != {'schema_version', 'id', 'method'}:
                        raise ValueError('Invalid control fields')
                    previous = value['id']
                    if value['method'] == 'cancel':
                        self.cancelled.set()
                    self._reply(previous, value['method'] == 'steer', value['method'] == 'approve', events)
                if len(buffer) > 16384:
                    raise ValueError('Control frame budget')
        except (OSError, ValueError, TypeError, RecursionError):
            if time.monotonic() < self.expires:
                self.cancelled.set()

    def close(self):
        self.stop.set()
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
