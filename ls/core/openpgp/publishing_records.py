"""Canonical publishing transition record shapes and serialization."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Mapping

from . import transition as _transition
from .contracts import normalize_fingerprint

TransitionError = _transition.TransitionError
TransitionErrorCode = _transition.TransitionErrorCode

_RECORD_FORMAT = "localsetup.openpgp-publishing-transition"
_SCHEMA_VERSION = 1
_PROPOSAL_FORMAT = "localsetup.openpgp-publishing-transition-proposal"
_PROOF_FORMAT = "localsetup.openpgp-publishing-transition-proof"
_APPROVAL_FORMAT = "localsetup.openpgp-publishing-transition-approval"
_RECORD_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "transition_id",
        "nonce",
        "current_owner",
        "old_publisher",
        "proposed_publisher",
        "effective_at",
        "overlap_seconds",
        "scope",
        "predecessor_fingerprint",
        "epoch",
        "reason",
    }
)


@dataclass(frozen=True, slots=True)
class PublishingTransitionProposal:
    """Canonical transition record signed by the proposed publisher."""

    record: bytes
    proposed_publisher_signature: bytes

    def to_bytes(self) -> bytes:
        return _encode(self, _PROPOSAL_FORMAT, ("proposed_publisher_signature",))

    @classmethod
    def from_bytes(cls, serialized: bytes) -> PublishingTransitionProposal:
        return _decode_proposal(serialized)


@dataclass(frozen=True, slots=True)
class PublishingTransitionProof:
    """Candidate proof plus old-publisher signature over that exact proof."""

    record: bytes
    proposed_publisher_signature: bytes
    old_publisher_signature: bytes

    def to_bytes(self) -> bytes:
        return _encode(
            self,
            _PROOF_FORMAT,
            ("proposed_publisher_signature", "old_publisher_signature"),
        )


@dataclass(frozen=True, slots=True)
class ApprovedPublishingTransitionRecord:
    """The owner signature binds the exact candidate and old-publisher proofs."""

    record: bytes
    proposed_publisher_signature: bytes
    old_publisher_signature: bytes
    owner_signature: bytes

    def to_bytes(self) -> bytes:
        return _encode(
            self,
            _APPROVAL_FORMAT,
            (
                "proposed_publisher_signature",
                "old_publisher_signature",
                "owner_signature",
            ),
        )

    @classmethod
    def from_bytes(cls, serialized: bytes) -> ApprovedPublishingTransitionRecord:
        return _decode_approval(serialized)


@dataclass(frozen=True, slots=True)
class VerifiedPublishingTransition:
    """Verified facts only; this result does not enroll or activate a publisher."""

    transition_id: str
    nonce: str
    current_owner_fingerprint: str
    old_publisher_fingerprint: str
    proposed_publisher_fingerprint: str
    predecessor_fingerprint: str
    epoch: int
    effective_at: int
    overlap_seconds: int
    scope: tuple[str, ...]
    current_owner_inspection: bytes
    old_publisher_inspection: bytes
    proposed_publisher_inspection: bytes
    reason: str
    approved_record: bytes


def _validate_record_shape(record: Mapping[str, object]) -> None:
    if (
        not isinstance(record, dict)
        or set(record) != _RECORD_FIELDS
        or record.get("format") != _RECORD_FORMAT
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != _SCHEMA_VERSION
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    _transition._validate_transition_id(record.get("transition_id"))
    _transition._validate_nonce(record.get("nonce"))
    _transition._validate_epoch(record.get("epoch"))
    _transition._validate_text(record.get("reason"))
    _transition._validate_time_fields(
        record.get("effective_at"), record.get("overlap_seconds")
    )
    predecessor = normalize_fingerprint(record.get("predecessor_fingerprint"))
    if predecessor != record.get("predecessor_fingerprint"):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    scope = _transition._validate_scope(record.get("scope"))
    if list(scope) != record.get("scope"):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    for role in ("current_owner", "old_publisher", "proposed_publisher"):
        entry = record.get(role)
        if not isinstance(entry, dict):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        _transition._validate_record_owner_shape(entry)
    if (
        record["old_publisher"]["fingerprint"]
        != record["predecessor_fingerprint"]
    ):
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)


def _record_object(record: bytes) -> dict[str, object]:
    if type(record) is not bytes or not (
        1 <= len(record) <= _transition._MAX_RECORD_BYTES
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    try:
        value = json.loads(
            record.decode("utf-8"),
            object_pairs_hook=_transition._unique_object,
            parse_constant=_transition._reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    if not isinstance(value, dict) or _transition._canonical_json(value) != record:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    _validate_record_shape(value)
    return value


def _encode(value: object, format_name: str, signature_fields: tuple[str, ...]) -> bytes:
    if isinstance(value, PublishingTransitionProposal):
        record = value.record
        signatures = {signature_fields[0]: value.proposed_publisher_signature}
        if not signatures[signature_fields[0]]:
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    elif isinstance(value, PublishingTransitionProof):
        record = value.record
        signatures = {
            signature_fields[0]: value.proposed_publisher_signature,
            signature_fields[1]: value.old_publisher_signature,
        }
    elif isinstance(value, ApprovedPublishingTransitionRecord):
        record = value.record
        signatures = {
            signature_fields[0]: value.proposed_publisher_signature,
            signature_fields[1]: value.old_publisher_signature,
            signature_fields[2]: value.owner_signature,
        }
    else:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    record_object = _record_object(record)
    encoded_signatures = {
        name: base64.b64encode(_transition._validated_signature(signature)).decode(
            "ascii"
        )
        for name, signature in signatures.items()
    }
    payload = _transition._canonical_json(
        {"format": format_name, "record": record_object, **encoded_signatures}
    )
    if len(payload) > _transition._MAX_PACKAGE_BYTES:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return payload


def _decode_proposal(serialized: bytes) -> PublishingTransitionProposal:
    wrapper, record = _decode_wrapper(
        serialized, _PROPOSAL_FORMAT, ("proposed_publisher_signature",)
    )
    return PublishingTransitionProposal(
        record,
        _transition._decode_signature(wrapper["proposed_publisher_signature"]),
    )


def _decode_proof(serialized: bytes) -> PublishingTransitionProof:
    wrapper, record = _decode_wrapper(
        serialized,
        _PROOF_FORMAT,
        ("proposed_publisher_signature", "old_publisher_signature"),
    )
    return PublishingTransitionProof(
        record,
        _transition._decode_signature(wrapper["proposed_publisher_signature"]),
        _transition._decode_signature(wrapper["old_publisher_signature"]),
    )


def _decode_approval(serialized: bytes) -> ApprovedPublishingTransitionRecord:
    wrapper, record = _decode_wrapper(
        serialized,
        _APPROVAL_FORMAT,
        (
            "proposed_publisher_signature",
            "old_publisher_signature",
            "owner_signature",
        ),
    )
    return ApprovedPublishingTransitionRecord(
        record,
        _transition._decode_signature(wrapper["proposed_publisher_signature"]),
        _transition._decode_signature(wrapper["old_publisher_signature"]),
        _transition._decode_signature(wrapper["owner_signature"]),
    )


def _decode_wrapper(
    serialized: bytes, format_name: str, signature_fields: tuple[str, ...]
) -> tuple[dict[str, object], bytes]:
    wrapper = _transition._parse_wrapper(
        serialized, format_name, set(signature_fields)
    )
    record_object = wrapper["record"]
    assert isinstance(record_object, dict)
    _validate_record_shape(record_object)
    record = _transition._canonical_json(record_object)
    if len(record) > _transition._MAX_RECORD_BYTES:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return wrapper, record
