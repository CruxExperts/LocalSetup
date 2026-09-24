"""Authenticated publishing-key transitions under the current owner."""

from __future__ import annotations

import os
from typing import Iterable, Mapping

from . import transition as _transition
from .contracts import (
    KeyCapability,
    LocalTrust,
    OpenPGPContractError,
    RSA_4096_TWO_YEAR_PROFILE,
    normalize_fingerprint,
    validate_key_profile,
)
from .keys import KeyInspection, KeyInspectionError
from .secrets import SecretReference, SecretResolver
from .publishing_records import (
    ApprovedPublishingTransitionRecord, PublishingTransitionProof,
    PublishingTransitionProposal, VerifiedPublishingTransition,
    _RECORD_FORMAT, _SCHEMA_VERSION, _PROPOSAL_FORMAT,
    _PROOF_FORMAT, _APPROVAL_FORMAT, _record_object,
    _validate_record_shape, _decode_proof,
)

TransitionError = _transition.TransitionError
TransitionErrorCode = _transition.TransitionErrorCode

__all__ = (
    "ApprovedPublishingTransitionRecord",
    "PublishingTransitionProof",
    "PublishingTransitionProposal",
    "VerifiedPublishingTransition",
    "approve_publishing_transition",
    "create_publishing_transition_proposal",
    "sign_publishing_transition_proposal",
    "verify_approved_publishing_transition",
)

