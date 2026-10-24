"""Capture process-local proposals without granting signing authority to IPC."""
from contextlib import contextmanager
import hashlib

from .live_configuration import ConfigurationError, _decode, _digest, _encode


def projection(record):
    return {key: record[key] for key in ('values', 'cadence_seconds', 'extensions')}


def scope(runtime):
    authority = runtime.store.authority
    return hashlib.sha256(_encode([str(runtime.store.root), runtime.store.store_id,
                                  authority.signer, list(authority.recipients)])).hexdigest()


@contextmanager
def _capture_lock(runtime):
    if not runtime.lock.acquire(blocking=False):
        raise ConfigurationError('configuration_busy')
    try:
        yield
    finally:
        runtime.lock.release()


class ConfigurationParticipant:
    """One explicitly admitted runtime, served by its existing process owner.

    An inherited Channel supplies identity, sequence, method and deadline bounds.
    No listener, discovery, background polling loop or signing API is installed.
    A captured sequence is immutable; later stages remain pending after reload.
    """
    def __init__(self, runtime):
        with runtime.lock:
            if runtime.participant is not None:
                raise ConfigurationError('configuration_busy')
            self.runtime, self.pending = runtime, None
            runtime.participant = self

    def request(self, method, data):
        with _capture_lock(self.runtime):
            self.runtime.store.authority.check()
            if (type(data) is not dict or not _digest(data.get('transaction'))
                    or data.get('scope') != scope(self.runtime)):
                raise ConfigurationError('configuration_participant_invalid')
            _encode(data)
            transaction = data['transaction']
            expected = {'transaction', 'scope'}
            if method == 'prepare':
                expected.add('proposal')
            elif method == 'finish':
                expected.add('digest')
            if set(data) != expected:
                raise ConfigurationError('configuration_participant_invalid')
            if method == 'finish' and data['digest'] is not None and not _digest(data['digest']):
                raise ConfigurationError('configuration_participant_invalid')
            if method == 'capture':
                return self._capture(transaction)
            if method == 'finish' and self.pending is None:
                snapshot = self.runtime.reload()
                if (snapshot is not None and snapshot.digest == data['digest']
                        and snapshot.record['provenance'] is not None
                        and snapshot.record['provenance']['operation'] == transaction):
                    return {'ok': True, 'digest': snapshot.digest}
            if self.pending is None or self.pending['transaction'] != transaction:
                raise ConfigurationError('configuration_pending')
            if method == 'prepare':
                return self._prepare(data['proposal'])
            if method == 'abort':
                intent = self.runtime.store._intent()
                current = self.runtime.store.load()
                if intent is not None and intent['candidate'] != (None if current is None else current.digest):
                    raise ConfigurationError('configuration_pending', candidate=intent['candidate'])
                self.runtime._validate_set(current)
                if self.reconcile(current):
                    self.runtime.accepted = current
                self.pending = None
                return {'ok': True}
            if method == 'finish':
                if self.pending['proposal'] is None:
                    self.pending = None  # No persistence was requested.
                snapshot = self.runtime.reload()
                digest = None if snapshot is None else snapshot.digest
                if digest != data['digest'] or self.pending is not None:
                    raise ConfigurationError('configuration_reload_pending', candidate=data['digest'])
                return {'ok': True, 'digest': digest}
            raise ConfigurationError('configuration_participant_invalid')

    def _capture(self, transaction):
        runtime = self.runtime
        # Observation may resolve a previous lost publication acknowledgement.
        # It never publishes an unpublished candidate or replays a write.
        snapshot = runtime.reload()
        if self.pending is not None or runtime.uncertain is not None:
            raise ConfigurationError('configuration_pending')
        base = None if snapshot is None else snapshot.digest
        self.pending = dict(transaction=transaction, base=base, proposal=None,
            versions=dict(runtime.staged_versions), cadence_version=runtime.cadence_version,
            values={name: _decode(raw) for name, raw in runtime.staged.items()},
            cadence=runtime.staged_cadence)
        return dict(ok=True, base=base, owners=sorted(runtime.validators),
                    values=self.pending['values'], cadence=self.pending['cadence'])

    def _prepare(self, proposed):
        runtime, pending = self.runtime, self.pending
        if (type(proposed) is not dict or set(proposed) != {'values', 'cadence_seconds', 'extensions'}
                or type(proposed['values']) is not dict or type(proposed['extensions']) is not dict
                or type(proposed['cadence_seconds']) is not int or not 1 <= proposed['cadence_seconds'] <= 3600):
            raise ConfigurationError('configuration_invalid')
        for name in runtime.validators:
            if name not in proposed['values']:
                raise ConfigurationError('configuration_incomplete')
            runtime._validate(name, _encode(proposed['values'][name]))
        if (any(proposed['values'].get(name) != value for name, value in pending['values'].items())
                or pending['cadence'] is not None and pending['cadence'] != proposed['cadence_seconds']):
            raise ConfigurationError('configuration_conflict')
        raw = _encode(proposed)
        if pending['proposal'] not in (None, raw):
            raise ConfigurationError('configuration_conflict')
        pending['proposal'] = raw
        return {'ok': True, 'proposal': hashlib.sha256(raw).hexdigest()}

    def reconcile(self, snapshot):
        pending, runtime = self.pending, self.runtime
        if pending is None or pending['proposal'] is None or snapshot is None:
            return False
        record = snapshot.record
        if (record['parent'] != pending['base'] or record['provenance'] is None
                or record['provenance']['operation'] != pending['transaction']
                or _encode(projection(record)) != pending['proposal']):
            return False
        for name in pending['values']:
            if runtime.staged_versions.get(name) == pending['versions'][name]:
                runtime.staged.pop(name, None)
        if runtime.cadence_version == pending['cadence_version']:
            runtime.staged_cadence = None
        self.pending = None
        runtime._event('applied', 'aggregate_verified', snapshot.digest)
        return True

    def serve_once(self, channel):
        def handle(method, data):
            try:
                return self.request(method, data)
            except ConfigurationError as error:
                return {'ok': False, 'code': error.code, 'candidate': error.candidate}
        try:
            channel.serve_once(handle, check=self.runtime.store.authority.check)
        except Exception:
            with self.runtime.lock:
                if self.pending is not None and self.pending['proposal'] is None:
                    self.pending = None  # No proposal could have been approved.
            raise
