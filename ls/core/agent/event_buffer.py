"""Bounded durable filtered views; not mutation evidence or execution authority."""
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import threading

from ..openpgp.container_storage import ensure_private_directory, read_private_file, write_private_file
from .broker_rpc import _decode
from .operation_journal import DIGEST, IDENTIFIER
from .runtime_lock import runtime_use

MAX_BYTES, MAX_EVENTS, MAX_LINES = 2*1024*1024, 256, 20000
MAX_EVENT = 1024*1024
MAX_TOTAL = 16*1024*1024
RUN = re.compile(r'[0-9a-f]{32}\Z')
STATUSES = {'completed', 'cancelled', 'timed_out', 'output_limit', 'failed'}


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def _identity(task, session, run):
    if (any(type(x) is not str or IDENTIFIER.fullmatch(x) is None for x in (task, session))
            or type(run) is not str or RUN.fullmatch(run) is None):
        raise ValueError('Invalid event buffer identity')


def _record(value):
    if (type(value) is not dict or set(value) != {'sequence', 'type', 'text', 'filter', 'status', 'checkpoint'}
            or type(value['sequence']) is not int or not 1 <= value['sequence'] <= 10001
            or value['type'] not in ('text', 'result')
            or value['text'] is not None and type(value['text']) is not str
            or value['status'] is not None and value['status'] not in STATUSES
            or value['checkpoint'] is not None and (type(value['checkpoint']) is not str
                                                   or DIGEST.fullmatch(value['checkpoint']) is None)):
        raise ValueError('Invalid retained event')
    metadata = value['filter']
    if (type(metadata) is not dict or set(metadata) != {'schema_version', 'action', 'orders', 'matches', 'policy_sha256'}
            or type(metadata['schema_version']) is not int or metadata['schema_version'] != 1
            or metadata['action'] not in ('allow', 'transform', 'block', 'quarantine')
            or type(metadata['orders']) is not list or len(metadata['orders']) > 64
            or any(type(n) is not int or not 1 <= n <= 2**31-1 for n in metadata['orders'])
            or metadata['orders'] != sorted(set(metadata['orders']))
            or type(metadata['matches']) is not int or not 0 <= metadata['matches'] <= 4096
            or type(metadata['policy_sha256']) is not str or DIGEST.fullmatch(metadata['policy_sha256']) is None
            or (value['text'] is None) != (metadata['action'] in ('block', 'quarantine'))):
        raise ValueError('Invalid retained filter metadata')
    if len(_encode(value)) > MAX_EVENT:
        raise ValueError('Retained event exceeds byte limit')


def _lines(record):
    return 0 if record['text'] is None else record['text'].count('\n')+1


