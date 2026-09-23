"""Stable, crypto-free contracts for LocalSetup OpenPGP consumers.

Public envelope headers carry only format and schema. Callers must bind the
exact header bytes inside authenticated encrypted content, then independently
verify the signature and packet recipients before releasing plaintext.
"""

from __future__ import annotations

import calendar
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import json
import re
from typing import Any

__all__ = [
    "ENVELOPE_FORMAT",
    "ENVELOPE_SCHEMA_VERSION",
    "ContractErrorCode",
    "EnvelopeHeader",
    "EnvelopePolicy",
    "KeyCapability",
    "KeyProfile",
    "LocalTrust",
    "OpenPGPContractError",
    "RSA_4096_TWO_YEAR_PROFILE",
    "TrustAssessment",
    "assess_signer_trust",
    "expected_expiry_date",
    "normalize_fingerprint",
    "require_capabilities",
    "validate_envelope_header",
    "validate_envelope_participants",
    "validate_key_profile",
]

ENVELOPE_FORMAT = "localsetup.openpgp-envelope"
ENVELOPE_SCHEMA_VERSION = 1
_FINGERPRINT_RE = re.compile(r"[0-9A-Fa-f]{40}\Z")


class ContractErrorCode(StrEnum):
    """Stable, non-secret error identifiers for callers and CLI mapping."""

    INVALID_FINGERPRINT = "INVALID_FINGERPRINT"
    DUPLICATE_FINGERPRINT = "DUPLICATE_FINGERPRINT"
    EMPTY_SIGNER_SET = "EMPTY_SIGNER_SET"
    EMPTY_RECIPIENT_SET = "EMPTY_RECIPIENT_SET"
    SIGNER_SET_MISMATCH = "SIGNER_SET_MISMATCH"
    RECIPIENT_SET_MISMATCH = "RECIPIENT_SET_MISMATCH"
    INVALID_PROFILE = "INVALID_PROFILE"
    INVALID_KEY_DATES = "INVALID_KEY_DATES"
    KEY_EXPIRY_MISMATCH = "KEY_EXPIRY_MISMATCH"
    INVALID_HEADER = "INVALID_HEADER"
    UNTRUSTED_SIGNER = "UNTRUSTED_SIGNER"
    INVALID_CAPABILITY = "INVALID_CAPABILITY"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    INVALID_POLICY = "INVALID_POLICY"


class OpenPGPContractError(ValueError):
    """A safe, stable contract failure that never includes input values."""

    def __init__(self, code: ContractErrorCode) -> None:
        self.code = ContractErrorCode(code)
        super().__init__(self.code.value)


def normalize_fingerprint(value: str) -> str:
    """Return an uppercase full v4 fingerprint; reject key IDs and truncation.

    Literal spaces used to group displayed fingerprints are ignored. No other
    separators, prefixes, or shortened key identifiers are accepted.
    """
    if not isinstance(value, str):
        raise OpenPGPContractError(ContractErrorCode.INVALID_FINGERPRINT)
    ungrouped = value.strip(" ").replace(" ", "")
    if _FINGERPRINT_RE.fullmatch(ungrouped) is None:
        raise OpenPGPContractError(ContractErrorCode.INVALID_FINGERPRINT)
    return ungrouped.upper()


def _fingerprint_set(
    values: Iterable[str], *, empty_code: ContractErrorCode | None = None
) -> frozenset[str]:
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise OpenPGPContractError(ContractErrorCode.INVALID_FINGERPRINT)
    try:
        items = tuple(values)
    except TypeError:
        raise OpenPGPContractError(ContractErrorCode.INVALID_FINGERPRINT) from None
    if not items and empty_code is not None:
        raise OpenPGPContractError(empty_code)
    normalized = tuple(normalize_fingerprint(item) for item in items)
    if len(normalized) != len(set(normalized)):
        raise OpenPGPContractError(ContractErrorCode.DUPLICATE_FINGERPRINT)
    return frozenset(normalized)


def _optional_fingerprint(value: str | None) -> str | None:
    return None if value is None else normalize_fingerprint(value)