def create_publishing_transition_proposal(
    *,
    current_owner_certificate: bytes,
    old_publisher_certificate: bytes,
    proposed_publisher_certificate: bytes,
    current_owner_fingerprint: str,
    current_publisher_fingerprint: str,
    transition_id: str,
    nonce: str,
    effective_at: int,
    overlap_seconds: int,
    scope: Iterable[str],
    epoch: int,
    reason: str,
    proposed_publisher_home: str | os.PathLike[str],
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    now: int | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> PublishingTransitionProposal:
    """Create the record and prove possession with the proposed publisher key."""
    timestamp = _transition._validated_now(now)
    owner_fingerprint = normalize_fingerprint(current_owner_fingerprint)
    old_publisher = normalize_fingerprint(current_publisher_fingerprint)
    selected_scope = _transition._validate_scope(scope)
    selected_id = _transition._validate_transition_id(transition_id)
    selected_nonce = _transition._validate_nonce(nonce)
    selected_epoch = _transition._validate_epoch(epoch)
    selected_reason = _transition._validate_text(reason)
    effective, overlap = _transition._validate_time_window(
        effective_at, overlap_seconds, timestamp
    )
    executable = _transition._validated_gpg_binary(gpg_binary)
    owner_inspection, owner_facts = _transition._inspect_owner_certificate(
        current_owner_certificate, executable
    )
    old_inspection, old_facts = _transition._inspect_owner_certificate(
        old_publisher_certificate, executable
    )
    proposed_inspection, proposed_facts = _transition._inspect_owner_certificate(
        proposed_publisher_certificate, executable
    )
    if (
        owner_inspection.primary_fingerprint != owner_fingerprint
        or old_inspection.primary_fingerprint != old_publisher
        or old_publisher == proposed_inspection.primary_fingerprint
    ):
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
    valid_until = effective + overlap
    _validate_party(owner_inspection, now=timestamp, valid_until=valid_until, owner=True)
    _validate_party(old_inspection, now=timestamp, valid_until=valid_until)
    _validate_party(proposed_inspection, now=timestamp, valid_until=valid_until)
    record_object = {
        "format": _RECORD_FORMAT,
        "schema_version": _SCHEMA_VERSION,
        "transition_id": selected_id,
        "nonce": selected_nonce,
        "current_owner": _transition._owner_record(
            current_owner_certificate, owner_facts
        ),
        "old_publisher": _transition._owner_record(
            old_publisher_certificate, old_facts
        ),
        "proposed_publisher": _transition._owner_record(
            proposed_publisher_certificate, proposed_facts
        ),
        "effective_at": effective,
        "overlap_seconds": overlap,
        "scope": list(selected_scope),
        "predecessor_fingerprint": old_publisher,
        "epoch": selected_epoch,
        "reason": selected_reason,
    }
    record = _transition._canonical_json(record_object)
    if len(record) > _transition._MAX_RECORD_BYTES:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    signature = _transition._sign_detached(
        record,
        fingerprint=proposed_inspection.primary_fingerprint,
        gnupg_home=proposed_publisher_home,
        passphrase_reference=passphrase_reference,
        secret_resolver=secret_resolver,
        gpg_binary=executable,
    )
    _transition._verify_detached_signature(
        record,
        signature,
        certificate=proposed_publisher_certificate,
        inspection=proposed_inspection,
        expected_fingerprint=proposed_inspection.primary_fingerprint,
        valid_until=valid_until,
        gpg_binary=executable,
    )
    return PublishingTransitionProposal(record, signature)


def sign_publishing_transition_proposal(
    proposal: PublishingTransitionProposal | bytes,
    *,
    old_publisher_fingerprint: str,
    old_publisher_home: str | os.PathLike[str],
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    now: int | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> PublishingTransitionProof:
    """Add the old publisher's proof over the candidate-signed package."""
    selected = (
        PublishingTransitionProposal.from_bytes(proposal)
        if type(proposal) is bytes
        else proposal
    )
    if not isinstance(selected, PublishingTransitionProposal):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    record_object, inspections, valid_until = _validate_proposal(
        selected, now=now, gpg_binary=_transition._validated_gpg_binary(gpg_binary)
    )
    expected = normalize_fingerprint(old_publisher_fingerprint)
    old_entry = record_object["old_publisher"]
    assert isinstance(old_entry, dict)
    if old_entry["fingerprint"] != expected:
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
    proof_payload = selected.to_bytes()
    signature = _transition._sign_detached(
        proof_payload,
        fingerprint=expected,
        gnupg_home=old_publisher_home,
        passphrase_reference=passphrase_reference,
        secret_resolver=secret_resolver,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
    )
    old_inspection = inspections["old_publisher"]
    _transition._verify_detached_signature(
        proof_payload,
        signature,
        certificate=_transition._certificate_from_owner_record(old_entry),
        inspection=old_inspection,
        expected_fingerprint=expected,
        valid_until=valid_until,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
    )
    return PublishingTransitionProof(
        selected.record, selected.proposed_publisher_signature, signature
    )


def approve_publishing_transition(
    proof: PublishingTransitionProof | bytes,
    *,
    current_owner_fingerprint: str,
    current_publisher_fingerprint: str,
    current_epoch: int,
    expected_scope: Iterable[str],
    local_trust: LocalTrust,
    known_transition_ids: Iterable[str],
    known_nonces: Iterable[str],
    gnupg_home: str | os.PathLike[str],
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    now: int,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> ApprovedPublishingTransitionRecord:
    """Have the current owner approve the exact candidate and publisher proof."""
    selected = (
        _decode_proof(proof)
        if type(proof) is bytes
        else proof
    )
    if not isinstance(selected, PublishingTransitionProof):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    record_object, inspections, valid_until = _validate_proof(
        selected,
        now=now,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
    )
    owner_fingerprint, publisher_fingerprint = _require_chain(
        record_object,
        current_owner_fingerprint=current_owner_fingerprint,
        current_publisher_fingerprint=current_publisher_fingerprint,
        current_epoch=current_epoch,
        local_trust=local_trust,
    )
    _require_scope(record_object, expected_scope)
    _transition._require_unseen(
        record_object["transition_id"],
        record_object["nonce"],
        known_transition_ids=known_transition_ids,
        known_nonces=known_nonces,
    )
    payload = selected.to_bytes()
    signature = _transition._sign_detached(
        payload,
        fingerprint=owner_fingerprint,
        gnupg_home=gnupg_home,
        passphrase_reference=passphrase_reference,
        secret_resolver=secret_resolver,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
    )
    owner_entry = record_object["current_owner"]
    assert isinstance(owner_entry, dict)
    _transition._verify_detached_signature(
        payload,
        signature,
        certificate=_transition._certificate_from_owner_record(owner_entry),
        inspection=inspections["current_owner"],
        expected_fingerprint=owner_fingerprint,
        valid_until=valid_until,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
    )
    return ApprovedPublishingTransitionRecord(
        selected.record,
        selected.proposed_publisher_signature,
        selected.old_publisher_signature,
        signature,
    )


def verify_approved_publishing_transition(
    approved: ApprovedPublishingTransitionRecord | bytes,
    *,
    current_owner_fingerprint: str,
    current_publisher_fingerprint: str,
    current_epoch: int,
    expected_scope: Iterable[str],
    local_trust: LocalTrust,
    known_transition_ids: Iterable[str],
    known_nonces: Iterable[str],
    now: int,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> VerifiedPublishingTransition:
    """Verify all three authorities without changing trust or activating keys."""
    selected = (
        ApprovedPublishingTransitionRecord.from_bytes(approved)
        if type(approved) is bytes
        else approved
    )
    if not isinstance(selected, ApprovedPublishingTransitionRecord):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    if not all(
        (
            selected.proposed_publisher_signature,
            selected.old_publisher_signature,
            selected.owner_signature,
        )
    ):
        raise TransitionError(TransitionErrorCode.INVALID_AUTHORIZATION)
    record_object, inspections, valid_until = _validate_approved(
        selected,
        now=now,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
        allow_overlap_verification=True,
    )
    owner_fingerprint, publisher_fingerprint = _require_chain(
        record_object,
        current_owner_fingerprint=current_owner_fingerprint,
        current_publisher_fingerprint=current_publisher_fingerprint,
        current_epoch=current_epoch,
        local_trust=local_trust,
    )
    _require_scope(record_object, expected_scope)
    _transition._require_unseen(
        record_object["transition_id"],
        record_object["nonce"],
        known_transition_ids=known_transition_ids,
        known_nonces=known_nonces,
    )
    owner_entry = record_object["current_owner"]
    old_entry = record_object["old_publisher"]
    proposed_entry = record_object["proposed_publisher"]
    assert isinstance(owner_entry, dict)
    assert isinstance(old_entry, dict)
    assert isinstance(proposed_entry, dict)
    executable = _transition._validated_gpg_binary(gpg_binary)
    _transition._verify_detached_signature(
        selected.record,
        selected.proposed_publisher_signature,
        certificate=_transition._certificate_from_owner_record(proposed_entry),
        inspection=inspections["proposed_publisher"],
        expected_fingerprint=proposed_entry["fingerprint"],
        valid_until=valid_until,
        gpg_binary=executable,
    )
    proof_payload = PublishingTransitionProposal(
        selected.record, selected.proposed_publisher_signature
    ).to_bytes()
    _transition._verify_detached_signature(
        proof_payload,
        selected.old_publisher_signature,
        certificate=_transition._certificate_from_owner_record(old_entry),
        inspection=inspections["old_publisher"],
        expected_fingerprint=publisher_fingerprint,
        valid_until=valid_until,
        gpg_binary=executable,
    )
    owner_payload = PublishingTransitionProof(
        selected.record,
        selected.proposed_publisher_signature,
        selected.old_publisher_signature,
    ).to_bytes()
    _transition._verify_detached_signature(
        owner_payload,
        selected.owner_signature,
        certificate=_transition._certificate_from_owner_record(owner_entry),
        inspection=inspections["current_owner"],
        expected_fingerprint=owner_fingerprint,
        valid_until=valid_until,
        gpg_binary=executable,
    )
    return VerifiedPublishingTransition(
        transition_id=record_object["transition_id"],
        nonce=record_object["nonce"],
        current_owner_fingerprint=owner_fingerprint,
        old_publisher_fingerprint=publisher_fingerprint,
        proposed_publisher_fingerprint=proposed_entry["fingerprint"],
        predecessor_fingerprint=record_object["predecessor_fingerprint"],
        epoch=record_object["epoch"],
        effective_at=record_object["effective_at"],
        overlap_seconds=record_object["overlap_seconds"],
        scope=tuple(record_object["scope"]),
        current_owner_inspection=_transition._canonical_json(
            owner_entry["inspection"]
        ),
        old_publisher_inspection=_transition._canonical_json(old_entry["inspection"]),
        proposed_publisher_inspection=_transition._canonical_json(
            proposed_entry["inspection"]
        ),
        reason=record_object["reason"],
        approved_record=selected.to_bytes(),
    )


def _validate_proposal(
    proposal: PublishingTransitionProposal,
    *,
    now: int | None,
    gpg_binary: str,
    allow_overlap_verification: bool = False,
) -> tuple[dict[str, object], dict[str, KeyInspection], int]:
    record_object = _record_object(proposal.record)
    inspections, _facts = _inspect_and_validate(
        record_object,
        now=now,
        gpg_binary=gpg_binary,
        allow_overlap_verification=allow_overlap_verification,
    )
    valid_until = _transition._transition_window_end(record_object)
    proposed_entry = record_object["proposed_publisher"]
    assert isinstance(proposed_entry, dict)
    _transition._verify_detached_signature(
        proposal.record,
        _transition._validated_signature(proposal.proposed_publisher_signature),
        certificate=_transition._certificate_from_owner_record(proposed_entry),
        inspection=inspections["proposed_publisher"],
        expected_fingerprint=proposed_entry["fingerprint"],
        valid_until=valid_until,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
    )
    return record_object, inspections, valid_until


def _validate_proof(
    proof: PublishingTransitionProof,
    *,
    now: int,
    gpg_binary: str,
    allow_overlap_verification: bool = False,
) -> tuple[dict[str, object], dict[str, KeyInspection], int]:
    record_object, inspections, valid_until = _validate_proposal(
        PublishingTransitionProposal(
            proof.record, proof.proposed_publisher_signature
        ),
        now=now,
        gpg_binary=gpg_binary,
        allow_overlap_verification=allow_overlap_verification,
    )
    old_entry = record_object["old_publisher"]
    assert isinstance(old_entry, dict)
    _transition._verify_detached_signature(
        PublishingTransitionProposal(
            proof.record, proof.proposed_publisher_signature
        ).to_bytes(),
        _transition._validated_signature(proof.old_publisher_signature),
        certificate=_transition._certificate_from_owner_record(old_entry),
        inspection=inspections["old_publisher"],
        expected_fingerprint=old_entry["fingerprint"],
        valid_until=valid_until,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
    )
    return record_object, inspections, valid_until


def _validate_approved(
    approved: ApprovedPublishingTransitionRecord,
    *,
    now: int,
    gpg_binary: str,
    allow_overlap_verification: bool = False,
) -> tuple[dict[str, object], dict[str, KeyInspection], int]:
    record_object, inspections, valid_until = _validate_proof(
        PublishingTransitionProof(
            approved.record,
            approved.proposed_publisher_signature,
            approved.old_publisher_signature,
        ),
        now=now,
        gpg_binary=gpg_binary,
        allow_overlap_verification=allow_overlap_verification,
    )
    owner_entry = record_object["current_owner"]
    assert isinstance(owner_entry, dict)
    _transition._verify_detached_signature(
        PublishingTransitionProof(
            approved.record,
            approved.proposed_publisher_signature,
            approved.old_publisher_signature,
        ).to_bytes(),
        _transition._validated_signature(approved.owner_signature),
        certificate=_transition._certificate_from_owner_record(owner_entry),
        inspection=inspections["current_owner"],
        expected_fingerprint=owner_entry["fingerprint"],
        valid_until=valid_until,
        gpg_binary=_transition._validated_gpg_binary(gpg_binary),
    )
    return record_object, inspections, valid_until


def _inspect_and_validate(
    record_object: Mapping[str, object],
    *,
    now: int | None,
    gpg_binary: str,
    allow_overlap_verification: bool,
) -> tuple[dict[str, KeyInspection], dict[str, dict[str, object]]]:
    timestamp = _transition._validated_now(now)
    _validate_record_shape(record_object)
    effective, overlap = _transition._validate_time_window(
        record_object["effective_at"],
        record_object["overlap_seconds"],
        timestamp,
        allow_overlap_verification=allow_overlap_verification,
    )
    valid_until = effective + overlap
    inspections: dict[str, KeyInspection] = {}
    facts: dict[str, dict[str, object]] = {}
    for role in ("current_owner", "old_publisher", "proposed_publisher"):
        entry = record_object[role]
        assert isinstance(entry, dict)
        inspection, owner_facts = _transition._inspect_owner_from_record(
            entry, _transition._validated_gpg_binary(gpg_binary)
        )
        if entry["fingerprint"] != inspection.primary_fingerprint:
            raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
        _validate_party(
            inspection,
            now=timestamp,
            valid_until=valid_until,
            owner=role == "current_owner",
        )
        inspections[role] = inspection
        facts[role] = owner_facts
    return inspections, facts


def _validate_party(
    inspection: KeyInspection,
    *,
    now: int,
    valid_until: int,
    owner: bool = False,
) -> None:
    primary = inspection.primary
    if (
        not primary.is_primary
        or primary.revoked
        or primary.disabled
        or primary.expired
        or primary.created_on > _transition._utc_date(now)
        or (owner and KeyCapability.CERTIFY not in primary.direct_capabilities)
    ):
        raise TransitionError(TransitionErrorCode.UNSAFE_CAPABILITY)
    if primary.expires_epoch is None or primary.expires_epoch <= valid_until:
        raise TransitionError(TransitionErrorCode.UNSAFE_EXPIRY)
    signers = tuple(
        record
        for record in inspection.records
        if KeyCapability.SIGN in record.direct_capabilities
        and not record.revoked
        and not record.disabled
        and not record.expired
        and record.created_on <= _transition._utc_date(now)
        and record.expires_epoch is not None
        and record.expires_epoch > valid_until
    )
    if not signers:
        raise TransitionError(TransitionErrorCode.UNSAFE_CAPABILITY)
    profile_records = {primary.fingerprint: primary}
    profile_records.update((record.fingerprint, record) for record in signers)
    for record in profile_records.values():
        if record.expires_on is None:
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


def _require_chain(
    record_object: Mapping[str, object],
    *,
    current_owner_fingerprint: str,
    current_publisher_fingerprint: str,
    current_epoch: int,
    local_trust: LocalTrust,
) -> tuple[str, str]:
    owner = normalize_fingerprint(current_owner_fingerprint)
    publisher = normalize_fingerprint(current_publisher_fingerprint)
    epoch = _transition._validate_epoch(current_epoch)
    if not isinstance(local_trust, LocalTrust):
        raise TransitionError(TransitionErrorCode.INVALID_INPUT)
    owner_entry = record_object["current_owner"]
    publisher_entry = record_object["old_publisher"]
    proposed_entry = record_object["proposed_publisher"]
    assert isinstance(owner_entry, dict)
    assert isinstance(publisher_entry, dict)
    assert isinstance(proposed_entry, dict)
    if (
        owner_entry["fingerprint"] != owner
        or publisher_entry["fingerprint"] != publisher
        or record_object["predecessor_fingerprint"] != publisher
        or record_object["epoch"] != epoch + 1
        or proposed_entry["fingerprint"] == publisher
    ):
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
    _transition._require_old_owner_trust(owner, local_trust)
    _transition._require_old_owner_trust(publisher, local_trust)
    return owner, publisher


def _require_scope(
    record_object: Mapping[str, object], expected_scope: Iterable[str]
) -> None:
    selected = _transition._validate_scope(expected_scope)
    if record_object.get("scope") != list(selected):
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
