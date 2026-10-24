"""Supervisor-owned task authority, never reconstructed from model/session text."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path, PurePosixPath
import threading
import time

PROTECTED = {'.git', '.agents', '.codex', '.claude', '.ssh', '.env'}


def relative(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise PermissionError('Invalid broker path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or str(path) != value or not path.parts:
        raise PermissionError('Broker paths must be canonical and relative')
    return path.parts


@dataclass(frozen=True)
class FileGrant:
    task: str
    session: str
    root: Path
    read: tuple[str, ...]
    write: tuple[str, ...]
    disclose: tuple[str, ...]
    expires: float
    revoked: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.task, str) or not self.task or not isinstance(self.session, str) or not self.session or not self.root.is_absolute() or '..' in self.root.parts or not math.isfinite(self.expires):
            raise ValueError('Grant requires explicit task/session/root and finite deadline')
        if any(part in PROTECTED or part.startswith('.env.') for part in self.root.parts):
            raise ValueError('Grant root cannot be inside protected state')
        for scopes in (self.read, self.write, self.disclose):
            if not isinstance(scopes, tuple):
                raise ValueError('Grant scopes must be immutable tuples')
            for scope in scopes:
                if scope != '.':
                    relative(scope)

    def check(self, task: str, session: str, operation: str, name: str, *, provider: bool = False) -> tuple[str, ...]:
        if task != self.task or session != self.session or self.revoked.is_set() or time.monotonic() >= self.expires:
            raise PermissionError('Task grant is mismatched, revoked or expired')
        parts = () if operation == 'list' and name == '.' else relative(name)
        if any(p in PROTECTED or p.startswith('.env.') for p in parts) or (operation == 'write' and 'AGENTS.md' in parts):
            raise PermissionError('Broker target is protected policy or private state')
        indexed = bool(parts and parts[0] == '@artifacts')
        if indexed and operation != 'read':
            raise PermissionError('Indexed references are read-only')
        def covered(scopes):
            return any((s == '.' and not indexed) or (s != '.' and
                parts[:len(PurePosixPath(s).parts)] == PurePosixPath(s).parts) for s in scopes)
        if operation not in ('read', 'write', 'list') or not covered(self.write if operation == 'write' else self.read):
            raise PermissionError('Operation is outside the task grant')
        if provider and not covered(self.disclose):
            raise PermissionError('Provider disclosure requires separate authority')
        return parts
