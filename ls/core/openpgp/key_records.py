"""OpenPGP key inspection records and GnuPG colon-format parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Final, TypedDict

from .contracts import (
    KeyCapability,
    KeyProfile,
    LocalTrust,
    OpenPGPContractError,
    normalize_fingerprint,
)

_ALGORITHMS: Final = {
    "1": "RSA",
    "2": "RSA",
    "3": "RSA",
    "16": "ELGAMAL",
    "17": "DSA",
    "18": "ECDH",
    "19": "ECDSA",
    "20": "ELGAMAL",
    "22": "EDDSA",
    "25": "X25519",
    "26": "X448",
    "27": "ED25519",
    "28": "ED448",
}
_CAPABILITIES: Final = {
    "e": KeyCapability.ENCRYPT,
    "s": KeyCapability.SIGN,
    "c": KeyCapability.CERTIFY,
    "a": KeyCapability.AUTHENTICATE,
}
_INSPECTION_SEAL: Final = object()


class _ParsedFields(TypedDict):
    algorithm: str
    bits: int
    capabilities: frozenset[KeyCapability]
    direct_capabilities: frozenset[KeyCapability]
    aggregate_capabilities: frozenset[KeyCapability]
    created: int
    expires: int | None
    is_primary: bool
    revoked: bool
    expired: bool
    disabled: bool
    invalid: bool


class KeyInspectionErrorCode(StrEnum):
    """Stable failures that do not expose certificate or process contents."""

    INVALID_SOURCE = "INVALID_SOURCE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_OUTPUT_LIMIT = "SOURCE_OUTPUT_LIMIT"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    INVALID_KEY = "INVALID_KEY"
    AMBIGUOUS_KEY = "AMBIGUOUS_KEY"
    KEY_NOT_FOUND = "KEY_NOT_FOUND"
    KEYRING_UNAVAILABLE = "KEYRING_UNAVAILABLE"
    UNSUPPORTED_KEYRING = "UNSUPPORTED_KEYRING"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    REVOKED_KEY = "REVOKED_KEY"
    EXPIRED_KEY = "EXPIRED_KEY"
    DISABLED_KEY = "DISABLED_KEY"
    INVALID_CAPABILITIES = "INVALID_CAPABILITIES"
    INVALID_ENROLLMENT = "INVALID_ENROLLMENT"


class KeyInspectionError(RuntimeError):
    """Safe key inspection failure with a stable code and no external details."""

    def __init__(self, code: KeyInspectionErrorCode) -> None:
        self.code = KeyInspectionErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """One primary key or subkey as reported by GnuPG's colon format."""

    fingerprint: str
    algorithm: str
    bits: int
    capabilities: frozenset[KeyCapability]
    created_on: date
    expires_on: date | None
    is_primary: bool
    expires_epoch: int | None = None
    revoked: bool = False
    expired: bool = False
    disabled: bool = False
    direct_capabilities: frozenset[KeyCapability] = field(
        default=frozenset(), repr=False, compare=False
    )
    aggregate_capabilities: frozenset[KeyCapability] = field(
        default=frozenset(), repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class KeyInspection:
    """Parsed certificate facts; it conveys no signer trust by itself."""

    primary: KeyRecord
    subkeys: tuple[KeyRecord, ...]
    _verification: object = field(default=None, repr=False, compare=False)

    @property
    def primary_fingerprint(self) -> str:
        return self.primary.fingerprint

    @property
    def records(self) -> tuple[KeyRecord, ...]:
        return (self.primary, *self.subkeys)

    @property
    def capabilities(self) -> frozenset[KeyCapability]:
        return frozenset(
            capability
            for record in self.records
            if not record.revoked and not record.expired and not record.disabled
            for capability in record.direct_capabilities
        )

    @property
    def signing_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            record.fingerprint
            for record in self.records
            if KeyCapability.SIGN in record.direct_capabilities
            and not record.revoked
            and not record.expired
            and not record.disabled
        )

    @property
    def encryption_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            record.fingerprint
            for record in self.records
            if KeyCapability.ENCRYPT in record.direct_capabilities
            and not record.revoked
            and not record.expired
            and not record.disabled
        )

    @property
    def signing_subkey_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            record.fingerprint
            for record in self.subkeys
            if KeyCapability.SIGN in record.direct_capabilities
            and not record.revoked
            and not record.expired
            and not record.disabled
        )

    @property
    def encryption_subkey_fingerprints(self) -> tuple[str, ...]:
        return tuple(
            record.fingerprint
            for record in self.subkeys
            if KeyCapability.ENCRYPT in record.direct_capabilities
            and not record.revoked
            and not record.expired
            and not record.disabled
        )

    @property
    def revoked_fingerprints(self) -> tuple[str, ...]:
        return tuple(record.fingerprint for record in self.records if record.revoked)

    @property
    def expired_fingerprints(self) -> tuple[str, ...]:
        return tuple(record.fingerprint for record in self.records if record.expired)

    @property
    def disabled_fingerprints(self) -> tuple[str, ...]:
        return tuple(record.fingerprint for record in self.records if record.disabled)

    @property
    def revoked(self) -> bool:
        return bool(self.revoked_fingerprints)

    @property
    def expired(self) -> bool:
        return bool(self.expired_fingerprints)

    @property
    def disabled(self) -> bool:
        return bool(self.disabled_fingerprints)


