"""Thin inherited-descriptor adapter for the shared configuration transaction."""
from contextlib import ExitStack, contextmanager
import os
import socket
import time

from .agent.broker_rpc import Channel
from .live_configuration import ConfigurationError, _digest
from .live_configuration_group import flush_all
from .live_configuration_participant import scope

METHODS = frozenset({'capture', 'prepare', 'finish', 'abort'})


@contextmanager
def connected(runtime, descriptors, *, cohort, timeout_seconds, authorized):
    """Only an owning supervisor can hand the CLI its admitted process channels."""
    if (authorized is not True or not _digest(cohort) or type(descriptors) is not list
            or not 1 <= len(descriptors) <= 64 or len(set(descriptors)) != len(descriptors)
            or any(type(fd) is not int or fd < 3 for fd in descriptors)
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600):
        raise ConfigurationError('configuration_participant_invalid')
    deadline = time.monotonic()+timeout_seconds
    with ExitStack() as stack:
        channels = []
        for fd in descriptors:
            connection = socket.socket(fileno=os.dup(fd))
            stack.callback(connection.close)
            os.set_inheritable(fd, False)
            channel = Channel(connection, task=scope(runtime), session=cohort,
                              methods=METHODS, expires=deadline)
            stack.callback(channel.close)
            channels.append(channel)
        yield tuple(channels), deadline


def flush_connected(runtime, descriptors, **options):
    with connected(runtime, descriptors, **options) as (channels, deadline):
        return flush_all(runtime, channels, authorized=options['authorized'], deadline=deadline)


def reconcile_connected(runtime, descriptors, *, snapshot=None, transaction=None, **options):
    """Finish a verified retained revision, or explicitly cancel an uncommitted capture.

    Cancellation requires the owning controller to stop/reconcile its previous
    dispatch first. It releases captured copies, never the pending owner edits.
    """
    with connected(runtime, descriptors, **options) as (channels, _deadline):
        reconcile_channels(runtime, channels, snapshot=snapshot, transaction=transaction)


def reconcile_channels(runtime, channels, *, snapshot=None, transaction=None):
    if snapshot is not None:
        record = snapshot.record
        if record['provenance'] is None:
            raise ConfigurationError('configuration_participant_invalid')
        transaction = record['provenance']['operation']
    if not _digest(transaction):
        raise ConfigurationError('configuration_participant_invalid')
    for channel in channels:
        data = dict(scope=scope(runtime), transaction=transaction)
        if snapshot is not None:
            data['digest'] = snapshot.digest
        try:
            result = channel.request('abort' if snapshot is None else 'finish', data)
            if result.get('ok') is not True:
                candidate = result.get('candidate')
                raise ConfigurationError('configuration_participant_rejected',
                                         candidate=candidate if _digest(candidate) else None)
            if snapshot is not None and result.get('digest') != snapshot.digest:
                raise ValueError
        except Exception as error:
            if isinstance(error, ConfigurationError):
                raise
            raise ConfigurationError('configuration_reload_pending',
                                     candidate=None if snapshot is None else snapshot.digest) from None
