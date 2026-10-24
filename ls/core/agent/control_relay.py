"""Fixed ciphertext-only stdio relay to an explicitly selected private socket."""
import math
import os
import signal
import sys
import threading
import time

from .control_socket import connect
from .control_transport import Endpoint, Stream, pack, unpack


class Forwarder(Endpoint):
    """Preserve exact encrypted frames; only the upstream owner authenticates."""
    def __init__(self, upstream):
        self.upstream = upstream

    def respond(self, raw):
        kind, identifier, reply, ciphertext = unpack(raw)
        if reply:
            raise ValueError('Relay requires a request')
        answer = self.upstream.exchange(kind, identifier, ciphertext)
        return pack(kind, identifier, answer, reply=True)


def relay(selected, *, expires, cancelled, input_fd=0, output_fd=1):
    upstream = connect(selected, expires=expires, cancelled=cancelled)
    downstream = None
    read_fd = write_fd = None
    try:
        read_fd = os.dup(input_fd)
        write_fd = os.dup(output_fd)
        downstream = Stream(read_fd, write_fd, expires=expires, cancelled=cancelled)
        read_fd = write_fd = None
        endpoint = Forwarder(upstream)
        while not cancelled.is_set():
            downstream.serve_once(endpoint)
    finally:
        try:
            if downstream is not None:
                downstream.close()
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)
        finally:
            upstream.close()


def main(args):
    if type(args.timeout) not in (int, float) or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        print('Control relay timeout must be within 0..3600 seconds.', file=sys.stderr)
        return 2
    cancelled = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: cancelled.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        relay(args.socket, expires=time.monotonic()+args.timeout, cancelled=cancelled)
        return 0
    except (OSError, ValueError, RuntimeError):
        print('Control relay ended; reconcile any unacknowledged request without replay.', file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
