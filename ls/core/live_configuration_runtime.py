"""One cadence for staged configuration writes and verified external reloads.

Owners read from one accepted immutable snapshot. Validation succeeds for every
registered owner before that snapshot changes; native effect application remains
with each owner's existing authority and transaction checks.
"""
from collections import deque
import math
import time

from .live_configuration import ConfigurationError, ConfigurationStore, _NAME, _decode, _digest, _encode


class ConfigurationRuntime:
    def __init__(self, store, validators, *, actor, machine, operation, clock=time.monotonic):
        if (not isinstance(store, ConfigurationStore) or type(validators) is not dict
                or not 1 <= len(validators) <= 64
                or any(type(name) is not str or _NAME.fullmatch(name) is None or not callable(check)
                       for name, check in validators.items())
                or any(not _digest(value) for value in (actor, machine, operation)) or not callable(clock)):
            raise ConfigurationError('configuration_invalid')
        self.store, self.validators, self.clock = store, dict(validators), clock
        self.provenance = dict(actor=actor, machine=machine, operation=operation)
        self.accepted, self.uncertain = None, None
        self.staged, self.staged_cadence = {}, None
        self.events = deque(maxlen=128)
        self.next_due = 0.0
        self.reload()
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ConfigurationError('configuration_invalid')
        self.next_due = now + self.cadence

    @property
    def cadence(self):
        return 60 if self.accepted is None else self.accepted.record['cadence_seconds']

    @property
    def dirty(self):
        return bool(self.staged) or self.staged_cadence is not None

    def _event(self, action, result, candidate=None):
        self.events.append(dict(self.provenance, action=action, result=result,
            revision=0 if self.accepted is None else self.accepted.revision,
            digest=candidate if candidate is not None else None if self.accepted is None else self.accepted.digest))

    def _validate(self, name, raw):
        value = _decode(raw)
        if name not in self.validators or type(value) is not dict:
            raise ConfigurationError('configuration_invalid')
        try:
            if self.validators[name](value) is not None or _encode(value) != raw:
                raise ValueError
        except Exception:
            raise ConfigurationError('configuration_invalid') from None

    def _validate_set(self, snapshot):
        if snapshot is not None:
            values = snapshot.record['values']
            for name in self.validators:
                if name not in values:
                    raise ConfigurationError('configuration_incomplete')
                self._validate(name, _encode(values[name]))

    def value(self, name):
        self.store.authority.check()
        if name not in self.validators or self.accepted is None:
            raise ConfigurationError('configuration_unavailable')
        return self.accepted.record['values'][name]

    def stage(self, name, value):
        try:
            raw = _encode(value)
            self._validate(name, raw)
            if self.uncertain is not None:
                raise ConfigurationError('configuration_pending')
            self.staged[name] = raw
            self._event('proposed', 'validated')
        except ConfigurationError as error:
            self._event('rejected', error.code)
            raise

    def stage_cadence(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 3600:
            self._event('rejected', 'configuration_invalid')
            raise ConfigurationError('configuration_invalid')
        if self.uncertain is not None:
            self._event('rejected', 'configuration_pending')
            raise ConfigurationError('configuration_pending')
        self.staged_cadence = seconds
        self._event('proposed', 'validated')

    def reload(self):
        try:
            snapshot = self.store.load(minimum_revision=0 if self.accepted is None else self.accepted.revision,
                ancestor_digest=None if self.accepted is None else self.accepted.digest)
            self._validate_set(snapshot)
            old = None if self.accepted is None else self.accepted.digest
            new = None if snapshot is None else snapshot.digest
            if self.dirty and new not in (old, self.uncertain):
                raise ConfigurationError('configuration_conflict')
            if self.uncertain is not None and new == self.uncertain:
                self.staged.clear()
                self.staged_cadence = self.uncertain = None
            self.accepted = snapshot
            if new != old:
                self._event('applied', 'verified')
            return snapshot
        except ConfigurationError as error:
            self._event('rejected', error.code)
            raise

    def discard_staged(self):
        """Explicitly abandon proposals; uncertain persistence must be resolved."""
        if self.uncertain is not None:
            raise ConfigurationError('configuration_pending')
        self.staged.clear()
        self.staged_cadence = None
        return self.reload()

    def flush(self, *, authorized):
        if not self.dirty and self.uncertain is None:
            return self.reload()
        try:
            if self.uncertain is not None:
                result = self.store.recover(authorized=authorized, candidate_digest=self.uncertain)
            else:
                values = {} if self.accepted is None else self.accepted.record['values']
                values.update({name: _decode(raw) for name, raw in self.staged.items()})
                if any(name not in values for name in self.validators):
                    raise ConfigurationError('configuration_incomplete')
                for name in self.validators:
                    self._validate(name, _encode(values[name]))
                result = self.store.commit(values,
                    expected_digest=None if self.accepted is None else self.accepted.digest,
                    authorized=authorized, cadence_seconds=self.cadence if self.staged_cadence is None else self.staged_cadence,
                    provenance=self.provenance)
            self.uncertain = result.digest
            self._event('persisted', 'verified', result.digest)
            # Re-read the complete signed revision after persistence. Validation
            # or competing changes retain the last accepted values and proposals.
            return self.reload()
        except ConfigurationError as error:
            if error.code == 'configuration_uncertain':
                self.uncertain = error.candidate
            self._event('rejected', error.code, error.candidate)
            raise

    def poll(self, *, authorized=False):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ConfigurationError('configuration_invalid')
        if now < self.next_due:
            return False
        try:
            self.flush(authorized=authorized) if self.dirty or self.uncertain else self.reload()
            return True
        finally:
            self.next_due = now + self.cadence
