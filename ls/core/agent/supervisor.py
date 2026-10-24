"""Bounded POSIX worker lifetime; terminal outcomes belong to the supervisor."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import selectors
import socket
import subprocess
import time

from .runtime_install import selected
from .worker_protocol import MAX_REQUEST, MAX_OUTPUT, MAX_DIAGNOSTICS, result
from .worker_lifetime import WorkerLifetime, qualify_wait_owner


@dataclass(frozen=True)
class Outcome:
    status: str
    returncode: int | None
    data: dict | None = None


class _BrokerCancellation:
    def __init__(self, original, current):
        self.original,self.current=original,current
        self.worker=None

    def is_set(self):
        return (self.original.is_set() or (self.current is not None and self.current.is_set())
                or (self.worker is not None and self.worker.poll() is not None))


def supervise(command: list[str], request: bytes, *, cwd: Path, environment: dict[str, str], timeout: float, cancel=None, capture: bool = False, pass_fds: tuple[int, ...] = (), broker=None) -> Outcome:
    if os.name != 'posix':
        raise RuntimeError('Worker supervision requires qualified POSIX process groups')
    if not math.isfinite(timeout) or timeout <= 0 or len(request) > MAX_REQUEST:
        raise ValueError('Invalid worker deadline or request size')
    if cancel is not None and cancel.is_set():
        return Outcome('cancelled', None)
    if (not isinstance(pass_fds, tuple) or any(type(fd) is not int or fd < 3 for fd in pass_fds)
            or len(set(pass_fds)) != len(pass_fds) or len(pass_fds) > 8):
        raise ValueError('Invalid explicit worker descriptors')
    deadline = time.monotonic() + timeout
    if broker is not None:
        from .broker_rpc import Channel
        if (not isinstance(broker,tuple) or len(broker)!=3 or not isinstance(broker[0],Channel)
                or not callable(broker[1]) or not callable(broker[2])):
            raise ValueError('Broker supervision requires a channel, handler and authority check')
        broker[0]._check()
        broker[0].expires=min(broker[0].expires,deadline)
        broker[0].cancelled=_BrokerCancellation(broker[0].cancelled,cancel)
    qualify_wait_owner()
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, umask=0o077, pass_fds=pass_fds)
    lifetime = WorkerLifetime(process, deadline, cancel)
    if broker is not None:
        broker[0].cancelled.worker=lifetime
    output, diagnostics = bytearray(), bytearray()
    status, offset = None, 0
    try:
        lifetime.start()
        with selectors.DefaultSelector() as selector:
            if broker is not None:
                selector.register(broker[0].connection,selectors.EVENT_READ,'broker')
            for stream, kind, events in ((process.stdin, 'input', selectors.EVENT_WRITE),
                    (process.stdout, 'output', selectors.EVENT_READ), (process.stderr, 'diagnostics', selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events, kind)
            while selector.get_map():
                lifetime.check()
                if lifetime.status is not None:
                    status = lifetime.status
                    break
                if cancel is not None and cancel.is_set():
                    status = 'cancelled'
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = 'timed_out'
                    break
                if lifetime.poll() is not None:
                    # Descendants cannot retain pipes after the owning worker exits.
                    lifetime.kill()
                    if broker is not None:
                        try:selector.unregister(broker[0].connection)
                        except KeyError:pass
                for key, _ in selector.select(min(remaining, 0.05)):
                    stream = key.fileobj
                    if key.data == 'broker':
                        channel,handler,check=broker
                        if channel.connection.recv(1, socket.MSG_PEEK)==b'':
                            selector.unregister(stream)
                        else:
                            def active_broker():
                                if lifetime.poll() is not None:
                                    raise ConnectionError('Worker exited; new broker dispatch is refused')
                                check()
                            channel.serve_once(handler,check=active_broker)
                        continue
                    if key.data == 'input':
                        try:
                            offset += os.write(stream.fileno(), request[offset:])
                        except BrokenPipeError:
                            offset = len(request)
                        if offset == len(request):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    try:
                        chunk = os.read(stream.fileno(), 16384)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    target, limit = (output, MAX_OUTPUT) if key.data == 'output' else (diagnostics, MAX_DIAGNOSTICS)
                    if len(target) + len(chunk) > limit:
                        status = 'output_limit'
                        break
                    target.extend(chunk)
                if status is not None:
                    break
            if status is None:
                while lifetime.poll() is None:
                    lifetime.check()
                    if cancel is not None and cancel.is_set():
                        status = 'cancelled'
                        break
                    if time.monotonic() >= deadline:
                        status = 'timed_out'
                        break
                    time.sleep(min(0.05, max(0.001, deadline-time.monotonic())))
    except (ConnectionError, TimeoutError):
        if cancel is not None and cancel.is_set():
            status = 'cancelled'
        elif time.monotonic() >= deadline:
            status = 'timed_out'
        else:
            raise
    except KeyboardInterrupt:
        status = 'cancelled'
    finally:
        try:
            lifetime.close()
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
    status = status or lifetime.status
    if status is not None:
        return Outcome(status, process.returncode)
    if capture:
        data = {'stdout': bytes(output).decode('utf-8', errors='replace'),
                'stderr': bytes(diagnostics).decode('utf-8', errors='replace')}
        return Outcome('completed' if process.returncode == 0 else 'failed', process.returncode, data)
    if process.returncode != 0:
        return Outcome('failed', process.returncode)
    try:
        data = result(bytes(output))
    except (ValueError, TypeError, RecursionError):
        return Outcome('protocol_error', process.returncode)
    return Outcome('completed', process.returncode, data)


def probe_runtime(root: Path, *, timeout: float = 30, cancel=None) -> Outcome:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Worker timeout must be finite and positive')
    deadline = time.monotonic() + timeout
    if cancel is not None and cancel.is_set():
        return Outcome('cancelled', None)
    with selected(root, timeout=timeout) as release:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Outcome('timed_out', None)
        return supervise([str(release/'venv/bin/python'), '-I', '-B', '-m', 'ls.core.agent.sdk_worker', '--serve'],
            json.dumps({'schema_version': 1, 'operation': 'probe'}).encode(), cwd=release,
            environment={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, timeout=remaining, cancel=cancel)
