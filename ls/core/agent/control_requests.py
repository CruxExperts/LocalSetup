"""Bounded owner-control dispatch, independent of its admitted transport.

This object does not authenticate callers or grant remote authority. Its owner
must admit the channel and supply the existing scoped control handlers.
"""
import hashlib
import json
import threading
import time

from .broker_rpc import _decode
from .operation_journal import CONTROL_METHODS


class ControlNotDispatched(ValueError):
    """The handler was never entered; journal intent may still need reconciliation."""


class Requests:
    def __init__(self, cancelled, expires, steering=None, approvals=None, events=None):
        self.cancelled, self.expires = cancelled, expires
        self.steering, self.approvals, self.events = steering, approvals, events
        self.previous = self.used = 0
        self.closed = False
        self._busy = threading.Lock()

    def dispatch_recorded(self, raw, journal, *, binding_sha256, request_id, timeout=30, reject_before_dispatch=False):
        """Record an already admitted request before invoking its handler.

        The caller owns authentication and the exact binding. This method does
        not infer authority from a digest or from previously recorded intent.
        """
        if type(reject_before_dispatch) is not bool:
            raise ValueError('Invalid control rejection mode')
        if not self._busy.acquire(blocking=False):
            raise PermissionError('Recorded control dispatch is already active')
        dispatching = False
        try:
            if self.closed or time.monotonic() >= self.expires:
                raise PermissionError('Control channel is closed or expired')
            if type(raw) is not bytes or not 1 <= len(raw) <= 16384:
                raise ValueError('Control frame budget')
            value = _decode(raw)
            if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                    or value['schema_version'] != 1):
                raise ValueError('Invalid control request')
            digest = hashlib.sha256(raw).hexdigest()
            operation = journal.begin('owner_control', {
                'binding_sha256': binding_sha256, 'request_sha256': digest,
                'request_id': request_id, 'sequence': value.get('id'),
                'method': value.get('method'), 'request_bytes': len(raw)}, timeout=timeout)
            # The persisted sequence, including prior uncertain requests, was
            # validated atomically. A fresh admitted instance may continue it.
            if self.previous == 0 and self.used == 0:
                self.previous = value['id']-1
            dispatching = True
            result = self._dispatch(raw)
            evidence = hashlib.sha256(json.dumps(result, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            journal.finish(operation, 'completed', evidence_sha256=evidence, timeout=timeout)
            if self.closed or time.monotonic() >= self.expires:
                raise PermissionError('Control authority ended before acknowledgement')
            return {'operation': operation, 'request_id': request_id, 'binding_sha256': binding_sha256,
                    'request_sha256': digest, 'result_sha256': evidence, 'result': result}
        except Exception:
            if reject_before_dispatch and not dispatching:
                raise ControlNotDispatched('Control handler was not entered') from None
            self.closed = True
            raise
        except BaseException:
            self.closed = True
            raise
        finally:
            self._busy.release()

    def dispatch(self, raw):
        if not self._busy.acquire(blocking=False):
            raise PermissionError('Control dispatch is already active')
        try:
            return self._dispatch(raw)
        except BaseException:
            self.closed = True
            raise
        finally:
            self._busy.release()

    def _dispatch(self, raw):
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
                or value['id'] > 1024 or value['method'] not in CONTROL_METHODS):
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
