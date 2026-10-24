"""Public result and identity types for protected OpenPGP key generation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_MAX_IDENTITY_FIELD: Final = 254
_MAX_CERTIFICATION_NOTATIONS: Final = 16
_MAX_CERTIFICATION_NOTATION_NAME_BYTES: Final = 255
_MAX_CERTIFICATION_NOTATION_VALUE_BYTES: Final = 4096
_MAX_CERTIFICATION_NOTATION_TOTAL_BYTES: Final = 16_384
_NOTATION_LABEL: Final = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_NOTATION_LOCAL_NAME: Final = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?"
_CERTIFICATION_NOTATION_NAME: Final = re.compile(
    rf"{_NOTATION_LOCAL_NAME}@{_NOTATION_LABEL}(?:\.{_NOTATION_LABEL})+\Z",
    re.ASCII,
)


class KeyGenerationErrorCode(StrEnum):
    """Stable, non-secret failures raised by protected key generation."""

    INVALID_IDENTITY = "INVALID_IDENTITY"
    INVALID_CERTIFICATION_NOTATION = "INVALID_CERTIFICATION_NOTATION"
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
    certification_notations: tuple[tuple[str, str], ...] = ()

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
        if not _valid_certification_notations(self.certification_notations):
            raise KeyGenerationError(
                KeyGenerationErrorCode.INVALID_CERTIFICATION_NOTATION
            )


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


def _valid_certification_notations(value: object) -> bool:
    """Accept only bounded, immutable, explicitly namespaced notation pairs."""
    if type(value) is not tuple or len(value) > _MAX_CERTIFICATION_NOTATIONS:
        return False
    seen: set[str] = set()
    total_bytes = 0
    for entry in value:
        if type(entry) is not tuple or len(entry) != 2:
            return False
        name, notation_value = entry
        if (
            not isinstance(name, str)
            or len(name) > _MAX_CERTIFICATION_NOTATION_NAME_BYTES
            or _CERTIFICATION_NOTATION_NAME.fullmatch(name) is None
        ):
            return False
        try:
            name_bytes = name.encode("ascii")
        except UnicodeEncodeError:
            return False
        if len(name_bytes) > _MAX_CERTIFICATION_NOTATION_NAME_BYTES:
            return False
        if name.casefold() in seen:
            return False
        seen.add(name.casefold())
        if (
            not isinstance(notation_value, str)
            or len(notation_value) > _MAX_CERTIFICATION_NOTATION_VALUE_BYTES
            or not notation_value.strip()
        ):
            return False
        if any(
            unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
            for character in notation_value
        ):
            return False
        try:
            value_bytes = notation_value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        if len(value_bytes) > _MAX_CERTIFICATION_NOTATION_VALUE_BYTES:
            return False
        total_bytes += len(name_bytes) + len(value_bytes) + 1
        if total_bytes > _MAX_CERTIFICATION_NOTATION_TOTAL_BYTES:
            return False
    return True
