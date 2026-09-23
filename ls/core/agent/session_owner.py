"""Exclusive live session ownership and recovery-gated broker dispatch."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

from .file_broker import FileBroker
from .file_recovery import reconcile, root_digest
from .operation_journal import IDENTIFIER, Journal
from .process_broker import run_recorded
from .runtime_install import _write_json
from .runtime_lock import _directory, runtime_use


class _Revocation:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)


def _separate(left, right):
    left, right = left.resolve(), right.resolve()
    if left.is_relative_to(right) or right.is_relative_to(left):
        raise ValueError('Session state must be separate from execution and target trees')


def _private(root):
    fd = _directory(root)
    if os.fstat(fd).st_mode & 0o077:
        os.close(fd)
        raise ValueError('Session state must be private')
    return fd


class SessionOwner:
    """Created only by lease(); persisted identity does not confer authority."""
    def __init__(self, root, journal, identity, expires, revoked):
        self.root, self._journal, self._identity = root, journal, identity
        self.expires, self._closed = expires, threading.Event()
        self.revoked = _Revocation(revoked, self._closed)
        self._thread, self._busy = threading.get_ident(), threading.Lock()
        self._bound_broker_source = None
        self._bound_broker = None

    def _check(self):
        if threading.get_ident() != self._thread or self.revoked.is_set() or time.monotonic() >= self.expires:
            raise PermissionError('Session owner is closed, revoked, expired or on another thread')

    @contextmanager
    def _operation(self, *, recovery=False):
        self._check()
        if not self._busy.acquire(blocking=False):
            raise PermissionError('Session dispatch is already active')
        try:
            states = self._journal.inspect(timeout=max(0, self.expires-time.monotonic()))
            self._check()
            if not recovery and any(state['outcome'] == 'uncertain' for state in states.values()):
                raise PermissionError('Session requires reconciliation before dispatch')
            yield states
        finally:
            self._busy.release()

    def inspect(self):
        with self._operation(recovery=True) as states:
            return states

    def _broker(self, broker):
        grant = broker.grant
        if (grant.task, grant.session) != (self._journal.task, self._journal.session):
            raise PermissionError('File grant does not belong to this session task')
        if root_digest(grant.root) != self._identity['workspace_sha256']:
            raise PermissionError('File grant workspace identity differs from session')
        _separate(self.root.parent, broker.lease_root)
        if self._bound_broker_source is not broker:
            if self._bound_broker is not None:
                self._bound_broker.clear_page_cache()
            self._bound_broker = FileBroker(
                replace(grant, expires=min(grant.expires, self.expires),
                        revoked=_Revocation(grant.revoked, self.revoked)),
                broker.lease_root,
            )
            self._bound_broker_source = broker
        bound = self._bound_broker
        if bound is None:
            raise RuntimeError('Session file broker was not bound')
        return bound

    def write(self, broker, name, data, *, expected_before, checkpoint=None, tool_call=None, profile=None):
        with self._operation():
            value = self._checkpoint(checkpoint) if checkpoint is not None else None
            if tool_call is not None and (value is None or value['run_id'] != tool_call.get('run_id') or value['profile'] != profile):
                raise PermissionError('Tool operation requires a matching run/profile checkpoint')
            bound = self._broker(broker)
            return bound.write_recorded(self._journal.task, self._journal.session, name, data,
                                        expected_before=expected_before, journal=self._journal, checkpoint=checkpoint, tool_call=tool_call)

    def read_text(self, broker, name, *, for_provider=False):
        """Read context/search inputs under their existing whole-text limits."""
        with self._operation():
            bound = self._broker(broker)
            raw = bound.read(self._journal.task, self._journal.session, name, for_provider=for_provider)
            result = {'content': raw.decode('utf-8'), 'sha256': hashlib.sha256(raw).hexdigest()}
            bound.grant.check(self._journal.task, self._journal.session, 'read', name, provider=for_provider)
            self._check()
            return result

    def read_page(self, broker, name, cursor=None, *, for_provider=False,
                  gnupg_home=None, policy=None, local_trust=None,
                  passphrase_reference=None, secret_resolver=None):
        with self._operation():
            bound = self._broker(broker)
            result = bound.read_page(
                self._journal.task, self._journal.session, name, cursor,
                for_provider=for_provider, gnupg_home=gnupg_home, policy=policy,
                local_trust=local_trust, passphrase_reference=passphrase_reference,
                secret_resolver=secret_resolver,
            )
            self._check()
            return result


    def reconcile_file(self, broker, operation):
        with self._operation(recovery=True):
            return reconcile(self._broker(broker), self._journal, operation,
                             task=self._journal.task, session=self._journal.session)

    def run(self, runtimes, grant, *, snapshot_sha256, provider=False, cancel=None, checkpoint=None):
        with self._operation():
            if checkpoint is not None:
                self._checkpoint(checkpoint)
            for boundary in (runtimes, grant.staging, Path('/usr')):
                _separate(self.root.parent, boundary)
            bound = replace(grant, expires=min(grant.expires, self.expires),
                            revoked=_Revocation(grant.revoked, self.revoked))
            return run_recorded(runtimes, bound, self._journal, snapshot_sha256=snapshot_sha256,
                                task=self._journal.task, session=self._journal.session,
                                provider=provider, cancel=cancel, checkpoint=checkpoint)


    def _checkpoints(self):
        from .checkpoint_store import Checkpoints
        root = self.root/'checkpoints'
        fd = _private(self.root)
        try:
            try:
                os.mkdir('checkpoints', mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            os.fsync(fd)
        finally:
            os.close(fd)
        return Checkpoints(root)

    def _checkpoint(self, digest):
        value = self._checkpoints().load(digest, timeout=max(0, self.expires-time.monotonic()))
        if (value['task'], value['session']) != (self._journal.task, self._journal.session):
            raise PermissionError('Checkpoint identity differs from session')
        if value['frontier'] != self._journal.frontier(timeout=max(0, self.expires-time.monotonic())):
            raise PermissionError('Checkpoint is stale relative to operation evidence')
        self._check()
        return value

    def save_checkpoint(self, messages, *, profile, run_id, step, state):
        with self._operation(recovery=True) as operations:
            if state == 'complete' and any(value['outcome'] == 'uncertain' for value in operations.values()):
                raise PermissionError('Uncertain operations cannot produce a complete checkpoint')
            from .checkpoint_store import MAX_MESSAGES
            if not isinstance(messages, bytes) or len(messages) > MAX_MESSAGES:
                raise ValueError('Checkpoint messages must be bounded serialized SDK bytes')
            value = {'schema_version': 1, 'task': self._journal.task, 'session': self._journal.session,
                     'profile': profile, 'run_id': run_id, 'step': step, 'state': state,
                     'frontier': self._journal.frontier(timeout=max(0, self.expires-time.monotonic())),
                     'messages': messages.decode('utf-8')}
            result = self._checkpoints().save(value, timeout=max(0, self.expires-time.monotonic()))
            self._check()
            return result

    def resume_checkpoint(self, digest, *, profile):
        with self._operation():
            value = self._checkpoint(digest)
            if value['state'] != 'complete' or value['profile'] != profile:
                raise PermissionError('Checkpoint requires settled history and a compatible profile')
            result = value['messages'].encode('utf-8')
            self._check()
            return result


@contextmanager
def lease(state: Path, *, task: str, session: str, workspace: Path, expires: float, revoked=None, create=True, new=False):
    """Own a canonical session until context exit; acquire before child leases."""
    if type(new) is not bool or (new and not create):
        raise ValueError('New session requires explicit creation mode')
    if type(create) is not bool:
        raise ValueError('Session creation mode must be explicit boolean')
    if any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in (task, session)):
        raise ValueError('Session requires bounded task and session identifiers')
    if not math.isfinite(expires) or expires <= time.monotonic():
        raise PermissionError('Session requires a live finite deadline')
    revoked = revoked if revoked is not None else threading.Event()
    if revoked.is_set():
        raise PermissionError('Session is revoked')
    state = state.absolute()
    _separate(state, workspace)
    name = hashlib.sha256(session.encode()).hexdigest()
    fd = _private(state)
    try:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                if new:
                    raise FileExistsError('Branch destination session already exists')
            os.fsync(fd)
    finally:
        os.close(fd)
    root = state / name
    identity = {'schema_version': 1, 'task': task, 'session': session,
                'workspace_sha256': root_digest(workspace)}
    with runtime_use(root, exclusive=True, timeout=0 if new else max(0, expires-time.monotonic())):
        fd = _private(root)
        try:
            if revoked.is_set() or time.monotonic() >= expires:
                raise PermissionError('Session authority ended while acquiring its lease')
            record = root / 'identity.json'
            if record.exists() or record.is_symlink():
                if new:
                    raise FileExistsError('New session identity was created concurrently')
                record_fd = os.open('identity.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    import stat
                    info = os.fstat(record_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o7077:
                        raise ValueError('Session identity must be a private regular file')
                    raw = os.read(record_fd, 16385)
                    if len(raw) > 16384 or json.loads(raw) != identity:
                        raise PermissionError('Stored session identity does not match this task and workspace')
                finally:
                    os.close(record_fd)
            else:
                if not create:
                    raise FileNotFoundError('Existing session identity is missing')
                _write_json(record, identity)
            if create:
                try:
                    os.mkdir('journal', mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                os.fsync(fd)
        finally:
            os.close(fd)
        owner = SessionOwner(root, Journal(root/'journal', task=task, session=session), identity, expires, revoked)
        try:
            owner.inspect()
            yield owner
        finally:
            owner._closed.set()
            if owner._bound_broker is not None:
                owner._bound_broker.clear_page_cache()
