"""Explicit, redacted secret references for trusted OpenPGP callers.

Secret values are returned only from :meth:`SecretResolver.resolve`; they are
never retained in a reference, included in errors, or emitted by a CLI surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import os
import re
import selectors
import subprocess
import time
from typing import Final

__all__ = [
    "SecretProvider",
    "SecretReference",
    "SecretResolutionError",
    "SecretResolutionErrorCode",
    "SecretResolver",
]

_SECRET_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_ENV_MAN_TIMEOUT_SECONDS: Final = 5.0
_ENV_MAN_MAX_OUTPUT_BYTES: Final = 1_048_576
_ENV_MAN_READ_SIZE: Final = 8_192


class SecretProvider(StrEnum):
    """Supported explicit sources for protected secret material."""

    ENV = "env"
    ENVMAN = "envman"


class SecretResolutionErrorCode(StrEnum):
    """Stable error identifiers that contain no source or secret data."""

    INVALID_REFERENCE = "INVALID_REFERENCE"
    MISSING_SECRET = "MISSING_SECRET"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    INVALID_SOURCE_RESPONSE = "INVALID_SOURCE_RESPONSE"
    SOURCE_OUTPUT_LIMIT = "SOURCE_OUTPUT_LIMIT"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"


class SecretResolutionError(RuntimeError):
    """Safe failure raised without echoing source names, values, or diagnostics."""

    def __init__(self, code: SecretResolutionErrorCode) -> None:
        self.code = SecretResolutionErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class SecretReference:
    """A validated source/name pair; it deliberately contains no secret value."""

    provider: SecretProvider
    name: str

    def __post_init__(self) -> None:
        try:
            provider = SecretProvider(self.provider)
        except (TypeError, ValueError):
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_REFERENCE
            ) from None
        object.__setattr__(self, "provider", provider)
        if (
            not isinstance(self.name, str)
            or _SECRET_NAME_RE.fullmatch(self.name) is None
        ):
            raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)


class SecretResolver:
    """Resolve explicitly selected sources for trusted internal callers only.

    The returned value is sensitive process memory. Callers must not log it,
    include it in diagnostics, or serialize it. This API is not a CLI reveal
    command and has no implicit provider fallback or cache.
    """

    def resolve(self, reference: SecretReference) -> str:
        """Return the selected secret for a trusted internal caller."""
        if not isinstance(reference, SecretReference):
            raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)
        if reference.provider is SecretProvider.ENV:
            value = os.environ.get(reference.name)
            if not value:
                raise SecretResolutionError(SecretResolutionErrorCode.MISSING_SECRET)
            return value
        if reference.provider is SecretProvider.ENVMAN:
            return self._resolve_envman(reference.name)
        raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)

    @staticmethod
    def _resolve_envman(name: str) -> str:
        deadline = time.monotonic() + _ENV_MAN_TIMEOUT_SECONDS
        try:
            process = subprocess.Popen(
                ["envman", "get", "--json", "--reveal", name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except (OSError, ValueError):
            raise SecretResolutionError(
                SecretResolutionErrorCode.SOURCE_UNAVAILABLE
            ) from None

        try:
            output = _read_bounded_output(process, deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SecretResolutionError(SecretResolutionErrorCode.SOURCE_TIMEOUT)
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise SecretResolutionError(
                    SecretResolutionErrorCode.SOURCE_TIMEOUT
                ) from None
            if return_code != 0:
                raise SecretResolutionError(
                    SecretResolutionErrorCode.SOURCE_UNAVAILABLE
                )
        except SecretResolutionError:
            _terminate(process)
            raise
        except Exception:
            _terminate(process)
            raise SecretResolutionError(
                SecretResolutionErrorCode.SOURCE_UNAVAILABLE
            ) from None
        finally:
            _close_pipes(process)

        try:
            response = json.loads(
                output.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
            ) from None
        variable = response.get("variable") if isinstance(response, dict) else None
        if (
            not isinstance(variable, dict)
            or variable.get("name") != name
            or "value" not in variable
        ):
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
            )
        value = variable["value"]
        if not isinstance(value, str):
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
            )
        if not value:
            raise SecretResolutionError(SecretResolutionErrorCode.MISSING_SECRET)
        return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _read_bounded_output(process: subprocess.Popen[bytes], deadline: float) -> bytes:
    if process.stdout is None or process.stderr is None:
        raise OSError
    stdout = bytearray()
    total = 0
    with selectors.DefaultSelector() as selector:
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SecretResolutionError(SecretResolutionErrorCode.SOURCE_TIMEOUT)
            events = selector.select(remaining)
            if not events:
                raise SecretResolutionError(SecretResolutionErrorCode.SOURCE_TIMEOUT)
            for key, _ in events:
                pipe = process.stdout if key.data == "stdout" else process.stderr
                chunk = os.read(pipe.fileno(), _ENV_MAN_READ_SIZE)
                if not chunk:
                    selector.unregister(pipe)
                    pipe.close()
                    continue
                total += len(chunk)
                if total > _ENV_MAN_MAX_OUTPUT_BYTES:
                    raise SecretResolutionError(
                        SecretResolutionErrorCode.SOURCE_OUTPUT_LIMIT
                    )
                if key.data == "stdout":
                    stdout.extend(chunk)
    return bytes(stdout)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _close_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            try:
                pipe.close()
            except OSError:
                pass
