#!/usr/bin/env python3
# Purpose: Gather maximum system context for baseline snapshot without sudo.
# Created: 2026-02-24
# Last updated: 2026-02-24
#
# Uses only stdlib and commands/files typically readable by unprivileged users.
# No network. Emits GFM markdown to stdout by default, or writes a timestamped
# markdown file when --output-basename is provided; warnings/errors go to stderr.

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


_SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _warn(message: str) -> None:
    print(f"[system_snapshot] WARNING: {message}", file=sys.stderr)


def _summarize(text: str, limit: int = 240) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LANG": "C"},
        )
        if r.returncode == 0:
            return (r.stdout or "").strip()

        detail = _summarize(r.stderr or r.stdout or "no stderr/stdout")
        _warn(f"{shlex.join(cmd)} exited {r.returncode}: {detail}")
        return ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        _warn(f"{shlex.join(cmd)} failed: {type(e).__name__}: {e}")
        return ""


def _read(path: Path, max_lines: int = 200) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[:max_lines]).rstrip()
    except (OSError, PermissionError) as e:
        _warn(f"{path}: {type(e).__name__}: {e}")
        return ""


def _section(title: str, body: str) -> str:
    if not body.strip():
        return f"## {title}\n\n(no output)\n\n"
    return f"## {title}\n\n~~~text\n{body}\n~~~\n\n"


def _output_path_from_basename(raw_basename: str) -> Path:
    if not raw_basename or raw_basename.strip() != raw_basename:
        raise ValueError("--output-basename must be non-empty with no leading or trailing whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_basename):
        raise ValueError("--output-basename must not contain control characters")
    if not _SAFE_BASENAME_RE.fullmatch(raw_basename):
        raise ValueError("--output-basename may contain only letters, numbers, '.', '_', '-', and '/'")

    basename = Path(raw_basename)
    if basename.is_absolute():
        raise ValueError("--output-basename must be relative to the current working directory")
    if ".." in basename.parts:
        raise ValueError("--output-basename must not contain '..' path segments")
    if basename.name in {"", "."}:
        raise ValueError("--output-basename must include a file basename")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = basename.with_suffix("") if basename.suffix == ".md" else basename
    return stem.parent / f"{stem.name}-{timestamp}.md"


def _write_output(raw_basename: str, content: str) -> Path:
    output_path = _output_path_from_basename(raw_basename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gather system baseline snapshot (no sudo, stdlib only).")
    parser.add_argument(
        "--output-basename",
        metavar="PATH",
        help=(
            "write markdown to PATH-YYYYMMDDTHHMMSSZ.md relative to the current working directory; "
            "parent directories are created"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    out: list[str] = []
    out.append("# System snapshot (no sudo)\n\n")

    # Identity and time
    hostname = _run(["hostname"]) or _read(Path("/etc/hostname"))
    uname = _run(["uname", "-a"])
    date = _run(["date", "-Iseconds"]) or _run(["date"])
    out.append(_section("Identity and time", f"hostname: {hostname}\n{uname}\n{date}"))

    # OS release
    os_release = _read(Path("/etc/os-release"))
    if os_release:
        out.append(_section("OS release", os_release))

    # Uptime and load
    uptime = _run(["uptime"])
    loadavg = _read(Path("/proc/uptime")) + "\n" + _read(Path("/proc/loadavg"))
    out.append(_section("Uptime and load", f"{uptime}\n\n{loadavg}".strip()))

    # CPU
    lscpu = _run(["lscpu"])
    if not lscpu:
        lscpu = _read(Path("/proc/cpuinfo"), max_lines=80)
    out.append(_section("CPU", lscpu or "(unavailable)"))

    # Memory
    free = _run(["free", "-h"])
    meminfo = _read(Path("/proc/meminfo"), max_lines=30)
    out.append(_section("Memory", f"{free}\n\n{meminfo}".strip() or "(unavailable)"))

    # Disk and block
    df_h = _run(["df", "-h"])
    df_i = _run(["df", "-i"])
    lsblk = _run(["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT"])
    partitions = _read(Path("/proc/partitions"))
    block = "\n\n".join(filter(None, [df_h, df_i, lsblk, partitions]))
    out.append(_section("Disk and block devices", block or "(unavailable)"))

    # Network (no sudo)
    ip_addr = _run(["ip", "-br", "addr"])
    if not ip_addr:
        ip_addr = _run(["ip", "addr"])
    ip_route = _run(["ip", "route"])
    resolv = _read(Path("/etc/resolv.conf"))
    net = "\n\n".join(filter(None, [ip_addr, ip_route, resolv]))
    out.append(_section("Network", net or "(unavailable)"))

    # Users / sessions
    who = _run(["w"]) or _run(["who"])
    out.append(_section("Sessions", who or "(unavailable)"))

    # Kernel modules (count + sample)
    mods = _read(Path("/proc/modules"), max_lines=50)
    if mods:
        lines = [l.split()[0] for l in mods.splitlines()]
        out.append(_section("Loaded modules (sample)", f"{len(lines)} shown\n" + "\n".join(lines)))

    # Runtimes in PATH (no package DB)
    runtimes: list[str] = []
    for name, version_cmd in [
        ("python3", ["python3", "--version"]),
        ("node", ["node", "--version"]),
    ]:
        which = _run(["which", name])
        ver = _run(version_cmd)
        if which or ver:
            runtimes.append(f"{name}: {which or '(not in PATH)'}  {ver or ''}".strip())
    if runtimes:
        out.append(_section("Runtimes (PATH)", "\n".join(runtimes)))

    content = "".join(out)
    if args.output_basename:
        try:
            output_path = _write_output(args.output_basename, content)
        except (OSError, ValueError) as e:
            print(f"[system_snapshot] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        print(output_path)
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
