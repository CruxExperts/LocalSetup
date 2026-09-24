"""Validation rules, limits, and stable errors for owner transitions."""

from __future__ import annotations

import base64
import binascii
import os
import re
import time
import unicodedata
import uuid
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Final, Iterable, Mapping

from .contracts import (
    KeyCapability,
    LocalTrust,
    OpenPGPContractError,
    RSA_4096_TWO_YEAR_PROFILE,
    normalize_fingerprint,
    validate_key_profile,
)
from .keys import KeyInspection, KeyRecord

_RECORD_FORMAT = "localsetup.openpgp-owner-transition"
_RECORD_SCHEMA_VERSION = 1
_PROPOSAL_FORMAT = "localsetup.openpgp-owner-transition-proposal"
_APPROVAL_FORMAT = "localsetup.openpgp-owner-transition-approval"
_MAX_CERTIFICATE_BYTES = 1_048_576
_MAX_RECORD_BYTES = 4_194_304
_MAX_PACKAGE_BYTES = 5_242_880
_MAX_SIGNATURE_BYTES = 65_536
_MAX_IDENTITIES = 32
_MAX_IDENTITY_BYTES = 1024
_MAX_IDENTITY_TOTAL_BYTES = 8192
_MAX_SCOPE_ITEMS = 64
_MAX_TEXT_BYTES = 4096
_MAX_REPLAY_ITEMS = 100_000
_MAX_OVERLAP_SECONDS = 90 * 24 * 60 * 60
_MAX_SCHEDULE_AHEAD_SECONDS = 365 * 24 * 60 * 60
_GPG_AGENT_START_TIMEOUT_SECONDS: Final = 15.0
_GPG_VERIFY_STATUS_PREFIX = b"[GNUPG:] "
_TRANSITION_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "transition_id",
        "nonce",
        "old_owner",
        "proposed_owner",
        "effective_at",
        "overlap_seconds",
        "scope",
        "predecessor_fingerprint",
        "epoch",
        "reason",
        "revocation_instructions",
        "rollback_instructions",
    }
)


