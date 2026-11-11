"""Display-only workload names; native process and container IDs remain authoritative."""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import sys
from typing import Iterable

from .framework_version import framework_version


_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)


@dataclass(frozen=True)
class NameContext:
    """Explicit, non-secret application context supplied by its owner.

    A version is included only when its source has been verified by the caller.
    Index allocation belongs to the launcher or engine, not this module.
    """
    who: str
    scope: str | None = None
    version: str | None = None
    version_verified: bool = False
    index: int = 0


@dataclass(frozen=True)
class NameFormat:
    include_scope: bool = True
    include_version: bool = True


@dataclass(frozen=True)
class NameResult:
    status: str
    full_name: str
    observed_name: str | None = None
    pid: int | None = None
    detail: str | None = None


def _component(value: str, field: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise ValueError(f"{field} must be a short, non-secret ASCII name component")
    return value


def format_name(context: NameContext, options: NameFormat = NameFormat()) -> str:
    """Format supplied metadata without inspecting command arguments or paths."""
    parts = [_component(context.who, "who")]
    if context.scope and options.include_scope:
        parts.append(_component(context.scope, "scope"))
    if context.version and context.version_verified and options.include_version:
        parts.append("v" + _component(context.version, "version"))
    if type(context.index) is not int or context.index < 0:
        raise ValueError("index must be a non-negative integer")
    if context.index:
        parts.append(str(context.index))
    return "-".join(parts)


def bounded_name(context: NameContext, limit: int, options: NameFormat = NameFormat()) -> str:
    """Keep the application and instance suffix visible on a short native surface."""
    full = format_name(context, options)
    if limit < 1:
        raise ValueError("name limit must be positive")
    if len(full) <= limit:
        return full
    suffix = f"-{context.index}" if context.index else ""
    if len(suffix) >= limit:
        raise ValueError("instance suffix does not fit native name limit")
    return full[:limit - len(suffix)].rstrip("-.") + suffix


def next_available_index(context: NameContext, occupied: Iterable[str], options: NameFormat = NameFormat()) -> int:
    """Choose the first free name from an owner's complete current inventory."""
    names = set(occupied)
    index = 0
    while True:
        candidate = NameContext(context.who, context.scope, context.version, context.version_verified, index)
        if format_name(candidate, options) not in names:
            return index
        index += 1


def apply_process_name(context: NameContext, options: NameFormat = NameFormat()) -> NameResult:
    """Apply and read back this thread's Linux comm, never another process's name."""
    full = format_name(context, options)
    pid = os.getpid()
    if sys.platform != "linux":
        return NameResult("unsupported", full, pid=pid, detail="native process naming is only implemented on Linux")
    native = bounded_name(context, 15, options)
    try:
        comm = Path("/proc/self/comm")
        comm.write_text(native, encoding="ascii")
        observed = comm.read_text(encoding="ascii").rstrip("\n")
    except (OSError, UnicodeError) as exc:
        return NameResult("unavailable", full, pid=pid, detail=type(exc).__name__)
    if observed != native:
        return NameResult("verification_failed", full, observed, pid)
    return NameResult("truncated" if native != full else "verified", full, observed, pid)


def verify_process_name(context: NameContext, pid: int, options: NameFormat = NameFormat()) -> NameResult:
    """Read a process's display name without changing it or asserting ownership."""
    full = format_name(context, options)
    if type(pid) is not int or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if sys.platform != "linux":
        return NameResult("unsupported", full, pid=pid, detail="native process naming is only implemented on Linux")
    native = bounded_name(context, 15, options)
    try:
        observed = Path(f"/proc/{pid}/comm").read_text(encoding="ascii").rstrip("\n")
    except (OSError, UnicodeError) as exc:
        return NameResult("unavailable", full, pid=pid, detail=type(exc).__name__)
    status = "verification_failed" if observed != native else ("truncated" if native != full else "verified")
    return NameResult(status, full, observed, pid)


def _owned_process_context(context: NameContext) -> NameContext:
    """Use a best-effort same-user inventory; never rename another process."""
    occupied: set[str] = set()
    own_name = None
    pid = os.getpid()
    uid = os.getuid()
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                name = Path(entry.path, "comm").read_text(encoding="ascii").rstrip("\n")
            except (FileNotFoundError, UnicodeError):
                continue
            if int(entry.name) == pid:
                own_name = name
            else:
                occupied.add(name)
    if own_name == bounded_name(context, 15):
        return context
    if own_name:
        suffix = re.search(r"-(\d+)$", own_name)
        if suffix:
            candidate = replace(context, index=int(suffix.group(1)))
            if bounded_name(candidate, 15) == own_name:
                return candidate
    index = 0
    while bounded_name(replace(context, index=index), 15) in occupied:
        index += 1
    return replace(context, index=index)


def set_process_name(scope: str | None = None) -> bool:
    """Name an owned LocalSetup process using its verified framework version."""
    try:
        context = NameContext("LS", scope=scope, version=framework_version(), version_verified=True)
        if sys.platform == "linux":
            context = _owned_process_context(context)
        result = apply_process_name(context)
    except (RuntimeError, OSError, UnicodeError, ValueError):
        return False
    return result.status in ("verified", "truncated")
