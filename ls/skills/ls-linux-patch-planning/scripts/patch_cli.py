#!/usr/bin/env python3
"""Linux patcher planning CLI.

This helper intentionally emits an auditable plan instead of executing remote
patch commands. Remote execution depends on site-specific SSH, sudo, package
manager, Docker, and maintenance-window policy; the skill guides the operator
through those checks without pretending a missing shell helper exists.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

HOST_MAX = 512
PATH_MAX = 4096
HOST_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")


class InputError(ValueError):
    """Raised when user-provided input is malformed or unsafe."""


CAPABILITIES = {
    "mode": "plan-only",
    "available": ["status", "auto", "host-only", "host-full", "multiple"],
    "unavailable": [
        "PatchMon API querying",
        "remote SSH execution",
        "package manager execution",
        "Docker execution",
        "parallel host updates",
    ],
}

PACKAGE_PREFLIGHT = (
    'if pm=$(command -v apt 2>/dev/null); then sudo -n -l -- "$pm" update; '
    'elif pm=$(command -v dnf 2>/dev/null); then sudo -n -l -- "$pm" check-update; '
    'elif pm=$(command -v yum 2>/dev/null); then sudo -n -l -- "$pm" check-update; '
    'elif pm=$(command -v zypper 2>/dev/null); then sudo -n -l -- "$pm" list-updates; '
    "else printf '%s\\n' 'no supported package manager found' >&2; exit 1; fi"
)


def _clean_text(value: str, *, max_len: int, label: str) -> str:
    if not isinstance(value, str):
        raise InputError(f"{label}: expected string")
    cleaned = value.strip()
    if not cleaned:
        raise InputError(f"{label}: empty value")
    if len(cleaned) > max_len:
        raise InputError(f"{label}: value exceeds {max_len} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise InputError(f"{label}: contains control characters")
    return cleaned


def _validate_host(value: str) -> str:
    host = _clean_text(value, max_len=HOST_MAX, label="host")
    if not HOST_RE.fullmatch(host):
        raise InputError("host: use only letters, numbers, dot, dash, underscore, colon, @")
    return host


def _validate_remote_path(value: str) -> str:
    path = _clean_text(value, max_len=PATH_MAX, label="docker_path")
    if not path.startswith("/"):
        raise InputError("docker_path: provide an absolute path on the remote host")
    return path


def _validate_config(value: str) -> Path:
    path = Path(_clean_text(value, max_len=PATH_MAX, label="config"))
    if not path.is_file():
        raise InputError(f"config: file not found: {path}")
    return path


def _ssh_command(host: str, remote_script: str) -> str:
    return f"ssh {shlex.quote(host)} {shlex.quote(remote_script)}"


def _host_only_steps(host: str) -> list[dict[str, str]]:
    return [
        {
            "phase": "preflight",
            "command": _ssh_command(
                host,
                PACKAGE_PREFLIGHT,
            ),
        },
        {
            "phase": "packages",
            "command": _ssh_command(
                host,
                "<run the distro package update command from SKILL.md after confirming maintenance window>",
            ),
        },
        {
            "phase": "verify",
            "command": _ssh_command(
                host,
                "test -f /var/run/reboot-required && echo reboot-required || true",
            ),
        },
    ]


def _host_full_steps(host: str, docker_path: str) -> list[dict[str, str]]:
    steps = _host_only_steps(host)
    docker_path_arg = shlex.quote(docker_path)
    steps.extend(
        [
            {
                "phase": "docker-preflight",
                "command": _ssh_command(
                    host,
                    f"test -d {docker_path_arg} && command -v docker",
                ),
            },
            {
                "phase": "docker-update",
                "command": _ssh_command(
                    host,
                    f"cd {docker_path_arg} && sudo docker compose pull && sudo docker compose up -d",
                ),
            },
            {
                "phase": "docker-verify",
                "command": _ssh_command(
                    host,
                    f"cd {docker_path_arg} && sudo docker compose ps",
                ),
            },
        ]
    )
    return steps


def _load_hosts_config(path: Path) -> list[dict[str, str]]:
    hosts: list[dict[str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) not in (1, 2):
            raise InputError(f"config:{lineno}: expected host or host,docker_path")
        host = _validate_host(parts[0])
        entry = {"host": host}
        if len(parts) == 2 and parts[1]:
            entry["docker_path"] = _validate_remote_path(parts[1])
        hosts.append(entry)
    if not hosts:
        raise InputError("config: no hosts found")
    return hosts


def _emit_plan(title: str, steps: list[dict[str, str]], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps({"title": title, "mode": "plan-only", "steps": steps}, indent=2))
        return 0
    print(f"# {title}")
    print()
    print("Mode: plan-only. Review commands, confirm a maintenance window, then run manually.")
    print()
    for idx, step in enumerate(steps, start=1):
        print(f"{idx}. **{step['phase']}**")
        print()
        print("```bash")
        print(step["command"])
        print("```")
        print()
    return 0


def _emit_status(*, json_output: bool) -> int:
    if json_output:
        print(json.dumps(CAPABILITIES, indent=2))
        return 0
    print("# Linux Patcher Status")
    print()
    print("Mode: plan-only. This helper never opens SSH sessions or applies updates.")
    print()
    print("Available modes:")
    for item in CAPABILITIES["available"]:
        print(f"- `{item}`")
    print()
    print("Unavailable until a tested Python implementation is added:")
    for item in CAPABILITIES["unavailable"]:
        print(f"- {item}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create safe Linux patching plans.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show available and unavailable capabilities")

    auto = sub.add_parser("auto", help="Explain PatchMon automatic mode requirements")
    auto.add_argument("--skip-docker", action="store_true", help="Plan packages-only behavior")
    auto.add_argument("--dry-run", action="store_true", help="Keep output plan-only")

    host_only = sub.add_parser("host-only", help="Plan package updates for one host")
    host_only.add_argument("host", help="user@hostname")

    host_full = sub.add_parser("host-full", help="Plan package and Docker updates for one host")
    host_full.add_argument("host", help="user@hostname")
    host_full.add_argument("docker_path", help="Absolute Docker Compose directory on the remote host")

    multiple = sub.add_parser("multiple", help="Plan updates from a simple host config file")
    multiple.add_argument("config", help="File with host or host,docker_path per line")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "status":
            return _emit_status(json_output=args.json)
        if args.cmd == "auto":
            mode = "packages only" if args.skip_docker else "packages and Docker where configured"
            dry_run_note = "The --dry-run flag was accepted for compatibility; all shipped modes are already plan-only." if args.dry_run else "Use --dry-run if callers require an explicit preview flag; behavior is unchanged."
            steps = [
                {"phase": "status", "command": "PatchMon automatic execution is unavailable and guidance-only in the current framework until a tested Python API client is added."},
                {"phase": "dry-run", "command": dry_run_note},
                {"phase": "inputs", "command": f"Collect PatchMon URL, credentials, target host list, and maintenance window for {mode}."},
                {"phase": "fallback", "command": "Use `python scripts/patch_cli.py host-only HOST` or `host-full HOST /compose/path` for auditable per-host plans."},
            ]
            return _emit_plan("PatchMon Automatic Patching Plan", steps, json_output=args.json)
        if args.cmd == "host-only":
            host = _validate_host(args.host)
            return _emit_plan(f"Host Package Patch Plan: {host}", _host_only_steps(host), json_output=args.json)
        if args.cmd == "host-full":
            host = _validate_host(args.host)
            docker_path = _validate_remote_path(args.docker_path)
            return _emit_plan(f"Host Full Patch Plan: {host}", _host_full_steps(host, docker_path), json_output=args.json)
        config = _validate_config(args.config)
        steps: list[dict[str, str]] = []
        for entry in _load_hosts_config(config):
            if "docker_path" in entry:
                steps.extend(_host_full_steps(entry["host"], entry["docker_path"]))
            else:
                steps.extend(_host_only_steps(entry["host"]))
        return _emit_plan(f"Multi-host Patch Plan: {config}", steps, json_output=args.json)
    except InputError as exc:
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
