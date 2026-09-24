"""Canonical transition records and their exact JSON encodings."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from .contracts import KeyCapability
from .transition_contracts import (
    _APPROVAL_FORMAT,
    _MAX_PACKAGE_BYTES,
    _MAX_RECORD_BYTES,
    _MAX_SIGNATURE_BYTES,
    _PROPOSAL_FORMAT,
    TransitionError,
    TransitionErrorCode,
    _validate_record_shape,
)

@dataclass(frozen=True, slots=True)
class TransitionProposal:
    """Candidate-signed canonical record awaiting old-owner approval."""

    record: bytes
    proposed_signature: bytes

    def to_bytes(self) -> bytes:
        try:
            return _encode_proposal(self)
        except TransitionError:
            raise
        except Exception:
            raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None

    @classmethod
    def from_bytes(cls, serialized: bytes) -> TransitionProposal:
        try:
            return _decode_proposal(serialized)
        except TransitionError:
            raise
        except Exception:
            raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None


@dataclass(frozen=True, slots=True)
class ApprovedTransitionRecord:
    """A candidate proof, old-owner proof, and explicit old-owner approval."""

    record: bytes
    proposed_signature: bytes
    old_owner_signature: bytes
    approval_signature: bytes

    def to_bytes(self) -> bytes:
        try:
            return _encode_approval(self)
        except TransitionError:
            raise
        except Exception:
            raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None

    @classmethod
    def from_bytes(cls, serialized: bytes) -> ApprovedTransitionRecord:
        try:
            return _decode_approval(serialized)
        except TransitionError:
            raise
        except Exception:
            raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None


@dataclass(frozen=True, slots=True)
class VerifiedTransition:
    """Verified facts only; this result does not enroll or activate a key."""

    transition_id: str
    nonce: str
    old_owner_fingerprint: str
    proposed_owner_fingerprint: str
    predecessor_fingerprint: str
    epoch: int
    effective_at: int
    overlap_seconds: int
    scope: tuple[str, ...]
    old_owner_identities: tuple[str, ...]
    proposed_owner_identities: tuple[str, ...]
    proposed_capabilities: frozenset[KeyCapability]
    reason: str
    revocation_instructions: str
    rollback_instructions: str
    approved_record: bytes


def _encode_proposal(proposal: TransitionProposal) -> bytes:
    if not isinstance(proposal, TransitionProposal):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    record_object = _record_object(proposal.record)
    signature = _validated_signature(proposal.proposed_signature)
    encoded = _canonical_json(
        {
            "format": _PROPOSAL_FORMAT,
            "record": record_object,
            "proposed_signature": base64.b64encode(signature).decode("ascii"),
        }
    )
    if len(encoded) > _MAX_PACKAGE_BYTES:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return encoded


def _decode_proposal(serialized: bytes) -> TransitionProposal:
    wrapper = _parse_wrapper(serialized, _PROPOSAL_FORMAT, {"proposed_signature"})
    record_object = wrapper["record"]
    signature = _decode_signature(wrapper["proposed_signature"])
    record = _canonical_json(record_object)
    if len(record) > _MAX_RECORD_BYTES:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    _validate_record_shape(record_object)
    return TransitionProposal(record, signature)


def _encode_approval(approved: ApprovedTransitionRecord) -> bytes:
    if not isinstance(approved, ApprovedTransitionRecord):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    record_object = _record_object(approved.record)
    proposed_signature = _validated_signature(approved.proposed_signature)
    old_signature = _validated_signature(approved.old_owner_signature)
    approval_signature = _validated_signature(approved.approval_signature)
    encoded = _canonical_json(
        {
            "format": _APPROVAL_FORMAT,
            "record": record_object,
            "proposed_signature": base64.b64encode(proposed_signature).decode("ascii"),
            "old_owner_signature": base64.b64encode(old_signature).decode("ascii"),
            "approval_signature": base64.b64encode(approval_signature).decode("ascii"),
        }
    )
    if len(encoded) > _MAX_PACKAGE_BYTES:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return encoded


def _decode_approval(serialized: bytes) -> ApprovedTransitionRecord:
    wrapper = _parse_wrapper(
        serialized,
        _APPROVAL_FORMAT,
        {"proposed_signature", "old_owner_signature", "approval_signature"},
    )
    record_object = wrapper["record"]
    proposed_signature = _decode_signature(wrapper["proposed_signature"])
    old_signature = _decode_signature(wrapper["old_owner_signature"])
    approval_signature = _decode_signature(wrapper["approval_signature"])
    record = _canonical_json(record_object)
    if len(record) > _MAX_RECORD_BYTES:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    _validate_record_shape(record_object)
    return ApprovedTransitionRecord(
        record, proposed_signature, old_signature, approval_signature
    )


def _parse_wrapper(
    serialized: bytes, expected_format: str, signature_fields: set[str]
) -> dict[str, object]:
    if type(serialized) is not bytes or not (
        1 <= len(serialized) <= _MAX_PACKAGE_BYTES
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    try:
        wrapper = json.loads(
            serialized.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    if not isinstance(wrapper, dict) or set(wrapper) != {
        "format",
        "record",
        *signature_fields,
    }:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    if wrapper.get("format") != expected_format or not isinstance(
        wrapper.get("record"), dict
    ):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    if _canonical_json(wrapper) != serialized:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return wrapper


def _record_object(record: bytes) -> dict[str, object]:
    if type(record) is not bytes or not (1 <= len(record) <= _MAX_RECORD_BYTES):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    try:
        parsed = json.loads(
            record.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    if not isinstance(parsed, dict) or _canonical_json(parsed) != record:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    _validate_record_shape(parsed)
    return parsed


def _canonical_json(value: object) -> bytes:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    if len(serialized) > _MAX_PACKAGE_BYTES:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    return serialized


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _decode_signature(value: object) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) > ((_MAX_SIGNATURE_BYTES + 2) // 3) * 4
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    try:
        signature = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE) from None
    if base64.b64encode(signature).decode("ascii") != value:
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    return _validated_signature(signature)


def _validated_signature(signature: object) -> bytes:
    if type(signature) is not bytes or not (
        1 <= len(signature) <= _MAX_SIGNATURE_BYTES
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    return signature
