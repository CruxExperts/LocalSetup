"""Display-only workload names; native process and container IDs remain authoritative."""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import sys
from typing import Callable, Iterable, TypedDict

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
    command_path: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class WorkloadResolution:
    """Safe identity evidence; no paths or command arguments are retained."""
    status: str
    context: NameContext
    application: str | None = None
    launcher: str | None = None
    worker: str | None = None
    application_source: str = "unresolved"
    caller_application: str | None = None
    command: str | None = None
    ancestry: tuple[str, ...] = ()
    command_path: tuple[str, ...] = ()
    compact_path: str | None = None
    version_source: str = "omitted"
    handoff: str = "unresolved"
    full_name: str | None = None
    compact_name: str | None = None
    abbreviations: tuple[tuple[str, str], ...] = ()
    omitted_components: tuple[str, ...] = ()
    compact_detail: str | None = None
    pid: int | None = None


class _ResolutionMetadata(TypedDict):
    full_name: str
    compact_name: str | None
    abbreviations: tuple[tuple[str, str], ...]
    omitted_components: tuple[str, ...]
    compact_detail: str | None


def resolve_workload(context: NameContext, *, pid: int | None = None,
                     command_path: Iterable[str] = (), owned_runtime: bool = False,
                     options: NameFormat = NameFormat()) -> WorkloadResolution:
    """Resolve explicit logical dispatch separately from Linux process ancestry.

    `owned_runtime` is valid only for the current LocalSetup runtime. No target
    executable is launched, and command arguments are never copied into results.
    """
    path = tuple(_component(part, "command path") for part in (command_path or context.command_path))
    context = replace(context, command_path=path)
    _ = format_name(context, options)
    compact = "/".join(part if len(part) <= 8 else part[:7] + "~" for part in path) or None
    verified_version = context.version if context.version_verified else None
    source = "caller_asserted" if verified_version else "omitted"
    if owned_runtime and (pid is None or pid == os.getpid()):
        verified_version, source = None, "omitted"
        try:
            actual = framework_version()
            if context.version is None or context.version == actual:
                verified_version, source = actual, "localsetup_package"
        except (RuntimeError, OSError, ValueError):
            pass
    resolved = replace(context, version=verified_version, version_verified=verified_version is not None)
    full = format_name(resolved, options)
    try:
        native, abbreviations, omitted = _bounded_details(resolved, 15, options)
        detail = None
    except ValueError as exc:
        native, abbreviations, omitted, detail = None, (), (), str(exc)
    metadata = _ResolutionMetadata(full_name=full, compact_name=native, abbreviations=abbreviations,
                                   omitted_components=omitted, compact_detail=detail)
    if pid is None:
        return WorkloadResolution("resolved", resolved, caller_application=context.who, pid=pid,
                                  command_path=path, compact_path=compact, version_source=source, **metadata)
    if type(pid) is not int or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if sys.platform != "linux":
        return WorkloadResolution("unsupported", resolved, caller_application=context.who, pid=pid,
                                  command_path=path, compact_path=compact, version_source=source, **metadata)
    names: list[str] = []
    current = pid
    seen: set[int] = set()
    try:
        while current > 0 and current not in seen and len(names) < 64:
            seen.add(current)
            proc = Path(f"/proc/{current}")
            executable = os.readlink(proc / "exe")
            name = Path(executable).name
            _ = _component(name, "executable")
            stat = (proc / "stat").read_text(encoding="ascii")
            parent = int(stat[stat.rfind(")") + 2:].split()[1])
            names.append(name)
            current = parent
    except (OSError, UnicodeError, ValueError, IndexError, TypeError):
        if not names:
            return WorkloadResolution("unresolvable", resolved, caller_application=context.who, pid=pid,
                                      command_path=path, compact_path=compact, version_source=source, **metadata)
    worker = names[0]
    command = None
    try:
        with Path(f"/proc/{pid}/cmdline").open("rb") as stream:
            argv0 = stream.read(4096).split(b"\0", 1)[0].decode("utf-8")
        if Path(argv0).name == worker:
            command = worker
    except (OSError, UnicodeError, ValueError):
        pass
    launcher = names[1] if len(names) > 1 else None
    # An ancestor is not proof of an exec: the earlier image no longer exists.
    handoff = "different_ancestor_exec_unresolved" if launcher and launcher != worker else "unresolved"
    application = worker if command == worker and worker == context.who else None
    return WorkloadResolution("resolved", resolved, application=application, launcher=launcher,
                              worker=worker, caller_application=context.who, pid=pid,
                              application_source="executable" if application else "unresolved",
                              command=command, ancestry=tuple(reversed(names)), command_path=path,
                              compact_path=compact, version_source=source, handoff=handoff, **metadata)


def verify_native_name(full: str, expected: str, reader: Callable[[], str]) -> tuple[str, str | None, str | None]:
    """Compare a live native listing to the expected display name."""
    try:
        observed = reader()
    except (OSError, UnicodeError) as exc:
        return "unavailable", None, type(exc).__name__
    if not observed:
        return "unavailable", None, None
    status = "verification_failed" if observed != expected else ("truncated" if expected != full else "verified")
    return status, observed, None


