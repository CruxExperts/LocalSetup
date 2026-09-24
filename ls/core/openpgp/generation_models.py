"""Public result and identity types for protected OpenPGP key generation."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_MAX_IDENTITY_FIELD: Final = 254


class KeyGenerationErrorCode(StrEnum):
    """Stable, non-secret failures raised by protected key generation."""

    INVALID_IDENTITY = "INVALID_IDENTITY"
    INVALID_PROFILE = "INVALID_PROFILE"
    INVALID_SECRET_REFERENCE = "INVALID_SECRET_REFERENCE"
    SECRET_RESOLUTION_FAILED = "SECRET_RESOLUTION_FAILED"
    INVALID_PASSPHRASE = "INVALID_PASSPHRASE"
    INVALID_KEYRING_HOME = "INVALID_KEYRING_HOME"
    KEYRING_NOT_EMPTY = "KEYRING_NOT_EMPTY"
    KEYRING_UNAVAILABLE = "KEYRING_UNAVAILABLE"
    GPG_UNAVAILABLE = "GPG_UNAVAILABLE"
    GPG_FAILED = "GPG_FAILED"
    GPG_TIMEOUT = "GPG_TIMEOUT"
    GPG_OUTPUT_LIMIT = "GPG_OUTPUT_LIMIT"
    GENERATED_KEY_INVALID = "GENERATED_KEY_INVALID"


class KeyGenerationError(RuntimeError):
    """Safe generation failure with no identity, secret, or process details."""

    def __init__(self, code: KeyGenerationErrorCode) -> None:
        self.code = KeyGenerationErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class KeyIdentity:
    """Explicit user-ID fields for a generated key."""

    name: str
    email: str | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        if not _valid_identity_field(self.name, required=True):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)
        if self.email is not None and (
            not _valid_identity_field(self.email, required=True)
            or "@" not in self.email
            or any(character.isspace() for character in self.email)
            or "<" in self.email
            or ">" in self.email
        ):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)
        if self.comment is not None and not _valid_identity_field(
            self.comment, required=True
        ):
            raise KeyGenerationError(KeyGenerationErrorCode.INVALID_IDENTITY)


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """Nonsecret generation result; the private key stays in ``keyring_home``."""

    public_certificate: bytes
    primary_fingerprint: str
    subkey_fingerprints: tuple[str, ...]


def _valid_identity_field(value: object, *, required: bool) -> bool:
    if not isinstance(value, str) or len(value) > _MAX_IDENTITY_FIELD:
        return False
    if required and not value.strip():
        return False
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in value
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