@dataclass(frozen=True)
class EnvelopePolicy:
    """Exact party allowlists; role pins never widen the signer/recipient sets."""

    expected_signers: Collection[str]
    expected_recipients: Collection[str]
    owner_fingerprint: str | None = None
    publisher_fingerprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expected_signers",
            _fingerprint_set(
                self.expected_signers, empty_code=ContractErrorCode.EMPTY_SIGNER_SET
            ),
        )
        object.__setattr__(
            self,
            "expected_recipients",
            _fingerprint_set(
                self.expected_recipients,
                empty_code=ContractErrorCode.EMPTY_RECIPIENT_SET,
            ),
        )
        object.__setattr__(
            self, "owner_fingerprint", _optional_fingerprint(self.owner_fingerprint)
        )
        object.__setattr__(
            self, "publisher_fingerprint", _optional_fingerprint(self.publisher_fingerprint)
        )


@dataclass(frozen=True)
class EnvelopeHeader:
    """The only permitted clear fields are format and schema version."""

    def to_json(self) -> str:
        """Serialize the fixed header schema in canonical, deterministic JSON."""
        return json.dumps(
            {"format": ENVELOPE_FORMAT, "schema_version": ENVELOPE_SCHEMA_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, serialized: str) -> EnvelopeHeader:
        """Reject extra clear metadata, including identities and provenance."""
        try:
            if not isinstance(serialized, str) or len(serialized) > 256:
                raise ValueError
            payload = json.loads(serialized, object_pairs_hook=_unique_object)
            if (
                type(payload) is not dict
                or set(payload) != {"format", "schema_version"}
                or payload["format"] != ENVELOPE_FORMAT
                or type(payload["schema_version"]) is not int
                or payload["schema_version"] != ENVELOPE_SCHEMA_VERSION
            ):
                raise ValueError
            return cls()
        except (TypeError, ValueError, RecursionError):
            raise OpenPGPContractError(ContractErrorCode.INVALID_HEADER) from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def validate_envelope_participants(
    actual_signers: Iterable[str],
    actual_recipients: Iterable[str],
    policy: EnvelopePolicy,
) -> None:
    """Match observed crypto signer/recipient fingerprints to policy exactly."""
    if not isinstance(policy, EnvelopePolicy):
        raise OpenPGPContractError(ContractErrorCode.INVALID_POLICY)
    signers = _fingerprint_set(actual_signers)
    recipients = _fingerprint_set(actual_recipients)
    if signers != policy.expected_signers:
        raise OpenPGPContractError(ContractErrorCode.SIGNER_SET_MISMATCH)
    if recipients != policy.expected_recipients:
        raise OpenPGPContractError(ContractErrorCode.RECIPIENT_SET_MISMATCH)


def validate_envelope_header(header: EnvelopeHeader) -> None:
    """Require a supported, format-only header; crypto checks are separate."""
    if not isinstance(header, EnvelopeHeader):
        raise OpenPGPContractError(ContractErrorCode.INVALID_HEADER)


def expected_expiry_date(created_on: date, *, years: int = 2) -> date:
    """Add calendar years, clamping leap-day to the final valid day of February."""
    if type(created_on) is not date or type(years) is not int or years < 1:
        raise OpenPGPContractError(ContractErrorCode.INVALID_KEY_DATES)
    target_year = created_on.year + years
    if target_year > date.max.year:
        raise OpenPGPContractError(ContractErrorCode.INVALID_KEY_DATES)
    target_day = min(
        created_on.day, calendar.monthrange(target_year, created_on.month)[1]
    )
    return date(target_year, created_on.month, target_day)


@dataclass(frozen=True)
class KeyProfile:
    """Algorithm, key size, and calendar validity profile for key inspection."""

    algorithm: str = "RSA"
    bits: int = 4096
    validity_years: int = 2

    def __post_init__(self) -> None:
        if (
            not isinstance(self.algorithm, str)
            or not self.algorithm.strip()
            or type(self.bits) is not int
            or self.bits < 1
            or type(self.validity_years) is not int
            or self.validity_years < 1
        ):
            raise OpenPGPContractError(ContractErrorCode.INVALID_PROFILE)
        object.__setattr__(self, "algorithm", self.algorithm.strip().upper())

    def expiry_date(self, created_on: date) -> date:
        return expected_expiry_date(created_on, years=self.validity_years)

    def validate(
        self, algorithm: str, bits: int, created_on: date, expires_on: date
    ) -> None:
        if (
            not isinstance(algorithm, str)
            or algorithm.strip().upper() != self.algorithm
            or type(bits) is not int
            or bits != self.bits
        ):
            raise OpenPGPContractError(ContractErrorCode.INVALID_PROFILE)
        if type(created_on) is not date or type(expires_on) is not date:
            raise OpenPGPContractError(ContractErrorCode.INVALID_KEY_DATES)
        if expires_on <= created_on:
            raise OpenPGPContractError(ContractErrorCode.INVALID_KEY_DATES)
        if expires_on != self.expiry_date(created_on):
            raise OpenPGPContractError(ContractErrorCode.KEY_EXPIRY_MISMATCH)


RSA_4096_TWO_YEAR_PROFILE = KeyProfile()


def validate_key_profile(
    algorithm: str,
    bits: int,
    created_on: date,
    expires_on: date,
    *,
    profile: KeyProfile = RSA_4096_TWO_YEAR_PROFILE,
) -> None:
    """Validate one key against the default or an explicitly selected profile."""
    if not isinstance(profile, KeyProfile):
        raise OpenPGPContractError(ContractErrorCode.INVALID_PROFILE)
    profile.validate(algorithm, bits, created_on, expires_on)


class KeyCapability(StrEnum):
    SIGN = "sign"
    ENCRYPT = "encrypt"
    CERTIFY = "certify"
    AUTHENTICATE = "authenticate"


def _capability_set(values: Iterable[str | KeyCapability]) -> frozenset[KeyCapability]:
    if isinstance(values, (bytes, bytearray, dict)):
        raise OpenPGPContractError(ContractErrorCode.INVALID_CAPABILITY)
    if isinstance(values, str):
        values = (values,)
    try:
        return frozenset(KeyCapability(value) for value in values)
    except (TypeError, ValueError):
        raise OpenPGPContractError(ContractErrorCode.INVALID_CAPABILITY) from None


def require_capabilities(
    actual: Iterable[str | KeyCapability],
    required: Iterable[str | KeyCapability],
) -> frozenset[KeyCapability]:
    """Normalize reported capabilities and fail if any requested use is absent."""
    actual_set = _capability_set(actual)
    required_set = _capability_set(required)
    if not required_set.issubset(actual_set):
        raise OpenPGPContractError(ContractErrorCode.MISSING_CAPABILITY)
    return actual_set


@dataclass(frozen=True)
class TrustAssessment:
    """Local trust result and a separate, non-authoritative discovery signal."""

    trusted: bool
    discovery_mismatch: bool


@dataclass(frozen=True)
class LocalTrust:
    """Authoritative local signer pins; remote discovery can only report mismatch."""

    fingerprints: Collection[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fingerprints", _fingerprint_set(self.fingerprints))

    def assess_signer(
        self, signer_fingerprint: str, *, discovered_fingerprint: str | None = None
    ) -> TrustAssessment:
        signer = normalize_fingerprint(signer_fingerprint)
        mismatch = False
        if discovered_fingerprint is not None:
            try:
                mismatch = normalize_fingerprint(discovered_fingerprint) != signer
            except OpenPGPContractError:
                mismatch = True
        return TrustAssessment(
            trusted=signer in self.fingerprints,
            discovery_mismatch=mismatch,
        )

    def require_signer(
        self, signer_fingerprint: str, *, discovered_fingerprint: str | None = None
    ) -> TrustAssessment:
        assessment = self.assess_signer(
            signer_fingerprint, discovered_fingerprint=discovered_fingerprint
        )
        if not assessment.trusted:
            raise OpenPGPContractError(ContractErrorCode.UNTRUSTED_SIGNER)
        return assessment


def assess_signer_trust(
    signer_fingerprint: str,
    local_trust: LocalTrust,
    *,
    discovered_fingerprint: str | None = None,
) -> TrustAssessment:
    """Functional entry point; discovery never adds trust or changes local pins."""
    if not isinstance(local_trust, LocalTrust):
        raise OpenPGPContractError(ContractErrorCode.INVALID_POLICY)
    return local_trust.assess_signer(
        signer_fingerprint, discovered_fingerprint=discovered_fingerprint
    )
