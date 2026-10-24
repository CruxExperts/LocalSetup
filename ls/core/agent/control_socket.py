"""Private run-owned Unix attachment; socket identity grants no control authority."""
import os
from pathlib import Path
import socket
import stat
import struct
import time

from .control_transport import Stream
from .session_owner import _private


def path(value):
    if (not isinstance(value, Path) or not value.is_absolute() or '..' in value.parts
            or '\x00' in str(value) or len(os.fsencode(value)) > 103):
        raise ValueError('Control socket requires a bounded absolute path')
    return value


def _peer(channel):
    if hasattr(socket, 'SO_PEERCRED'):
        _, uid, _ = struct.unpack('3i', channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    elif hasattr(channel, 'getpeereid'):
        uid, _ = channel.getpeereid()
    else:
        raise RuntimeError('Control attachment requires qualified local peer credentials')
    if uid != os.getuid():
        raise PermissionError('Control attachment belongs to another user')


def connect(selected, *, expires, cancelled=None):
    selected = path(selected)
    directory = _private(selected.parent)
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        info = os.stat(selected.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError('Control attachment must be a private owned socket')
        if time.monotonic() >= expires or (cancelled is not None and cancelled.is_set()):
            raise TimeoutError('Control attachment deadline ended')
        channel.settimeout(min(1, expires-time.monotonic()))
        channel.connect(str(selected))
        after = os.stat(selected.name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
            raise PermissionError('Control attachment changed during connection')
        _peer(channel)
        fd = channel.detach()
        try:
            return Stream(fd, fd, expires=expires, cancelled=cancelled)
        except BaseException:
            os.close(fd)
            raise
    finally:
        channel.close()
        os.close(directory)


class Listener:
    """Bind only a fresh path and remove only the inode this instance created."""
    def __init__(self, selected):
        self.path = path(selected)
        self.directory = _private(self.path.parent)
        self.channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.identity = None
        self.closed = False
        try:
            self.channel.bind(str(self.path))  # Existing paths are never removed.
            info = os.stat(self.path.name, dir_fd=self.directory, follow_symlinks=False)
            self.identity = (info.st_dev, info.st_ino)
            os.chmod(self.path.name, 0o600, dir_fd=self.directory, follow_symlinks=False)
            self.channel.listen(1)
            self.channel.settimeout(.05)
        except BaseException:
            self.close()
            raise

    def accept(self, *, expires, cancelled):
        while not cancelled.is_set() and time.monotonic() < expires:
            try:
                connection, _ = self.channel.accept()
            except socket.timeout:
                continue
            try:
                _peer(connection)
                fd = connection.detach()
                try:
                    return Stream(fd, fd, expires=expires, cancelled=cancelled)
                except BaseException:
                    os.close(fd)
                    raise
            finally:
                connection.close()
        raise TimeoutError('Control attachment ended before connection')

    def close(self):
        if self.closed:
            return
        try:
            self.channel.close()
            if self.identity is not None:
                try:
                    info = os.stat(self.path.name, dir_fd=self.directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if (info.st_dev, info.st_ino) != self.identity:
                        raise PermissionError('Control socket replaced; cleanup refused')
                    os.unlink(self.path.name, dir_fd=self.directory)
        finally:
            os.close(self.directory)
            self.closed = True
