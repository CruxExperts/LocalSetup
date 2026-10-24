"""Save one captured owner set across explicit process participants."""
from dataclasses import replace
import hashlib
import secrets
import time

from .live_configuration import ConfigurationError, ConfigurationStore, _encode
from .live_configuration_participant import ConfigurationParticipant, projection, scope


class ConfigurationGroup:
    """Bind a fixed cohort to the coordinator's existing manual/cadence flush."""
    def __init__(self, runtime, participants, *, timeout_seconds=300):
        if (type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600
                or type(participants) is not tuple or not 1 <= len(participants) <= 64
                or runtime.group is not None):
            raise ConfigurationError('configuration_participant_invalid')
        self.runtime, self.participants, self.timeout_seconds = runtime, participants, timeout_seconds
        runtime.group = self

    def flush(self, *, authorized):
        return flush_all(self.runtime, self.participants, authorized=authorized,
                         deadline=time.monotonic()+self.timeout_seconds)


def flush_all(runtime, participants, *, authorized, deadline):
    """Freeze proposals, validate all owners, persist once, then verify all reloads.

    Participants are trusted controller-selected Channels or local participants.
    Unknown dispatch outcomes remain pending; there is no automatic write retry.
    """
    if (authorized is not True or type(participants) is not tuple or len(participants) > 64
            or len({id(peer) for peer in participants}) != len(participants)
            or type(deadline) not in (int, float) or not 0 < deadline-time.monotonic() <= 3600):
        raise ConfigurationError('configuration_unauthorized')
    def check():
        runtime.store.authority.check()
        if time.monotonic() >= deadline:
            raise ConfigurationError('configuration_expired')
    local = runtime.participant or ConfigurationParticipant(runtime)
    if local in participants:
        raise ConfigurationError('configuration_participant_invalid')
    transaction = secrets.token_hex(32)
    envelope = dict(transaction=transaction, scope=scope(runtime))
    captured, dispatched = [], False
    def call(peer, method, **data):
        check()
        reply = peer.request(method, envelope | data)
        check()
        if type(reply) is not dict or reply.get('ok') is not True:
            raise ConfigurationError('configuration_participant_rejected')
        _encode(reply)
        return reply
    try:
        frozen = []
        for peer in (local, *participants):
            # Include the attempted peer in abort/reconciliation, even if its
            # capture acknowledgement disappears. No writes have started yet.
            captured.append(peer)
            frozen.append(call(peer, 'capture'))
        with runtime.lock:
            snapshot = runtime.accepted
            base = None if snapshot is None else snapshot.digest
            proposed = (dict(values={}, cadence_seconds=runtime.cadence, extensions={})
                        if snapshot is None else projection(snapshot.record))
        updates, cadence = {}, None
        for item in frozen:
            if (set(item) != {'ok', 'base', 'owners', 'values', 'cadence'} or item['base'] != base
                    or type(item['owners']) is not list or item['owners'] != sorted(set(item['owners']))
                    or not set(item['owners']) <= runtime.validators.keys()
                    or type(item['values']) is not dict or not set(item['values']) <= set(item['owners'])):
                raise ConfigurationError('configuration_conflict')
            for name, value in item['values'].items():
                if name in updates and updates[name] != value:
                    raise ConfigurationError('configuration_conflict')
                runtime._validate(name, _encode(value))
                updates[name] = value
            if item['cadence'] is not None:
                if type(item['cadence']) is not int or not 1 <= item['cadence'] <= 3600:
                    raise ConfigurationError('configuration_invalid')
                if cadence is not None and cadence != item['cadence']:
                    raise ConfigurationError('configuration_conflict')
                cadence = item['cadence']
        if updates or cadence is not None:
            proposed['values'].update(updates)
            if cadence is not None:
                proposed['cadence_seconds'] = cadence
            digest = hashlib.sha256(_encode(proposed)).hexdigest()
            for peer in captured:
                if call(peer, 'prepare', proposal=proposed).get('proposal') != digest:
                    raise ConfigurationError('configuration_conflict')
            authority = replace(runtime.store.authority, current_check=check)
            store = ConfigurationStore(runtime.store.root, runtime.store.store_id, authority)
            check()
            dispatched = True
            snapshot = store.commit(proposed['values'], expected_digest=base, authorized=authorized,
                cadence_seconds=proposed['cadence_seconds'], extensions=proposed['extensions'],
                provenance=runtime.provenance | {'operation': transaction})
        result = None if snapshot is None else snapshot.digest
        for peer in captured:
            try:
                if call(peer, 'finish', digest=result).get('digest') != result:
                    raise ConfigurationError('configuration_reload_pending', candidate=result)
            except Exception:
                raise ConfigurationError('configuration_reload_pending', candidate=result) from None
        return snapshot
    except Exception as error:
        # Before persistence is dispatched, abort only captured memory freezes;
        # proposals stay intact. After dispatch, preserve unknown outcomes for
        # signed-store recovery and the participants' existing cadence reload.
        safe_abort = not dispatched or isinstance(error, ConfigurationError) and error.code in (
            'configuration_conflict', 'configuration_busy', 'configuration_unauthorized',
            'configuration_invalid', 'configuration_limit', 'configuration_signing_failed', 'configuration_pending')
        if safe_abort:
            for peer in captured:
                try:
                    peer.request('abort', envelope)
                except Exception:
                    pass
        if isinstance(error, ConfigurationError):
            error.transaction = transaction
            raise
        failure = ConfigurationError('configuration_participant_unavailable')
        failure.transaction = transaction
        raise failure from None
