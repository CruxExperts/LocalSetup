"""Bounded, one-use passphrase delivery over an inherited anonymous pipe."""

from __future__ import annotations

import os

__all__ = ["PassphrasePipe"]

_MAX_PASSPHRASE_BYTES = 4096
_NEWLINE = b"\n"


class PassphrasePipe:
    """Own a mutable passphrase buffer and expose a nonblocking pipe writer.

    The caller passes ``read_fd`` to one child with ``pass_fds`` and registers
    ``write_fd`` in its existing selector loop. The child is spawned before
    ``write_some`` is called, so a full pipe can never deadlock before its
    reader exists.
    """

    def __init__(self, secret: bytearray | bytes) -> None:
        if isinstance(secret, bytearray):
            self.secret = secret
        elif isinstance(secret, bytes):
            self.secret = bytearray(secret)
        else:
            raise ValueError("invalid passphrase buffer")
        if (not self.secret or len(self.secret) > _MAX_PASSPHRASE_BYTES
                or any(value in self.secret for value in (0, 10, 13))):
            self._wipe()
            raise ValueError("invalid passphrase buffer")
        self._secret_offset = 0
        self._newline_offset = 0
        self.read_fd = -1
        self.write_fd = -1
        try:
            self.read_fd, self.write_fd = os.pipe()
            os.set_inheritable(self.read_fd, True)
            os.set_blocking(self.write_fd, False)
        except BaseException:
            self.close()
            raise

    @property
    def pending(self) -> bool:
        return (self._secret_offset < len(self.secret)
                or self._newline_offset < len(_NEWLINE))

    def write_some(self) -> bool:
        """Write one available chunk; return true after secret plus newline."""
        if self.write_fd < 0:
            return not self.pending
        if not self.pending:
            return True
        if self._secret_offset < len(self.secret):
            view = memoryview(self.secret)
            try:
                chunk = view[self._secret_offset:]
                try:
                    try:
                        written = os.write(self.write_fd, chunk)
                    except (BlockingIOError, InterruptedError):
                        return False
                finally:
                    chunk.release()
            finally:
                view.release()
            self._secret_offset += written
        else:
            view = memoryview(_NEWLINE)
            try:
                chunk = view[self._newline_offset:]
                try:
                    try:
                        written = os.write(self.write_fd, chunk)
                    except (BlockingIOError, InterruptedError):
                        return False
                finally:
                    chunk.release()
            finally:
                view.release()
            self._newline_offset += written
        if written <= 0:
            raise OSError("short passphrase pipe write")
        return not self.pending

    def close_reader(self) -> None:
        if self.read_fd >= 0:
            os.close(self.read_fd)
            self.read_fd = -1

    def close_writer(self) -> None:
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1

    def _wipe(self) -> None:
        for index in range(len(self.secret)):
            self.secret[index] = 0

    def close(self) -> None:
        try:
            self.close_writer()
        finally:
            try:
                self.close_reader()
            finally:
                self._wipe()

    def __enter__(self) -> PassphrasePipe:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