def _snapshot(raw, *, task, session, run):
    _identity(task, session, run)
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise ValueError('Event buffer exceeds byte limit')
    value = _decode(raw)
    if (type(value) is not dict or set(value) != {'schema_version', 'task', 'session', 'run', 'last', 'total_bytes', 'events'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or (value['task'], value['session'], value['run']) != (task, session, run)
            or type(value['last']) is not int or not 0 <= value['last'] <= 10001
            or type(value['total_bytes']) is not int or not 0 <= value['total_bytes'] <= MAX_TOTAL
            or type(value['events']) is not list or len(value['events']) > MAX_EVENTS):
        raise ValueError('Invalid event snapshot')
    events = value['events']
    for index, record in enumerate(events, value['last']-len(events)+1):
        _record(record)
        if record['sequence'] != index:
            raise ValueError('Event sequence gap inside retained range')
    if ((not events and (value['last'] != 0 or value['total_bytes'] != 0))
            or value['total_bytes'] < sum(len(_encode(record)) for record in events)
            or sum(map(_lines, events)) > MAX_LINES or _encode(value) != raw):
        raise ValueError('Invalid event snapshot bounds or encoding')
    return value


def page(raw, *, task, session, run, after=0, limit=20, max_bytes=16384):
    if (type(after) is not int or not 0 <= after <= 10001
            or type(limit) is not int or not 1 <= limit <= MAX_EVENTS
            or type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_BYTES):
        raise ValueError('Invalid event page limits')
    value = _snapshot(raw, task=task, session=session, run=run)
    if after > value['last']:
        raise ValueError('Event cursor is ahead of this buffer')
    first = value['events'][0]['sequence'] if value['events'] else 1
    cursor = max(after, first-1)
    result = dict(schema_version=1, task=task, session=session, run=run,
        checkpoint=hashlib.sha256(raw).hexdigest(), first=first, last=value['last'],
        gap=None if after >= first-1 else {'from': after+1, 'through': first-1},
        events=[], next_cursor={'run': run, 'after': cursor}, more=cursor < value['last'], required_bytes=None)
    for event in value['events']:
        if event['sequence'] <= cursor:
            continue
        candidate = dict(result, events=[*result['events'], event],
            next_cursor={'run': run, 'after': event['sequence']}, more=event['sequence'] < value['last'])
        required = len(_encode(candidate))
        if required > max_bytes:
            if not result['events']:
                result['required_bytes'] = required
            break
        result = candidate
        if len(result['events']) == limit:
            break
    return result


def read(root, *, task, session, run, **limits):
    _identity(task, session, run)
    ensure_private_directory(root, create=False)
    raw = read_private_file(root/run/'events.json', maximum=MAX_BYTES)
    return page(raw, task=task, session=session, run=run, **limits)


class EventBuffer:
    def __init__(self, root: Path, *, task, session, run, check=lambda: None,
                 max_bytes=MAX_BYTES, max_events=MAX_EVENTS, max_lines=MAX_LINES):
        _identity(task, session, run)
        for value, maximum in ((max_bytes, MAX_BYTES), (max_events, MAX_EVENTS), (max_lines, MAX_LINES)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Invalid event retention limit')
        self.task, self.session, self.run, self.check = task, session, run, check
        self.maximum, self.count, self.lines = max_bytes, max_events, max_lines
        self.path = root/run/'events.json'
        self.failed, self.busy = False, threading.Lock()
        self.raw = _encode(dict(schema_version=1, task=task, session=session, run=run, last=0, total_bytes=0, events=[]))
        if len(self.raw) > max_bytes:
            raise ValueError('Event retention limit is too small')
        check()
        ensure_private_directory(root, create=True)
        with runtime_use(root, exclusive=True, timeout=0):
            with os.scandir(root) as entries:
                if len(list(islice(entries, 1001))) >= 1000:
                    raise ValueError('Event run inventory is full')
            (root/run).mkdir(mode=0o700)
            write_private_file(self.path, self.raw, replace=False)
        check()

    def append(self, view, *, kind='text', status=None, checkpoint=None):
        if not self.busy.acquire(blocking=False):
            raise ValueError('Event writer is busy')
        try:
            self.check()
            if self.failed:
                raise ValueError('Event publication requires reconciliation')
            value = _snapshot(self.raw, task=self.task, session=self.session, run=self.run)
            if type(view) is not dict or set(view) != {'text', 'filter'}:
                raise ValueError('Invalid filtered event view')
            event = dict(sequence=value['last']+1, type=kind, **view, status=status, checkpoint=checkpoint)
            _record(event)
            value['total_bytes'] += len(_encode(event))
            if value['total_bytes'] > MAX_TOTAL:
                raise ValueError('Retained event work budget exceeded')
            if _lines(event) > self.lines:
                raise ValueError('Retained event exceeds line limit')
            value['events'].append(event);value['last'] += 1
            while (len(value['events']) > self.count or sum(map(_lines, value['events'])) > self.lines
                   or len(_encode(value)) > self.maximum):
                if len(value['events']) == 1:
                    raise ValueError('Retained event exceeds buffer capacity')
                value['events'].pop(0)
            raw = _encode(value)
            self.check()
            self.failed = True  # Any uncertain publication closes this writer.
            if read_private_file(self.path, maximum=MAX_BYTES) != self.raw:
                raise ValueError('Event buffer changed outside its writer')
            write_private_file(self.path, raw, replace=True)
            self.raw, self.failed = raw, False
            self.check()
        finally:
            self.busy.release()

    def read(self, **limits):
        # The control reader uses the last acknowledged immutable bytes; it never
        # waits for the writer's filesystem I/O or acquires its mutation lock.
        return page(self.raw, task=self.task, session=self.session, run=self.run, **limits)
