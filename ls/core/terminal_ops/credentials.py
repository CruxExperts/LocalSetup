"""Transient sudo credential lookup for operation-bound terminal runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

MAX_CREDENTIAL_BYTES = 4096
ENV_KEY_SUFFIX = "_SUDO_KEY"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class CredentialCandidate:
    """A credential kept in memory only, paired with its safe variable name."""

    variable: str
    value: str = field(repr=False)


def child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy an environment while removing every sudo credential value."""
    return {
        name: value
        for name, value in environment.items()
        if not name.endswith(ENV_KEY_SUFFIX)
    }


def candidate_names(target: str) -> tuple[str]:
    """Return the single supported credential variable name.

    ``target`` remains part of the operation binding, but credentials are
    intentionally shared through one explicit controller variable so renamed
    hosts and repositories cannot select stale hostname-specific secrets.
    """
    if not isinstance(target, str) or not target or "\0" in target:
        raise ValueError("sudo target must be explicit")
    return ("GENERIC_SUDO_KEY",)


def valid_credential(value: object) -> bool:
    if not isinstance(value, str) or not value or any(ch in value for ch in "\0\r\n"):
        return False
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeError:
        return False
    return 0 < size <= MAX_CREDENTIAL_BYTES


def _envman_value(
    name: str,
    environment: Mapping[str, str],
    *,
    runner: Callable[..., object] | None = None,
    status: dict[str, str] | None = None,
) -> str | None:
    """Read a missing controller variable through EnvMan's JSON interface."""
    if not _ENV_NAME.fullmatch(name):
        return None
    executable = shutil.which("envman", path=environment.get("PATH", ""))
    if not executable:
        if status is not None:
            status[name] = "unavailable"
        return None
    invoke = subprocess.run if runner is None else runner
    try:
        result = invoke(
            [executable, "get", name, "--json", "--reveal"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=5.0,
            text=True,
            env=child_environment(environment),
        )
        returncode = getattr(result, "returncode", 1)
        if returncode != 0:
            stdout = getattr(result, "stdout", "")
            stderr = getattr(result, "stderr", "")
            if isinstance(stdout, bytes):
                if len(stdout) > 65536:
                    stdout = None
                else:
                    stdout = stdout.decode("utf-8", "strict")
            if isinstance(stderr, bytes):
                if len(stderr) > 65536:
                    stderr = None
                else:
                    stderr = stderr.decode("utf-8", "strict")
            # EnvMan's stable missing-name result is exit code 3 and the exact
            # redacted CLI diagnostic. Other failures (including unrelated
            # exit-code-3 errors) must remain loading errors.
            missing_diagnostic = f"envman: {name} is not managed.\n"
            if (returncode == 3 and stdout == "" and stderr == missing_diagnostic):
                if status is not None:
                    status[name] = "unavailable"
                return None
            if status is not None:
                status[name] = "loading_error"
            return None
        stdout = getattr(result, "stdout", "")
        if isinstance(stdout, bytes):
            if len(stdout) > 65536:
                if status is not None:
                    status[name] = "loading_error"
                return None
            stdout = stdout.decode("utf-8", "strict")
        if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > 65536:
            if status is not None:
                status[name] = "loading_error"
            return None
        payload = json.loads(stdout)
        variable = payload.get("variable") if isinstance(payload, dict) else None
        if (
            not isinstance(variable, dict)
            or variable.get("name") != name
            or not isinstance(variable.get("value"), str)
        ):
            if status is not None:
                status[name] = "loading_error"
            return None
        value = variable["value"]
        if value == "":
            if status is not None:
                status[name] = "empty"
            return None
        if not valid_credential(value):
            if status is not None:
                status[name] = "invalid"
            return None
        if status is not None:
            status[name] = "available"
        return value if valid_credential(value) else None
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError):
        # Never propagate EnvMan output or diagnostics into logs or operation state.
        if status is not None:
            status[name] = "loading_error"
        return None


def iter_candidates(
    target: str,
    environment: Mapping[str, str],
    *,
    envman_runner: Callable[..., object] | None = None,
    statuses: dict[str, str] | None = None,
) -> Iterator[CredentialCandidate]:
    """Yield the generic controller credential lazily.

    A controller environment value wins whenever its name is present, including
    an empty or malformed value. EnvMan is consulted only when that name is
    absent. The lookup is deferred until sudo actually requests authentication.
    """
    names = candidate_names(target)
    result_statuses = statuses if statuses is not None else {}
    seen: set[str] = set()
    for name in names:
        if name in environment:
            value: object = environment[name]
        else:
            envman_status: dict[str, str] = {}
            value = _envman_value(name, environment, runner=envman_runner,
                                  status=envman_status)
            if value is None:
                result_statuses[name] = envman_status.get(name, "unavailable")
                continue
        if value is None:
            result_statuses[name] = "unavailable"
            continue
        if value == "":
            result_statuses[name] = "empty"
            continue
        if not valid_credential(value):
            result_statuses[name] = "invalid"
            continue
        if value in seen:
            result_statuses[name] = "duplicate"
            continue
        seen.add(value)
        result_statuses[name] = "available"
        yield CredentialCandidate(name, value)


def normalize_candidate(
    target: str,
    variable: object,
    value: object,
) -> CredentialCandidate | None:
    """Validate values supplied by an injectable candidate source."""
    allowed = set(candidate_names(target))
    if not isinstance(variable, str) or variable not in allowed or not valid_credential(value):
        return None
    return CredentialCandidate(variable, value)


__all__ = [
    "MAX_CREDENTIAL_BYTES",
    "ENV_KEY_SUFFIX",
    "CredentialCandidate",
    "candidate_names",
    "child_environment",
    "iter_candidates",
    "normalize_candidate",
    "valid_credential",
]