class TransitionErrorCode(StrEnum):
    """Stable transition failures; errors never include caller or GnuPG data."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_CERTIFICATE = "INVALID_CERTIFICATE"
    UNTRUSTED_OLD_OWNER = "UNTRUSTED_OLD_OWNER"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    INVALID_AUTHORIZATION = "INVALID_AUTHORIZATION"
    RECORD_MISMATCH = "RECORD_MISMATCH"
    REPLAY = "REPLAY"
    UNSAFE_TIME = "UNSAFE_TIME"
    UNSAFE_CAPABILITY = "UNSAFE_CAPABILITY"
    UNSAFE_EXPIRY = "UNSAFE_EXPIRY"
    PASSPHRASE_UNAVAILABLE = "PASSPHRASE_UNAVAILABLE"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    APPROVAL_FAILED = "APPROVAL_FAILED"


class TransitionError(RuntimeError):
    """A bounded, non-secret transition failure with a stable code."""

    def __init__(self, code: TransitionErrorCode) -> None:
        self.code = TransitionErrorCode(code)
        super().__init__(self.code.value)

def _validate_record_shape(record: Mapping[str, object]) -> None:
    if (
        not isinstance(record, dict)
        or set(record) != _TRANSITION_FIELDS
        or record.get("format") != _RECORD_FORMAT
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != _RECORD_SCHEMA_VERSION
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    _validate_transition_id(record.get("transition_id"))
    _validate_nonce(record.get("nonce"))
    _validate_epoch(record.get("epoch"))
    _validate_text(record.get("reason"))
    _validate_text(record.get("revocation_instructions"))
    _validate_text(record.get("rollback_instructions"))
    _validate_time_fields(record.get("effective_at"), record.get("overlap_seconds"))
    predecessor = normalize_fingerprint(record.get("predecessor_fingerprint"))
    if predecessor != record.get("predecessor_fingerprint"):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    scope = _validate_scope(record.get("scope"))
    if list(scope) != record.get("scope"):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    for key in ("old_owner", "proposed_owner"):
        entry = record.get(key)
        if not isinstance(entry, dict) or set(entry) != {
            "fingerprint",
            "certificate",
            "inspection",
        }:
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        fingerprint = normalize_fingerprint(entry.get("fingerprint"))
        if fingerprint != entry.get("fingerprint"):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        if not isinstance(entry.get("certificate"), str) or not isinstance(
            entry.get("inspection"), dict
        ):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)


def _validate_owner_usable(
    inspection: KeyInspection,
    *,
    now: int,
    effective_at: int,
    overlap_seconds: int,
) -> None:
    if not isinstance(inspection, KeyInspection):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    transition_end = effective_at + overlap_seconds
    primary = inspection.primary
    if (
        not primary.is_primary
        or primary.revoked
        or primary.disabled
        or primary.created_on > _utc_date(now)
        or KeyCapability.CERTIFY not in primary.direct_capabilities
    ):
        raise TransitionError(TransitionErrorCode.UNSAFE_CAPABILITY)
    if (
        primary.expired
        or primary.expires_epoch is None
        or primary.expires_epoch <= transition_end
    ):
        raise TransitionError(TransitionErrorCode.UNSAFE_EXPIRY)
    selected_records: dict[str, KeyRecord] = {primary.fingerprint: primary}
    for capability in (KeyCapability.SIGN, KeyCapability.ENCRYPT):
        usable = tuple(
            record
            for record in inspection.records
            if capability in record.direct_capabilities
            and not record.revoked
            and not record.disabled
            and not record.expired
            and record.created_on <= _utc_date(now)
            and record.expires_epoch is not None
            and record.expires_epoch > transition_end
        )
        if not usable:
            raise TransitionError(TransitionErrorCode.UNSAFE_CAPABILITY)
        selected_records.update((record.fingerprint, record) for record in usable)
    for record in selected_records.values():
        if record.expires_on is None or record.expires_epoch is None:
            raise TransitionError(TransitionErrorCode.UNSAFE_EXPIRY)
        try:
            validate_key_profile(
                record.algorithm,
                record.bits,
                record.created_on,
                record.expires_on,
                profile=RSA_4096_TWO_YEAR_PROFILE,
            )
        except OpenPGPContractError:
            raise TransitionError(TransitionErrorCode.UNSAFE_EXPIRY) from None


def _validate_record_owner_shape(owner: Mapping[str, object]) -> None:
    if not isinstance(owner, dict) or set(owner) != {
        "fingerprint",
        "certificate",
        "inspection",
    }:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    normalize_fingerprint(owner.get("fingerprint"))
    if not isinstance(owner.get("certificate"), str) or not isinstance(
        owner.get("inspection"), dict
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)


def _validate_transition_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    canonical = str(parsed)
    if (
        canonical != value
        or parsed.variant != uuid.RFC_4122
        or parsed.version not in {4, 7}
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return canonical


def _validate_nonce(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value)
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (binascii.Error, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return value


def _validate_epoch(value: object) -> int:
    if type(value) is not int or not (1 <= value <= 2**63 - 1):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return value


def _validated_now(value: int | None) -> int:
    if value is None:
        return int(time.time())
    if type(value) is not int or not (0 <= value <= 2**63 - 1):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return value


def _validate_time_window(
    effective_at: object,
    overlap_seconds: object,
    now: int,
    *,
    allow_historical: bool = False,
    allow_overlap_verification: bool = False,
) -> tuple[int, int]:
    _validate_time_fields(effective_at, overlap_seconds)
    assert type(effective_at) is int and type(overlap_seconds) is int
    end = effective_at + overlap_seconds
    if (
        allow_overlap_verification
        and now > end
        and not allow_historical
    ):
        raise TransitionError(TransitionErrorCode.UNSAFE_TIME)
    if (
        not allow_overlap_verification
        and effective_at < now
        and not allow_historical
    ):
        raise TransitionError(TransitionErrorCode.UNSAFE_TIME)
    if effective_at > now + _MAX_SCHEDULE_AHEAD_SECONDS:
        raise TransitionError(TransitionErrorCode.UNSAFE_TIME)
    return effective_at, overlap_seconds


def _validate_time_fields(effective_at: object, overlap_seconds: object) -> None:
    if (
        type(effective_at) is not int
        or not (0 <= effective_at <= 2**63 - 1)
        or type(overlap_seconds) is not int
        or not (0 <= overlap_seconds <= _MAX_OVERLAP_SECONDS)
    ):
        raise TransitionError(TransitionErrorCode.UNSAFE_TIME)


def _transition_window_end(record_object: Mapping[str, object]) -> int:
    effective_at = record_object.get("effective_at")
    overlap_seconds = record_object.get("overlap_seconds")
    _validate_time_fields(effective_at, overlap_seconds)
    assert type(effective_at) is int and type(overlap_seconds) is int
    return effective_at + overlap_seconds


def _validate_scope(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    try:
        iterator = iter(values)  # type: ignore[arg-type]
    except TypeError:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    selected: list[str] = []
    total = 0
    try:
        for value in iterator:
            text = _validate_text(value, max_bytes=256)
            if any(character.isspace() for character in text):
                raise TransitionError(TransitionErrorCode.INVALID_INPUT)
            selected.append(text)
            total += len(text.encode("utf-8"))
            if len(selected) > _MAX_SCOPE_ITEMS or total > 2048:
                raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    except TransitionError:
        raise
    except Exception:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    if not selected or len(set(selected)) != len(selected):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return tuple(sorted(selected))


def _validate_text(value: object, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.strip()
        or unicodedata.normalize("NFC", value) != value
        or "\x00" in value
        or any(
            unicodedata.category(character) == "Cc" and character not in "\n\t"
            for character in value
        )
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    if len(encoded) > max_bytes:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return value


def _require_unseen(
    transition_id: object,
    nonce: object,
    *,
    known_transition_ids: Iterable[str],
    known_nonces: Iterable[str],
) -> None:
    selected_id = _validate_transition_id(transition_id)
    selected_nonce = _validate_nonce(nonce)
    known_ids = _bounded_history(known_transition_ids, _validate_transition_id)
    known_nonce_set = _bounded_history(known_nonces, _validate_nonce)
    if selected_id in known_ids or selected_nonce in known_nonce_set:
        raise TransitionError(TransitionErrorCode.REPLAY)


def _bounded_history(values: object, validate) -> frozenset[str]:
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    try:
        result: set[str] = set()
        for index, value in enumerate(values):  # type: ignore[union-attr]
            if index >= _MAX_REPLAY_ITEMS:
                raise TransitionError(TransitionErrorCode.INVALID_INPUT)
            result.add(validate(value))
        return frozenset(result)
    except TransitionError:
        raise
    except Exception:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None


def _require_old_owner_trust(fingerprint: str, local_trust: LocalTrust) -> None:
    try:
        local_trust.require_signer(fingerprint)
    except OpenPGPContractError:
        raise TransitionError(TransitionErrorCode.UNTRUSTED_OLD_OWNER) from None


def _validated_gpg_binary(value: str | os.PathLike[str]) -> str:
    try:
        executable = os.fspath(value)
    except (TypeError, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return executable


def _utc_date(timestamp: int) -> date:
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        raise TransitionError(TransitionErrorCode.UNSAFE_TIME) from None


def _validate_signing_component(
    inspection: KeyInspection,
    signing_fingerprint: str,
    *,
    valid_until: int,
) -> None:
    """Require GnuPG's actual signer to meet the transition key profile."""
    component = next(
        (
            record
            for record in inspection.records
            if record.fingerprint == signing_fingerprint
        ),
        None,
    )
    if (
        component is None
        or KeyCapability.SIGN not in component.direct_capabilities
        or component.revoked
        or component.disabled
        or component.expired
        or component.created_on > _utc_date(valid_until)
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    if (
        component.expires_on is None
        or component.expires_epoch is None
        or component.expires_epoch <= valid_until
    ):
        raise TransitionError(TransitionErrorCode.UNSAFE_EXPIRY)
    try:
        validate_key_profile(
            component.algorithm,
            component.bits,
            component.created_on,
            component.expires_on,
            profile=RSA_4096_TWO_YEAR_PROFILE,
        )
    except OpenPGPContractError:
        raise TransitionError(TransitionErrorCode.UNSAFE_EXPIRY) from None
