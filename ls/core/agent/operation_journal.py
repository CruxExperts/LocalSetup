"""Durable operation evidence; unfinished intent never authorizes replay."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .file_grants import relative
from .runtime_lock import _directory, runtime_use, LOCK_NAME

DIGEST = re.compile(r'[0-9a-f]{64}\Z')
IDENTIFIER = re.compile(r'[a-zA-Z0-9_.:-]{1,128}\Z')
MAX_RECORD = 16384
MAX_RECORDS = 10000
MAX_TOTAL = 64 * 1024 * 1024
OUTCOMES = {'applied', 'not_applied', 'completed', 'failed', 'cancelled', 'timed_out', 'output_limit', 'uncertain'}
CONTROL_METHODS = frozenset({'status', 'cancel', 'steer', 'approve',
                             'events.status', 'events.pause', 'events.resume', 'events.read'})


def _encode(value):
    data = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if len(data) > MAX_RECORD:
        raise ValueError('Operation record exceeds 16 KiB')
    return data


def _request(kind, value):
    if not isinstance(value, dict):
        raise ValueError('Invalid operation request')
    if kind == 'file_replace' and set(value) in ({'path', 'before', 'after'}, {'path', 'before', 'after', 'root_sha256'},
            {'path', 'before', 'after', 'root_sha256', 'before_properties', 'after_properties'}):
        relative(value['path'])
        if len(value['path'].encode()) > 4096:
            raise ValueError('Operation path is oversized')
        hashes = [value['after']] + ([] if value['before'] is None else [value['before']])
        if 'root_sha256' in value:
            hashes.append(value['root_sha256'])
        if 'after_properties' in value:
            if (value['before'] is None) != (value['before_properties'] is None):
                raise ValueError('Inconsistent file property precondition')
            hashes += [value['after_properties']] + ([] if value['before_properties'] is None else [value['before_properties']])
    elif kind == 'process' and set(value) == {'argv_sha256', 'snapshot_sha256'}:
        hashes = list(value.values())
    elif kind == 'owner_control' and set(value) == {
            'binding_sha256', 'request_sha256', 'request_id', 'sequence', 'method', 'request_bytes'}:
        if (not isinstance(value['request_id'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', value['request_id'])
                or type(value['sequence']) is not int or not 1 <= value['sequence'] <= 1024
                or type(value['request_bytes']) is not int or not 1 <= value['request_bytes'] <= 16384
                or not isinstance(value['method'], str) or value['method'] not in CONTROL_METHODS):
            raise ValueError('Invalid owner control identity')
        hashes = [value['binding_sha256'], value['request_sha256']]
    else:
        raise ValueError('Unsupported operation request schema')
    if any(not isinstance(x, str) or not DIGEST.fullmatch(x) for x in hashes):
        raise ValueError('Operation request requires SHA-256 evidence')


class Journal:
    def __init__(self, root: Path, *, task: str, session: str):
        if any(not isinstance(x, str) or not IDENTIFIER.fullmatch(x) for x in (task, session)):
            raise ValueError('Journal requires bounded task and session identifiers')
        self.root, self.task, self.session = root.absolute(), task, session
        self._issued = set()

    def _load(self, directory, *, check=None):
        names = sorted(os.listdir(directory))
        records, previous, total = [], None, 0
        for name in names:
            if check is not None:
                check()
            if name == LOCK_NAME or re.fullmatch(r'\.pending-[0-9a-f]{32}', name):
                continue
            if name != f'{len(records):08d}.json' or len(records) >= MAX_RECORDS:
                raise ValueError('Operation journal inventory is invalid or exceeds its limit')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o7077:
                    raise ValueError('Operation record must be a private owned regular file')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    raw = stream.read(MAX_RECORD+1)
                total += len(raw)
                if len(raw) > MAX_RECORD or total > MAX_TOTAL:
                    raise ValueError('Operation journal exceeds byte limits')
                value = json.loads(raw)
                if not isinstance(value, dict) or _encode(value) != raw:
                    raise ValueError('Operation record must use canonical JSON')
            finally:
                os.close(fd)
            if (value.get('schema_version') != 1 or type(value.get('schema_version')) is not int
                    or value.get('sequence') != len(records) or type(value.get('sequence')) is not int
                    or value.get('previous') != previous or value.get('task') != self.task or value.get('session') != self.session):
                raise ValueError('Operation journal identity or hash chain mismatch')
            records.append(value)
            previous = hashlib.sha256(raw).hexdigest()
        self._states(records)
        return records, previous, total

    @staticmethod
    def _states(records):
        states, calls = {}, set()
        control_ids, control_binding, control_sequence, control_bytes = set(), None, 0, 0
        common = {'schema_version', 'sequence', 'previous', 'task', 'session', 'operation', 'type'}
        for record in records:
            operation = record.get('operation')
            if not isinstance(operation, str) or not re.fullmatch(r'[0-9a-f]{32}', operation):
                raise ValueError('Invalid operation identity')
            if record.get('type') == 'intent' and set(record) in (common | {'kind', 'request', 'checkpoint'}, common | {'kind', 'request', 'checkpoint', 'tool_call'}):
                _request(record['kind'], record['request'])
                control = record['kind'] == 'owner_control'
                if states and any((x['intent']['kind'] == 'owner_control') != control for x in states.values()):
                    raise ValueError('Owner control requires a separate journal')
                bypass = control and record['request']['method'] in {'status', 'cancel'}
                if operation in states or (not bypass and any(x['outcome'] == 'uncertain' for x in states.values())):
                    raise ValueError('Unreconciled or duplicate operation intent')
                if control:
                    request = record['request']
                    if ('tool_call' in record or record['checkpoint'] is not None
                            or request['request_id'] in control_ids
                            or request['sequence'] != control_sequence+1
                            or control_binding not in (None, request['binding_sha256'])):
                        raise ValueError('Owner control identity changed or was already accepted; reconcile without replay')
                    control_ids.add(request['request_id'])
                    control_binding, control_sequence = request['binding_sha256'], request['sequence']
                    control_bytes += request['request_bytes']+1
                    if control_bytes > 1024*1024:
                        raise ValueError('Owner control byte budget exceeded')
                if record['checkpoint'] is not None and (not isinstance(record['checkpoint'], str) or not DIGEST.fullmatch(record['checkpoint'])):
                    raise ValueError('Invalid checkpoint evidence digest')
                if 'tool_call' in record:
                    call = record['tool_call']
                    if not isinstance(call, dict) or set(call) != {'run_id','call_id','name','arguments_sha256'} or record['checkpoint'] is None:
                        raise ValueError('Tool operation requires a checkpoint and explicit call identity')
                    if any(not isinstance(call[k], str) or not IDENTIFIER.fullmatch(call[k]) for k in ('run_id','call_id','name')) or not isinstance(call['arguments_sha256'], str) or not DIGEST.fullmatch(call['arguments_sha256']):
                        raise ValueError('Invalid tool call identity or argument digest')
                    identity = (call['run_id'], call['call_id'])
                    if identity in calls:
                        raise ValueError('Tool call already has an operation; reconcile without replay')
                    calls.add(identity)
                states[operation] = {'intent': record, 'outcome': 'uncertain'}
            elif record.get('type') == 'outcome' and set(record) == common | {'outcome', 'evidence_sha256', 'reconciled'}:
                state = states.get(operation)
                if (state is None or state['outcome'] != 'uncertain' or record['outcome'] not in OUTCOMES
                        or type(record['reconciled']) is not bool or not isinstance(record['evidence_sha256'], str)
                        or not DIGEST.fullmatch(record['evidence_sha256'])):
                    raise ValueError('Invalid operation outcome transition')
                if state['intent']['kind'] == 'file_replace' and record['outcome'] not in {'applied', 'not_applied', 'uncertain'}:
                    raise ValueError('Invalid file replacement outcome')
                if state['intent']['kind'] == 'process' and record['outcome'] == 'applied':
                    raise ValueError('Invalid process outcome')
                if state['intent']['kind'] == 'owner_control' and record['outcome'] not in {'completed', 'uncertain'}:
                    raise ValueError('Invalid owner control outcome')
                if 'result' in state and not record['reconciled']:
                    raise ValueError('Uncertain outcome requires reconciliation')
                state.update(outcome=record['outcome'], result=record)
            else:
                raise ValueError('Unsupported operation record schema')
        return states

    def _append(self, value, *, timeout):
        value = json.loads(_encode(value))
        with runtime_use(self.root, exclusive=True, timeout=timeout):
            directory = _directory(self.root)
            try:
                if os.fstat(directory).st_mode & 0o077:
                    raise ValueError('Journal root must be private')
                records, previous, total = self._load(directory)
                value = {**value, 'schema_version': 1, 'sequence': len(records), 'previous': previous,
                         'task': self.task, 'session': self.session}
                states = self._states([*records, value])
                raw = _encode(value)
                reserve = int(value['type'] == 'intent' or value.get('outcome') == 'uncertain')
                if states and next(iter(states.values()))['intent']['kind'] == 'owner_control':
                    reserve = sum(state['outcome'] == 'uncertain' for state in states.values())
                if len(records) + 1 + reserve > MAX_RECORDS or total + len(raw) + reserve * MAX_RECORD > MAX_TOTAL:
                    raise ValueError('Operation journal is full')
                temporary = '.pending-' + uuid.uuid4().hex
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw);stream.flush();os.fsync(stream.fileno())
                os.rename(temporary, f'{len(records):08d}.json', src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
        return value

    def begin(self, kind: str, request: dict, *, checkpoint: str | None = None, tool_call: dict | None = None, timeout: float = 30) -> str:
        operation = uuid.uuid4().hex
        self._append({'type': 'intent', 'operation': operation, 'kind': kind,
                      'request': request, 'checkpoint': checkpoint, **({'tool_call': tool_call} if tool_call is not None else {})}, timeout=timeout)
        self._issued.add(operation)
        return operation

    def finish(self, operation: str, outcome: str, *, evidence_sha256: str, reconciled: bool = False, timeout: float = 30) -> None:
        if operation not in self._issued and not reconciled:
            raise PermissionError('Recovered operations require explicit reconciliation')
        self._append({'type': 'outcome', 'operation': operation, 'outcome': outcome,
                      'evidence_sha256': evidence_sha256, 'reconciled': reconciled}, timeout=timeout)
        self._issued.discard(operation)

    def inspect(self, *, timeout: float = 30) -> dict:
        with runtime_use(self.root, timeout=timeout):
            directory = _directory(self.root)
            try:
                if os.fstat(directory).st_mode & 0o077:
                    raise ValueError('Journal root must be private')
                records, _, _ = self._load(directory)
                return self._states(records)
            finally:
                os.close(directory)

    def frontier(self, *, timeout: float = 30) -> str:
        """Fingerprint exact validated history, including terminal reconciliation."""
        with runtime_use(self.root, timeout=timeout):
            directory = _directory(self.root)
            try:
                if os.fstat(directory).st_mode & 0o077:
                    raise ValueError('Journal root must be private')
                records, previous, _ = self._load(directory)
                return hashlib.sha256(_encode({'task': self.task, 'session': self.session,
                                               'count': len(records), 'head': previous})).hexdigest()
            finally:
                os.close(directory)

    def control_observation(self, *, binding_sha256, request_id, request_sha256, timeout=30):
        """Read one original control outcome and its exact journal frontier.

        Absence is only an observation, never authority to retry an operation.
        """
        if (any(not isinstance(value, str) or not DIGEST.fullmatch(value)
                for value in (binding_sha256, request_sha256))
                or not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id)):
            raise ValueError('Invalid control observation identity')
        with runtime_use(self.root, timeout=timeout):
            directory = _directory(self.root)
            try:
                if os.fstat(directory).st_mode & 0o077:
                    raise ValueError('Journal root must be private')
                records, previous, _ = self._load(directory)
                result = dict(binding_sha256=binding_sha256, request_id=request_id,
                    request_sha256=request_sha256, state='not_observed', operation=None,
                    result_sha256=None, frontier=hashlib.sha256(_encode({
                        'task': self.task, 'session': self.session,
                        'count': len(records), 'head': previous})).hexdigest())
                for operation, state in self._states(records).items():
                    intent = state['intent']
                    if (intent['kind'] != 'owner_control'
                            or intent['request']['binding_sha256'] != binding_sha256):
                        raise PermissionError('Journal does not belong to the admitted control binding')
                    if intent['request']['request_id'] == request_id:
                        if intent['request']['request_sha256'] != request_sha256:
                            raise PermissionError('Control request digest differs from accepted intent')
                        result.update(state=state['outcome'], operation=operation,
                            result_sha256=state['result']['evidence_sha256']
                            if state['outcome'] == 'completed' else None)
                return result
            finally:
                os.close(directory)


    def after(self, frontier: str, *, timeout: float = 30) -> dict:
        """Prove an exact settled prefix and return its subsequent operations."""
        if not isinstance(frontier, str) or not DIGEST.fullmatch(frontier):
            raise ValueError('Invalid journal frontier')
        with runtime_use(self.root, timeout=timeout):
            directory = _directory(self.root)
            try:
                if os.fstat(directory).st_mode & 0o077:
                    raise ValueError('Journal root must be private')
                records, previous, _ = self._load(directory)
                def fingerprint(count, head):
                    return hashlib.sha256(_encode({'task': self.task, 'session': self.session,
                                                   'count': count, 'head': head})).hexdigest()
                position = None
                head = None
                for count in range(len(records)+1):
                    if fingerprint(count, head) == frontier:
                        position = count
                        break
                    if count < len(records):
                        head = hashlib.sha256(_encode(records[count])).hexdigest()
                if position is None:
                    raise PermissionError('Checkpoint frontier is not in this journal')
                if any(value['outcome'] == 'uncertain' for value in self._states(records[:position]).values()):
                    raise PermissionError('Recovery checkpoint prefix contains uncertain operations')
                states = self._states(records)
                return {'frontier': fingerprint(len(records), previous),
                        'operations': {key: value for key, value in states.items()
                                       if value['intent']['sequence'] >= position}}
            finally:
                os.close(directory)
