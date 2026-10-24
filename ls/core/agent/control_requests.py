"""Bounded owner-control dispatch, independent of its admitted transport.

This object does not authenticate callers or grant remote authority. Its owner
must admit the channel and supply the existing scoped control handlers.
"""
import threading
import time

from .broker_rpc import _decode


class Requests:
    def __init__(self, cancelled, expires, steering=None, approvals=None, events=None):
        self.cancelled, self.expires = cancelled, expires
        self.steering, self.approvals, self.events = steering, approvals, events
        self.previous = self.used = 0
        self.closed = False
        self._busy = threading.Lock()

    def dispatch(self, raw):
        if not self._busy.acquire(blocking=False):
            raise PermissionError('Control dispatch is already active')
        try:
            if self.closed or time.monotonic() >= self.expires:
                raise PermissionError('Control channel is closed or expired')
            if type(raw) is not bytes or len(raw) > 16384:
                raise ValueError('Control frame budget')
            self.used += len(raw) + 1
            if self.used > 1024*1024:
                raise ValueError('Control byte budget')
            value = _decode(raw)
            if (not isinstance(value, dict) or not {'schema_version', 'id', 'method'} <= set(value)
                    or type(value['schema_version']) is not int or value['schema_version'] != 1
                    or type(value['id']) is not int or value['id'] != self.previous+1
                    or value['id'] > 1024 or value['method'] not in ('status', 'cancel', 'steer', 'approve',
                        'events.status', 'events.pause', 'events.resume', 'events.read')):
                raise ValueError('Invalid control request')
            # Consume identity before calling a handler: a failed callback may
            # already have applied its effect and must never be retried here.
            self.previous = value['id']
            events = None
            method = value['method']
            if method.startswith('events.'):
                if self.events is None:
                    raise ValueError('Event retention is unavailable')
                events = self.events.request(value)
            elif method == 'steer':
                if self.steering is None:
                    raise ValueError('Steering is unavailable')
                self.steering.accept(value)
            elif method == 'approve':
                if self.approvals is None:
                    raise ValueError('Approvals are unavailable')
                self.approvals.decide(value)
            elif set(value) != {'schema_version', 'id', 'method'}:
                raise ValueError('Invalid control fields')
            if method == 'cancel':
                self.cancelled.set()
            if self.closed or time.monotonic() >= self.expires:
                raise PermissionError('Control deadline ended during dispatch')
            status = 'cancellation_requested' if self.cancelled.is_set() else 'active'
            if method == 'steer':
                status = 'queued'
            elif method == 'approve':
                status = 'decision_recorded'
            result = {'schema_version': 1, 'id': self.previous, 'status': status}
            if events is not None:
                result['events'] = events
            return result
        except BaseException:
            self.closed = True
            raise
        finally:
            self._busy.release()
