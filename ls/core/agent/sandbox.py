"""Task-bound Linux sandbox invocations over private, broker-prepared snapshots."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import stat
import threading
import time
from types import MappingProxyType
from typing import Mapping

from .file_grants import PROTECTED
from .native_bundle import _platform
from .runtime_install import selected
from .resource_group import Limits, resource_group


@dataclass(frozen=True)
class ProcessGrant:
    task: str
    session: str
    staging: Path
    command: tuple[str, ...]
    expires: float
    disclose_output: bool = False
    revoked: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)

    resource_parent: Path | None = None
    limits: Limits = field(default_factory=Limits)
    work_bytes: int = 512 * 1024 * 1024
    temporary_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if (not isinstance(self.task, str) or not self.task or not isinstance(self.session, str) or not self.session
                or not self.staging.is_absolute() or '..' in self.staging.parts or not math.isfinite(self.expires)
                or type(self.disclose_output) is not bool):
            raise ValueError('Process grant requires explicit identity, staging root and deadline')
        if (not isinstance(self.command, tuple) or not self.command or len(self.command) > 256
                or any(not isinstance(x, str) or '\x00' in x for x in self.command)
                or sum(len(x.encode()) for x in self.command) > 16384):
            raise ValueError('Process command must be a bounded immutable argument tuple')
        if not isinstance(self.limits, Limits) or (self.resource_parent is not None and
                (not isinstance(self.resource_parent, Path) or not self.resource_parent.is_absolute())):
            raise ValueError('Process resource delegation requires an absolute path and limits')
        for size in (self.work_bytes,self.temporary_bytes):
            if type(size) is not int or not 16*1024*1024 <= size <= 1024*1024*1024:
                raise ValueError('Sandbox writable storage requires bounded integer sizes')
        executable = Path(self.command[0])
        if executable.parent != Path('/usr/bin') or str(executable) != self.command[0]:
            raise ValueError('Process executable must be an explicit system tool under /usr/bin')

    def check(self, task: str, session: str) -> None:
        if task != self.task or session != self.session or self.revoked.is_set() or time.monotonic() >= self.expires:
            raise PermissionError('Process grant is mismatched, revoked or expired')


@dataclass(frozen=True)
class Invocation:
    command: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    pass_fds: tuple[int, ...] = ()


def coding_invocation(release: Path, channel_fd: int, *, task: str, session: str,
                      expires: float) -> Invocation:
    """Confine the installed coding worker to its runtime and inherited broker FD.

    The caller holds the selected runtime lease and qualified tool preflight for
    the entire child lifetime. No workspace, home, host socket or network mount
    crosses this boundary.
    """
    if (not isinstance(release, Path) or not release.is_absolute()
            or release.is_symlink() or not release.is_dir()
            or type(channel_fd) is not int or channel_fd < 3
            or any(not isinstance(x, str) or not x for x in (task, session))
            or not math.isfinite(expires) or expires <= time.monotonic()):
        raise ValueError('Coding sandbox requires a selected runtime, channel and deadline')
    _platform()
    binary = release / 'venv/lscli-native/bwrap'
    worker = release / 'venv/bin/python'
    if (not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK)
            or not worker.is_file() or not os.access(worker, os.X_OK)):
        raise RuntimeError('Selected runtime lacks a sealed coding sandbox or Python worker')
    command = (
        str(binary), '--unshare-all', '--unshare-user', '--disable-userns',
        '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
        '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
        '--ro-bind', str(release), '/runtime', '--proc', '/proc', '--dev', '/dev',
        '--size', str(64 * 1024 * 1024), '--tmpfs', '/tmp',
        '--size', str(512 * 1024 * 1024), '--tmpfs', '/work',
        '--remount-ro', '/', '--chdir', '/work', '--clearenv',
        '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/tmp',
        '--setenv', 'LANG', 'C.UTF-8', '--', '/runtime/venv/bin/python', '-I', '-B', '-m',
        'ls.core.agent.coding_worker', str(channel_fd), task, session, str(expires),
    )
    return Invocation(command, release,
                      MappingProxyType({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}),
                      (channel_fd,))


def _system_boundary(runtimes: Path) -> None:
    runtimes = runtimes.resolve(strict=True)
    if runtimes.is_relative_to('/usr') or Path('/usr').is_relative_to(runtimes):
        raise ValueError('Runtime must be outside the exposed system toolchain')


def _snapshot(root: Path, runtimes: Path) -> None:
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('Snapshot paths cannot contain symlinks')
    root = root.resolve(strict=True)
    runtimes = runtimes.resolve(strict=True)
    if any(root.is_relative_to(p) or p.is_relative_to(root) for p in (runtimes, Path('/usr'))):
        raise ValueError('Snapshot must be separate from runtime and system toolchain')
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Snapshot root must be a private owned directory')
    count, size = 0, 0
    def fail(error):
        raise error
    for directory, names, files in os.walk(root, followlinks=False, onerror=fail):
        for name in (*names, *files):
            count += 1
            entry = Path(directory) / name
            info = entry.lstat()
            if name in PROTECTED or name.startswith('.env.') or name == 'AGENTS.md':
                raise ValueError('Snapshot contains protected context or private state')
            if (info.st_uid != os.getuid() or info.st_mode & 0o7022
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise ValueError('Snapshot requires owned regular files and directories without shared writes')
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
            if count > 30000 or size > 256 * 1024 * 1024:
                raise ValueError('Snapshot inventory exceeds process limits')


@contextmanager
def invocation(runtimes: Path, grant: ProcessGrant, *, task: str, session: str):
    """Hold the selected runtime lease until the caller has reaped its process tree.

    Caller exclusively owns the snapshot and must enforce cancellation/deadlines
    while running, then authorize disclosure and journal any workspace writeback.
    """
    grant.check(task, session)
    _platform()
    with selected(runtimes, timeout=max(0, grant.expires - time.monotonic())) as release:
        _system_boundary(runtimes)
        binary = release / 'venv' / 'lscli-native' / 'bwrap'
        if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
            raise RuntimeError('Selected runtime has no sealed executable sandbox bundle')
        copies=list(release.glob('venv/lib/python*/site-packages/ls/core/agent/sandbox_copy.py'))
        if len(copies)!=1 or not copies[0].is_file() or copies[0].is_symlink():
            raise RuntimeError('Selected runtime has no sealed sandbox copy bootstrap')
        _snapshot(grant.staging, runtimes)
        grant.check(task, session)
        command = [str(binary), '--unshare-all', '--unshare-user', '--disable-userns', '--die-with-parent', '--new-session',
                   '--cap-drop', 'ALL', '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
                   '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
                   '--proc', '/proc', '--dev', '/dev',
                   '--size', str(grant.temporary_bytes), '--tmpfs', '/tmp',
                   '--ro-bind', str(grant.staging), '/inputs',
                   '--ro-bind', str(copies[0]), '/lscli-copy.py',
                   '--size', str(grant.work_bytes), '--tmpfs', '/work',
                   '--remount-ro', '/', '--chdir', '/work', '--clearenv',
                   '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/tmp',
                   '--setenv', 'LANG', 'C.UTF-8', '--',
                   '/usr/bin/python3', '-I', '-B', '/lscli-copy.py', *grant.command]
        with ExitStack() as stack:
            descriptors = ()
            if grant.resource_parent is not None:
                group = stack.enter_context(resource_group(grant.resource_parent, grant.limits))
                fd = stack.enter_context(group.membership())
                descriptors = (fd,)
                command = [str(release/'venv/bin/python'), '-I', '-B', '-m',
                           'ls.core.agent.resource_exec', str(fd), '--', *command]
            grant.check(task, session)
            yield Invocation(tuple(command), release,
                             MappingProxyType({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), descriptors)
