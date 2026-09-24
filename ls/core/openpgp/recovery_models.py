"""Shared protected-backup errors, records, limits, and input validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Iterable

from .contracts import (
    KeyCapability,
    KeyProfile,
    OpenPGPContractError,
    normalize_fingerprint,
)
from .secrets import SecretReference, SecretResolver

_MAX_PASSPHRASE_BYTES: Final = 4096
_MAX_PLAINTEXT_BYTES: Final = 16_777_216
_MAX_BACKUP_BYTES: Final = 16_777_216
_MAX_DIAGNOSTIC_BYTES: Final = 1_048_576
_GPG_TIMEOUT_SECONDS: Final = 30.0
_GPG_READ_SIZE: Final = 8192

class RecoveryErrorCode(StrEnum):
    """Stable failures without key material, identity text, or diagnostics."""

    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_FINGERPRINT = "INVALID_FINGERPRINT"
    INVALID_KEYRING_HOME = "INVALID_KEYRING_HOME"
    KEYRING_NOT_EMPTY = "KEYRING_NOT_EMPTY"
    KEYRING_UNAVAILABLE = "KEYRING_UNAVAILABLE"
    HOMES_NOT_ISOLATED = "HOMES_NOT_ISOLATED"
    OWNER_KEY_INVALID = "OWNER_KEY_INVALID"
    OWNER_POLICY_INVALID = "OWNER_POLICY_INVALID"
    RECOVERY_KEY_INVALID = "RECOVERY_KEY_INVALID"
    SECRET_RESOLUTION_FAILED = "SECRET_RESOLUTION_FAILED"
    INVALID_PASSPHRASE = "INVALID_PASSPHRASE"
    GPG_UNAVAILABLE = "GPG_UNAVAILABLE"
    GPG_FAILED = "GPG_FAILED"
    GPG_TIMEOUT = "GPG_TIMEOUT"
    IO_LIMIT = "IO_LIMIT"
    BACKUP_INVALID = "BACKUP_INVALID"
    BACKUP_EXISTS = "BACKUP_EXISTS"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    RESTORE_INVALID = "RESTORE_INVALID"


class RecoveryError(RuntimeError):
    """A redacted recovery failure with a stable non-secret code."""

    def __init__(self, code: RecoveryErrorCode) -> None:
        self.code = RecoveryErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class ProtectedBackup:
    """Non-secret receipt for a newly created encrypted backup."""

    path: Path
    primary_fingerprint: str
    recovery_recipient_fingerprint: str


@dataclass(frozen=True, slots=True)
class RecoveredKey:
    """Non-secret result for a key verified in the caller-selected home."""

    keyring_home: Path
    primary_fingerprint: str
    recovery_recipient_fingerprint: str


def _validated_executable(value: str | os.PathLike[str]) -> str:
    try:
        executable = os.fspath(value)
    except (TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE) from None
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise RecoveryError(RecoveryErrorCode.GPG_UNAVAILABLE)
    return executable


def _fingerprint(value: str) -> str:
    try:
        return normalize_fingerprint(value)
    except (OpenPGPContractError, TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.INVALID_FINGERPRINT) from None


def _capabilities(
    values: Iterable[KeyCapability | str],
) -> frozenset[KeyCapability]:
    try:
        if isinstance(values, (str, bytes, bytearray, dict)):
            raise ValueError
        selected = frozenset(KeyCapability(value) for value in values)
        if not selected or not selected.issubset(
            {KeyCapability.SIGN, KeyCapability.ENCRYPT}
        ):
            raise ValueError
        return selected
    except (TypeError, ValueError):
        raise RecoveryError(RecoveryErrorCode.OWNER_POLICY_INVALID) from None


def _validate_policy_inputs(
    profile: KeyProfile, capabilities: frozenset[KeyCapability]
) -> None:
    if not isinstance(profile, KeyProfile) or not capabilities:
        raise RecoveryError(RecoveryErrorCode.OWNER_POLICY_INVALID)


def _resolve_passphrase(
    reference: SecretReference,
    resolver: SecretResolver,
    *,
    failure_code: RecoveryErrorCode,
) -> bytearray:
    try:
        secret = resolver.resolve(reference)
    except Exception:
        raise RecoveryError(failure_code) from None
    try:
        payload = _passphrase_payload(secret)
    finally:
        del secret
    if payload is None:
        raise RecoveryError(RecoveryErrorCode.INVALID_PASSPHRASE)
    return payload


def _passphrase_payload(secret: object) -> bytearray | None:
    if (
        not isinstance(secret, str)
        or not secret
        or any(character in secret for character in ("\x00", "\r", "\n"))
    ):
        return None
    try:
        encoded = str.encode(secret, "utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_PASSPHRASE_BYTES:
        return None
    return bytearray(encoded + b"\n")