@dataclass(frozen=True, slots=True)
class KeyEnrollment:
    """Explicit enrollment result; persist ``local_trust.fingerprints`` yourself."""

    fingerprint: str
    local_trust: LocalTrust
    required_capabilities: frozenset[KeyCapability]
    profile: KeyProfile


def _parse_colon_output(output: bytes) -> tuple[KeyInspection, ...]:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY) from None
    records: list[KeyInspection] = []
    components: list[KeyRecord] = []
    pending: _ParsedFields | None = None
    current_fingerprint: str | None = None
    now = int(datetime.now(timezone.utc).timestamp())

    def finish_component() -> None:
        nonlocal pending, current_fingerprint
        if pending is None:
            return
        if current_fingerprint is None:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
        components.append(_record_from_fields(pending, current_fingerprint))
        pending = None
        current_fingerprint = None

    def finish_certificate() -> None:
        finish_component()
        if not components:
            return
        if not components[0].is_primary or any(
            component.is_primary for component in components[1:]
        ):
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
        records.append(
            KeyInspection(
                components[0],
                tuple(components[1:]),
                _verification=_INSPECTION_SEAL,
            )
        )
        components.clear()

    for line in text.splitlines():
        fields = line.split(":")
        kind = fields[0]
        if kind in {"sec", "ssb"}:
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
        if kind == "pub":
            finish_certificate()
            pending = _parse_key_fields(fields, is_primary=True, now=now)
        elif kind == "sub":
            finish_component()
            if not components or pending is not None:
                raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
            pending = _parse_key_fields(fields, is_primary=False, now=now)
        elif kind == "fpr":
            if pending is None or current_fingerprint is not None or len(fields) <= 9:
                raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
            try:
                current_fingerprint = normalize_fingerprint(fields[9])
            except OpenPGPContractError:
                raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY) from None
    finish_certificate()
    return tuple(records)


def _parse_key_fields(
    fields: list[str], *, is_primary: bool, now: int
) -> _ParsedFields:
    if len(fields) <= 11:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    try:
        bits = int(fields[2], 10)
        algorithm = _ALGORITHMS[fields[3]]
        created = int(fields[5], 10)
        expires = int(fields[6], 10) if fields[6] else None
    except (KeyError, ValueError):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY) from None
    if bits < 1 or created < 1 or (expires is not None and expires < 1):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    capabilities, direct_capabilities, aggregate_capabilities = _parse_capabilities(
        fields[11]
    )
    validity = fields[1].casefold()
    return {
        "algorithm": algorithm,
        "bits": bits,
        "capabilities": capabilities,
        "direct_capabilities": direct_capabilities,
        "aggregate_capabilities": aggregate_capabilities,
        "created": created,
        "expires": expires,
        "is_primary": is_primary,
        "revoked": validity == "r",
        "expired": validity == "e" or (expires is not None and expires <= now),
        "disabled": validity == "d",
        "invalid": validity == "i",
    }


def _record_from_fields(fields: _ParsedFields, fingerprint: str) -> KeyRecord:
    if fields["invalid"]:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    created = _timestamp_date(fields["created"])
    expires_epoch = fields["expires"]
    expires_on = None if expires_epoch is None else _timestamp_date(expires_epoch)
    return KeyRecord(
        fingerprint=fingerprint,
        algorithm=fields["algorithm"],
        bits=fields["bits"],
        capabilities=fields["capabilities"],
        direct_capabilities=fields["direct_capabilities"],
        aggregate_capabilities=fields["aggregate_capabilities"],
        created_on=created,
        expires_on=expires_on,
        is_primary=bool(fields["is_primary"]),
        expires_epoch=expires_epoch,
        revoked=bool(fields["revoked"]),
        expired=bool(fields["expired"]),
        disabled=bool(fields["disabled"]),
    )


def _timestamp_date(value: object) -> date:
    if type(value) is not int:
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY)
    try:
        return datetime.fromtimestamp(value, timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        raise KeyInspectionError(KeyInspectionErrorCode.INVALID_KEY) from None


def _parse_capabilities(
    value: str,
) -> tuple[
    frozenset[KeyCapability],
    frozenset[KeyCapability],
    frozenset[KeyCapability],
]:
    direct: set[KeyCapability] = set()
    aggregate: set[KeyCapability] = set()
    for character in value:
        capability = _CAPABILITIES.get(character.casefold())
        if (
            capability is None
            or not character.isascii()
            or not (character.islower() or character.isupper())
        ):
            raise KeyInspectionError(KeyInspectionErrorCode.INVALID_CAPABILITIES)
        (direct if character.islower() else aggregate).add(capability)
    direct_capabilities = frozenset(direct)
    aggregate_capabilities = frozenset(aggregate)
    return (
        direct_capabilities | aggregate_capabilities,
        direct_capabilities,
        aggregate_capabilities,
    )
