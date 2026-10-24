"""Run-owned encrypted control reader and private attachment; no fallback."""
from contextlib import contextmanager
import math
import os
import threading
import time
import uuid

from .control_envelope import ControlBinding
from .control_requests import Requests
from .control_transport import Endpoint, Stream
from .operation_journal import Journal
from .control_ready import challenge, seal_ready
from .run_control import validate
from .runtime_install import _write_json
from .session_owner import _private
from ..openpgp.container_identity import machine_binding


@contextmanager
def listen(fd, admission, owner, run_id, *, cancelled, ready, nonce,
           steering=None, approvals=None, events=None, socket_path=None):
    """Read a connected local relay FD only after live owner qualification.

    Shutdown revokes new control dispatch before collecting the reader. It waits
    for any synchronous bounded crypto/handler call rather than releasing the
    owner with a live background thread. Transport failure cancels the run;
    neither the request nor the transport is automatically retried.
    """
    challenge(nonce)
    if (fd is None) == (socket_path is None):
        raise ValueError('Select exactly one encrypted control attachment')
    if fd is not None:
        validate(fd)
    owner._check()
    admission.peer.check()
    now = int(time.time())
    remaining = owner.expires-time.monotonic()
    binding = ControlBinding(admission.agent, machine_binding().digest,
        admission.peer.scope, owner._journal.task, owner._journal.session,
        run_id, uuid.uuid4().hex, owner.control_generation, now,
        now+min(86400, max(1, math.ceil(remaining))))
    authority = owner.control_authority(binding, run_id=run_id, methods=admission.methods)
    root = owner.root/'controls'
    root.mkdir(mode=0o700, exist_ok=True)
    os.close(_private(root))
    selected = root/run_id
    selected.mkdir(mode=0o700)
    journal_root = selected/'journal'
    journal_root.mkdir(mode=0o700)
    _write_json(selected/'binding.json', binding.value())
    journal = Journal(journal_root, task=binding.task, session=binding.session)
    stop = threading.Event()
    requests = Requests(cancelled, owner.expires, steering, approvals, events)
    def authorize(selected_binding, method):
        if stop.is_set():
            raise PermissionError('Encrypted control scope has ended')
        return authority(selected_binding, method)
    endpoint = Endpoint(admission.peer, binding, requests, journal, authorize)
    # The connection is exclusive to this reader after entry. Do not duplicate
    # it into a model worker or leave it inheritable across process launches.
    listener = stream = None
    if socket_path is not None:
        from .control_socket import Listener
        listener = Listener(socket_path)
    else:
        os.set_inheritable(fd, False)
        owned_fd = os.dup(fd)
        try:
            stream = Stream(owned_fd, owned_fd, expires=owner.expires, cancelled=stop)
        except BaseException:
            os.close(owned_fd)
            raise
    failed = threading.Event()
    def serve():
        nonlocal stream
        try:
            if listener is not None:
                stream = listener.accept(expires=owner.expires, cancelled=stop)
            while not stop.is_set():
                stream.serve_once(endpoint)
        except BaseException:
            if not stop.is_set():
                failed.set()
                cancelled.set()
        finally:
            requests.closed = True
            if stream is not None:
                stream.close()
    thread = threading.Thread(target=serve, name='agent-encrypted-control')
    started = False
    try:
        announcement = seal_ready(admission.peer, binding, admission.methods, nonce=nonce)
        owner._check()
        thread.start()
        started = True
        ready(announcement)
        yield binding
    finally:
        stop.set()
        requests.closed = True
        if started:
            thread.join()
        try:
            if stream is not None:
                stream.close()
        finally:
            if listener is not None:
                listener.close()
    if failed.is_set():
        raise ConnectionError('Encrypted control ended; reconcile without replay')
