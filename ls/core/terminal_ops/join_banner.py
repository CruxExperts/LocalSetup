"""Deterministic, terminal-safe shell join advertisements."""

from __future__ import annotations

import re
import shlex
import unicodedata
from collections.abc import Mapping, Sequence


_SESSION_ALIAS = re.compile(r"ls-[a-f0-9]{8}-[a-f0-9]{8}\Z")
_SESSION_HANDLE = re.compile(r"lsshell1\.[a-f0-9]{24}\.[a-f0-9]{32}\Z")
_OPERATION_HANDLE = re.compile(r"lsopcmd1\.[a-f0-9]{24}\.[a-f0-9]{32}\.[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LINE_CONTROLS = {"\n", "\r", "\v", "\f", "\x85", "\u2028", "\u2029"}


class JoinBannerError(ValueError):
    """An advertisement is not safe to show as an attach instruction."""


def _has_control(text: str) -> bool:
    return any(character in _LINE_CONTROLS or unicodedata.category(character).startswith("C")
               for character in text)


def _display_width(text: str) -> int:
    width = 0
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("M"):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def validate_join_entry(entry: Mapping[str, object]) -> tuple[str, str]:
    """Return a safe host and unchanged service-generated attach command."""
    host = entry.get("host")
    command = entry.get("join_command")
    if not isinstance(host, str) or not host or _has_control(host):
        raise JoinBannerError("The verified machine host is missing or contains terminal controls.")
    if not isinstance(command, str) or not command or _has_control(command):
        raise JoinBannerError("The service attach command is missing or contains terminal controls.")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise JoinBannerError("The service attach command is malformed.") from exc
    if len(tokens) != 5 or tokens[:3] != ["localsetup", "shell", "attach"]:
        raise JoinBannerError("Only the service attach command may be advertised.")
    flag, target = tokens[3:]
    if flag == "--session":
        valid_target = bool(_SESSION_ALIAS.fullmatch(target))
    elif flag == "--handle":
        valid_target = bool(_SESSION_HANDLE.fullmatch(target) or _OPERATION_HANDLE.fullmatch(target))
    else:
        valid_target = False
    if not valid_target:
        raise JoinBannerError("Only a verified session or operation attach command may be advertised.")
    return host, command


def render_join_banner(entries: Sequence[Mapping[str, object]], *, color: bool = False) -> str:
    """Format host and attach rows at one shared display width."""
    if not entries:
        return ""
    validated = [validate_join_entry(entry) for entry in entries]
    width = max(max(_display_width(command), _display_width(host) + 4)
                for host, command in validated)

    def host_row(host: str) -> str:
        centered = f" {host} "
        remaining = width - _display_width(centered)
        left = remaining // 2
        right = remaining - left
        return "#" * left + centered + "#" * right

    rows: list[str] = []
    for host, command in validated:
        rows.extend((host_row(host), "", command, ""))
    rows.append("#" * width)
    if color:
        rows = [f"\x1b[36m{row}\x1b[0m" if row.startswith("#") else row for row in rows]
    return "\n".join(rows)
