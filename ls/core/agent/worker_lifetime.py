"""Own a worker's unreaped identity until its process group has been stopped."""
from __future__ import annotations

import os
import signal
import threading
import time


def qualify_wait_owner() -> None:
    # Ignored SIGCHLD can auto-reap children; a handler can reap them elsewhere.
    # Either would release the process-group leader's ID before our last signal.
    if (not all(hasattr(os, name) for name in ('waitid', 'P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT'))
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
        raise RuntimeError('Worker supervision requires exclusive non-reaping child observation')


class WorkerLifetime:
    """Cancellation flags must be thread-safe, nonblocking reads, not callbacks.

    Only this owner may reap the child. Broker/authority callbacks stay on their
    calling thread. The monitor only observes lifetime and signals the owned
    group; it cannot interrupt a trusted callback or acknowledge its effects.
    """

    def __init__(self, process, deadline: float, cancel):
        self.process, self.deadline, self.cancel = process, deadline, cancel
        self.status = None
        self.error = None
        self._owned = True
        self._closed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name='ls-worker-lifetime')

    def start(self) -> None:
        self._thread.start()

    def _poll(self):
        if not self._owned:
            raise RuntimeError('Worker wait ownership was lost')
        try:
            observed = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            self._owned = False
            raise RuntimeError('Worker wait ownership was lost') from None
        if observed is None:
            return None
        return observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status

    def poll(self):
        with self._lock:
            if self._closed:
                return self.process.returncode
            return self._poll()

    def kill(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._poll()  # A live child or unreaped zombie still reserves its ID.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _watch(self) -> None:
        try:
            while not self._stop.is_set():
                if self.cancel is not None and self.cancel.is_set():
                    self.status = 'cancelled'
                elif time.monotonic() >= self.deadline:
                    self.status = 'timed_out'
                if self.status is not None or self.poll() is not None:
                    self.kill()
                    return
                self._stop.wait(.025)
        except Exception as error:
            self.error = error
            try:
                self.kill()
            except Exception:
                pass  # Preserve the first error; never signal after lost ownership.

    def check(self) -> None:
        if self.error is not None:
            raise RuntimeError('Worker lifetime monitoring failed') from self.error

    def close(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()
        # No monitor can signal after wait() releases the child's identity.
        if self._owned:
            self.kill()
            self.process.wait()
            self._closed = True
        self.check()
