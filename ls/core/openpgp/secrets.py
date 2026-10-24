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
from typing import Final, Mapping
from pathlib import Path

from .secret_files import (
    _MAX_SECRET_BYTES,
    _SecretFileError,
    _SecretFileErrorCode,
    _create_protected_secret_file,
    _read_protected_secret_file,
)
from .native_input import (
    InputOperation,
    NativeInputContext,
    NativeInputError,
)

__all__ = [
    "SecretProvider",
    "SecretReference",
    "SecretResolutionError",
    "SecretResolutionErrorCode",
    "SecretResolver",
    "create_protected_secret",
]

_SECRET_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_ENV_MAN_TIMEOUT_SECONDS: Final = 5.0
_ENV_MAN_MAX_OUTPUT_BYTES: Final = 1_048_576
_ENV_MAN_READ_SIZE: Final = 8_192
_MAX_PASSPHRASE_BYTES: Final = 4096
_PROTECTED_FILE_MAX_BYTES: Final = _MAX_SECRET_BYTES


class SecretProvider(StrEnum):
    """Supported explicit sources for protected secret material."""

    ENV = "env"
    ENVMAN = "envman"
    PROTECTED_FILE = "protected-file"


class SecretResolutionErrorCode(StrEnum):
    """Stable error identifiers that contain no source or secret data."""

    INVALID_REFERENCE = "INVALID_REFERENCE"
    MISSING_SECRET = "MISSING_SECRET"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    INVALID_SOURCE_RESPONSE = "INVALID_SOURCE_RESPONSE"
    SOURCE_OUTPUT_LIMIT = "SOURCE_OUTPUT_LIMIT"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    PARTIAL_SECRET = "PARTIAL_SECRET"


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

    def __init__(self, *, envman_binary: str | os.PathLike[str] = "envman",
                 env_values: Mapping[str, str] | None = None,
                 protected_file_root: str | os.PathLike[str] | None = None) -> None:
        try:
            executable = os.fspath(envman_binary)
        except (TypeError, ValueError):
            raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE) from None
        if (not isinstance(executable, str) or not executable or "\x00" in executable
                or (executable != "envman" and not os.path.isabs(executable))):
            raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)
        self._envman_binary = executable
        self._env_values = None if env_values is None else dict(env_values)
        self._protected_file_root = _validated_protected_file_root(protected_file_root)

    def resolve(self, reference: SecretReference) -> str:
        """Return the selected secret for a trusted internal caller."""
        if not isinstance(reference, SecretReference):
            raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)
        if reference.provider is SecretProvider.ENV:
            values = os.environ if self._env_values is None else self._env_values
            value = values.get(reference.name)
            if not value:
                raise SecretResolutionError(SecretResolutionErrorCode.MISSING_SECRET)
            return value
        if reference.provider is SecretProvider.ENVMAN:
            return self._resolve_envman(reference.name)
        if reference.provider is SecretProvider.PROTECTED_FILE:
            return self._resolve_protected_file(reference.name)
        raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)

    def resolve_buffer(
        self,
        reference: SecretReference | None,
        *,
        operation: InputOperation,
        native_input: NativeInputContext | None = None,
    ) -> bytearray:
        """Resolve exactly one explicit source into a bounded mutable buffer.

        Native input is one-use and never falls through to another secret
        provider. Legacy references keep their existing
        source behavior while callers that need disposal use this byte API.
        """
        if (
            not isinstance(operation, InputOperation)
            or operation.purpose not in {"seal-envelope", "open-envelope", "generate-key"}
            or ((reference is None) == (native_input is None))
        ):
            raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)
        if native_input is not None:
            if not isinstance(native_input, NativeInputContext):
                raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)
            try:
                payload = native_input.request_passphrase(operation)
            except NativeInputError:
                raise SecretResolutionError(
                    SecretResolutionErrorCode.SOURCE_UNAVAILABLE
                ) from None
            if not isinstance(payload, bytearray) or not _valid_secret_buffer(payload):
                if isinstance(payload, bytearray):
                    _wipe_secret(payload)
                raise SecretResolutionError(
                    SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
                )
            return payload

        if not isinstance(reference, SecretReference):
            raise SecretResolutionError(SecretResolutionErrorCode.INVALID_REFERENCE)
        value = self.resolve(reference)
        if not isinstance(value, str):
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
            )
        try:
            payload = bytearray(value.encode("utf-8", "strict"))
        except UnicodeEncodeError:
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
            ) from None
        if not _valid_secret_buffer(payload):
            _wipe_secret(payload)
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
            )
        return payload

    def _resolve_protected_file(self, name: str) -> str:
        root = self._protected_file_root
        if root is None:
            raise SecretResolutionError(SecretResolutionErrorCode.SOURCE_UNAVAILABLE)
        try:
            payload = _read_protected_secret_file(root, name)
        except _SecretFileError as exc:
            code = {
                _SecretFileErrorCode.MISSING: SecretResolutionErrorCode.MISSING_SECRET,
                _SecretFileErrorCode.OUTPUT_LIMIT: SecretResolutionErrorCode.SOURCE_OUTPUT_LIMIT,
                _SecretFileErrorCode.INVALID_REQUEST: SecretResolutionErrorCode.INVALID_REFERENCE,
                _SecretFileErrorCode.UNAVAILABLE: SecretResolutionErrorCode.SOURCE_UNAVAILABLE,
            }[exc.code]
            raise SecretResolutionError(code) from None
        try:
            value = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
            ) from None
        if (
            not value
            or "\x00" in value
            or "\r" in value
            or "\n" in value
            or len(payload) > _PROTECTED_FILE_MAX_BYTES
        ):
            raise SecretResolutionError(
                SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
            )
        del payload
        return value

    def _resolve_envman(self, name: str) -> str:
        deadline = time.monotonic() + _ENV_MAN_TIMEOUT_SECONDS
        try:
            process = subprocess.Popen(
                [self._envman_binary, "get", "--json", "--reveal", name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                env={key: os.environ[key] for key in (
                    "PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL"
                ) if key in os.environ},
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


def create_protected_secret(
    protected_file_root: str | os.PathLike[str], name: str
) -> tuple[SecretReference, bool]:
    """Create or reuse a random protected passphrase and return its reference.

    This primitive is for trusted setup owners. It never returns the generated
    passphrase; consumers resolve the symbolic reference through the existing
    explicit resolver when they need to use it. The boolean is true only when
    this call created the file and false when it reused a valid existing file.
    """
    reference = SecretReference(SecretProvider.PROTECTED_FILE, name)
    root = _validated_protected_file_root(protected_file_root)
    if root is None:
        raise SecretResolutionError(SecretResolutionErrorCode.SOURCE_UNAVAILABLE)
    try:
        created = _create_protected_secret_file(root, reference.name)
    except _SecretFileError as exc:
        code = {
            _SecretFileErrorCode.INVALID_REQUEST: SecretResolutionErrorCode.INVALID_REFERENCE,
            _SecretFileErrorCode.PARTIAL: SecretResolutionErrorCode.PARTIAL_SECRET,
            _SecretFileErrorCode.MISSING: SecretResolutionErrorCode.MISSING_SECRET,
            _SecretFileErrorCode.OUTPUT_LIMIT: SecretResolutionErrorCode.SOURCE_OUTPUT_LIMIT,
            _SecretFileErrorCode.UNAVAILABLE: SecretResolutionErrorCode.SOURCE_UNAVAILABLE,
        }[exc.code]
        raise SecretResolutionError(code) from None
    return reference, created


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


def _validated_protected_file_root(
    value: str | os.PathLike[str] | None,
) -> Path | None:
    if value is None:
        return None
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError
        root = Path(raw)
        if (
            not root.is_absolute()
            or not root.name
            or ".." in root.parts
            or len(root.parts) > 128
        ):
            raise ValueError
    except (OSError, TypeError, ValueError):
        raise SecretResolutionError(
            SecretResolutionErrorCode.INVALID_REFERENCE
        ) from None
    return root


def _valid_secret_buffer(value: bytearray) -> bool:
    return (
        1 <= len(value) <= _MAX_PASSPHRASE_BYTES
        and b"\x00" not in value
        and b"\r" not in value
        and b"\n" not in value
    )


def _wipe_secret(value: bytearray) -> None:
    value[:] = b"\x00" * len(value)
    value.clear()
