"""Owned SSH stream using shared strict SSH policy and worker collection.

The trusted controller selects and authorizes the exact remote helper argv.
This library grants neither command authority nor automatic transport fallback.
"""
import math
import os
import selectors
import subprocess
import threading
import time

from .control_transport import Stream, TransportError
from .worker_lifetime import WorkerLifetime, qualify_wait_owner
from ..herdr_ops.remote import ssh_command


class _Stopped:
    def __init__(self, original, local):
        self.original, self.local = original, local

    def is_set(self):
        return self.local.is_set() or (self.original is not None and self.original.is_set())


class SSH:
    """One admitted helper process, one bounded encrypted frame stream.

    stderr is drained and counted, never retained or disclosed. Its 64 KiB cap,
    cancellation and the absolute deadline terminate the owned process group.
    close() must collect it; remote-operation completion still requires its
    authenticated receipt, never an SSH exit status.
    """
    def __init__(self, target, remote_argv, *, expires, cancelled=None):
        if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.monotonic():
            raise ValueError('SSH control requires a future deadline')
        self.stop = threading.Event()
        self.cancelled = _Stopped(cancelled, self.stop)
        self.stream = self.process = self.lifetime = self.reader = None
        self.closed = False
        self.diagnostic_bytes = 0
        if self.cancelled.is_set():
            raise TransportError(dispatched=False)
        command = ssh_command(target, remote_argv, timeout=min(30, expires-time.monotonic()))
        if self.cancelled.is_set() or time.monotonic() >= expires:
            raise TransportError(dispatched=False)
        qualify_wait_owner()
        environment = {name: os.environ[name] for name in
            ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'XDG_RUNTIME_DIR')
            if name in os.environ}
        environment['PATH'] = os.defpath
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, close_fds=True, umask=0o077, env=environment)
        self.lifetime = WorkerLifetime(self.process, expires, self.cancelled)
        read_fd = write_fd = None
        try:
            self.lifetime.start()
            read_fd = os.dup(self.process.stdout.fileno())
            write_fd = os.dup(self.process.stdin.fileno())
            self.stream = Stream(read_fd, write_fd, expires=expires, cancelled=self.cancelled)
            read_fd = write_fd = None  # Stream now owns both descriptors.
            self.reader = threading.Thread(target=self._diagnostics, name='agent-control-ssh-stderr')
            self.reader.start()
        except BaseException:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)
            self.close()
            raise

    def _diagnostics(self):
        try:
            fd = self.process.stderr.fileno()
            os.set_blocking(fd, False)
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_READ)
                while not self.cancelled.is_set():
                    if not selector.select(.025):
                        continue
                    try:
                        raw = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    if not raw:
                        return
                    self.diagnostic_bytes += len(raw)
                    if self.diagnostic_bytes > 65536:
                        self.stop.set()
        except BaseException:
            self.stop.set()

    def exchange(self, kind, request_id, ciphertext):
        if self.closed:
            raise TransportError(dispatched=False)
        try:
            self.lifetime.check()
            return self.stream.exchange(kind, request_id, ciphertext)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.stop.set()
        try:
            try:
                if self.stream is not None:
                    self.stream.close()
            finally:
                if self.lifetime is not None:
                    self.lifetime.close()
        finally:
            if self.reader is not None and self.reader.ident is not None:
                self.reader.join()
            if self.process is not None:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    stream.close()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