def _component(value: str, field: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise ValueError(f"{field} must be a short, non-secret ASCII name component")
    return value


def _logical_path(context: NameContext) -> list[str]:
    application = _component(context.who, "who")
    path = [_component(part, "command path") for part in context.command_path]
    if not path or path[0] != application:
        path.insert(0, application)
    return path


def format_name(context: NameContext, options: NameFormat = NameFormat()) -> str:
    """Format supplied metadata without inspecting command arguments or paths."""
    parts = _logical_path(context)
    if context.scope and options.include_scope:
        parts.append(_component(context.scope, "scope"))
    if context.version and context.version_verified and options.include_version:
        parts.append("v" + _component(context.version, "version"))
    if type(context.index) is not int or context.index < 0:
        raise ValueError("index must be a non-negative integer")
    if context.index:
        parts.append(str(context.index))
    return "-".join(parts)


def _bounded_details(context: NameContext, limit: int, options: NameFormat) -> tuple[str, tuple[tuple[str, str], ...], tuple[str, ...]]:
    full = format_name(context, options)
    if limit < 1:
        raise ValueError("name limit must be positive")
    if len(full) <= limit:
        return full, (), ()
    path = _logical_path(context)
    parts = list(path)
    if context.scope and options.include_scope:
        parts.append(context.scope)
    if context.version and context.version_verified and options.include_version:
        parts.append("v" + context.version)
    suffix = f"-{context.index}" if context.index else ""
    # A dot marks a shortened component; a standalone dot marks omitted layers.
    shortened = list(parts)
    omitted: set[int] = set()
    def render() -> str:
        result: list[str] = []
        in_gap = False
        for i, part in enumerate(shortened):
            if i in omitted:
                if not in_gap:
                    result.append(".")
                in_gap = True
            else:
                result.append(part)
                in_gap = False
        return "-".join(result) + suffix
    while len(render()) > limit:
        candidates = [i for i, part in enumerate(shortened) if i not in omitted and len(part) > 2]
        if not candidates:
            break
        i = max(candidates, key=lambda n: len(shortened[n]))
        part = shortened[i]
        shortened[i] = part[:-2] + "." if part.endswith(".") else part[:-1] + "."
    # Preserve the first and last logical path components whenever possible.
    protected = {0, len(path) - 1}
    while len(render()) > limit:
        candidates = [i for i in range(len(parts)) if i not in protected and i not in omitted]
        if not candidates:
            raise ValueError("native name limit cannot represent the logical path and instance suffix")
        middle = (len(parts) - 1) / 2
        omitted.add(min(candidates, key=lambda i: abs(i - middle)))
    abbreviations = tuple((part, shortened[i]) for i, part in enumerate(parts)
                          if i not in omitted and part != shortened[i])
    return render(), abbreviations, tuple(part for i, part in enumerate(parts) if i in omitted)


def bounded_name(context: NameContext, limit: int, options: NameFormat = NameFormat()) -> str:
    """Return a marked compact name, or reject an unrepresentable surface."""
    return _bounded_details(context, limit, options)[0]


def next_available_index(context: NameContext, occupied: Iterable[str], options: NameFormat = NameFormat()) -> int:
    """Choose the first free name from an owner's complete current inventory."""
    names = set(occupied)
    index = 0
    while True:
        candidate = replace(context, index=index)
        if format_name(candidate, options) not in names:
            return index
        index += 1


def apply_process_name(context: NameContext, options: NameFormat = NameFormat(), *,
                       owned_runtime: bool = False) -> NameResult:
    """Apply and read back this thread's Linux comm, never another process's name."""
    pid = os.getpid()
    context = resolve_workload(context, pid=pid, owned_runtime=owned_runtime, options=options).context
    full = format_name(context, options)
    if sys.platform != "linux":
        return NameResult("unsupported", full, pid=pid, detail="native process naming is only implemented on Linux")
    try:
        native = bounded_name(context, 15, options)
    except ValueError as exc:
        return NameResult("unsupported", full, pid=pid, detail=str(exc))
    try:
        comm = Path("/proc/self/comm")
        comm.write_text(native, encoding="ascii")
        status, observed, detail = verify_native_name(full, native, lambda: comm.read_text(encoding="ascii").rstrip("\n"))
    except (OSError, UnicodeError) as exc:
        return NameResult("unavailable", full, pid=pid, detail=type(exc).__name__)
    return NameResult(status, full, observed, pid, detail)


def verify_process_name(context: NameContext, pid: int, options: NameFormat = NameFormat()) -> NameResult:
    """Read a process's display name without changing it or asserting ownership."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("pid must be a positive integer")
    context = resolve_workload(context, pid=pid, options=options).context
    full = format_name(context, options)
    if sys.platform != "linux":
        return NameResult("unsupported", full, pid=pid, detail="native process naming is only implemented on Linux")
    try:
        native = bounded_name(context, 15, options)
    except ValueError as exc:
        return NameResult("unsupported", full, pid=pid, detail=str(exc))
    status, observed, detail = verify_native_name(
        full, native, lambda: Path(f"/proc/{pid}/comm").read_text(encoding="ascii").rstrip("\n"))
    return NameResult(status, full, observed, pid, detail)


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


def set_process_name(scope: str | None = None, *, command_path: tuple[str, ...] = ()) -> bool:
    """Name an owned LocalSetup process using its verified framework version."""
    try:
        context = resolve_workload(NameContext("LS", scope=scope, command_path=command_path), owned_runtime=True).context
        if sys.platform == "linux":
            context = _owned_process_context(context)
        result = apply_process_name(context, owned_runtime=True)
    except (RuntimeError, OSError, UnicodeError, ValueError):
        return False
    return result.status in ("verified", "truncated")
