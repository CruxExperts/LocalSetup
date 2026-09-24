"""Authenticated routine OpenPGP owner transitions.

A candidate owner signs the canonical transition record. The verified envelope
delivers that proposal only to the current owner, who signs the same record
and separately signs the exact candidate-signed proposal as approval.
Consumers verify all proofs without changing local trust or activating the
proposed authority.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping

from . import envelope as _envelope
from . import keys as _keys
from . import recovery as _recovery
from .contracts import LocalTrust, OpenPGPContractError, normalize_fingerprint
from .envelope import EnvelopeError, EnvelopeErrorCode, seal_envelope
from .keys import KeyInspection, KeyInspectionError, KeyRecord, inspect_key
from .opening import EnvelopeOpenError, open_envelope
from .secrets import SecretReference, SecretResolver
from .transition_contracts import (
    _APPROVAL_FORMAT,
    _GPG_AGENT_START_TIMEOUT_SECONDS,
    _GPG_VERIFY_STATUS_PREFIX,
    _MAX_CERTIFICATE_BYTES,
    _MAX_IDENTITY_BYTES,
    _MAX_IDENTITY_TOTAL_BYTES,
    _MAX_IDENTITIES,
    _MAX_OVERLAP_SECONDS,
    _MAX_PACKAGE_BYTES,
    _MAX_RECORD_BYTES,
    _MAX_REPLAY_ITEMS,
    _MAX_SCHEDULE_AHEAD_SECONDS,
    _MAX_SCOPE_ITEMS,
    _MAX_SIGNATURE_BYTES,
    _MAX_TEXT_BYTES,
    _PROPOSAL_FORMAT,
    _RECORD_FORMAT,
    _RECORD_SCHEMA_VERSION,
    _TRANSITION_FIELDS,
    TransitionError,
    TransitionErrorCode,
    _bounded_history,
    _require_old_owner_trust,
    _require_unseen,
    _transition_window_end,
    _utc_date,
    _validate_epoch,
    _validate_nonce,
    _validate_owner_usable,
    _validate_record_owner_shape,
    _validate_record_shape,
    _validate_scope,
    _validate_signing_component,
    _validate_text,
    _validate_time_fields,
    _validate_time_window,
    _validate_transition_id,
    _validated_gpg_binary,
    _validated_now,
)
from .transition_crypto import (
    _certificate_from_owner_record,
    _inspect_owner_certificate,
    _inspect_owner_from_record,
    _inspection_facts,
    _managed_transition_agent,
    _owner_record,
    _read_identities,
    _sign_detached,
    _verify_detached_signature,
)
from .transition_records import (
    ApprovedTransitionRecord,
    TransitionProposal,
    VerifiedTransition,
    _canonical_json,
    _decode_approval,
    _decode_proposal,
    _decode_signature,
    _encode_approval,
    _encode_proposal,
    _parse_wrapper,
    _record_object,
    _reject_json_constant,
    _unique_object,
    _validated_signature,
)

__all__ = (
    "ApprovedTransitionRecord",
    "TransitionError",
    "TransitionErrorCode",
    "TransitionProposal",
    "VerifiedTransition",
    "approve_transition_proposal",
    "create_transition_proposal",
    "encrypt_transition_proposal",
    "verify_approved_transition",
)

def create_transition_proposal(
    *,
    old_owner_certificate: bytes,
    proposed_owner_certificate: bytes,
    old_owner_fingerprint: str,
    transition_id: str,
    nonce: str,
    effective_at: int,
    overlap_seconds: int,
    scope: Iterable[str],
    predecessor_fingerprint: str,
    epoch: int,
    reason: str,
    revocation_instructions: str,
    rollback_instructions: str,
    proposed_owner_home: str | os.PathLike[str],
    local_trust: LocalTrust,
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    known_transition_ids: Iterable[str],
    known_nonces: Iterable[str],
    now: int | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> TransitionProposal:
    """Inspect both certificates and obtain the proposed owner's proof.

    ``effective_at`` is a UTC Unix timestamp in seconds; ``overlap_seconds``
    is bounded to 90 days and the schedule to one year ahead. ``nonce`` must
    be a fresh 32-byte base64url token and ``transition_id`` a UUIDv4/v7.
    Callers must provide their persisted replay history, including empty sets
    for first use. This function does not enroll either certificate.
    """
    secret: object | None = None
    try:
        owner_fingerprint = normalize_fingerprint(old_owner_fingerprint)
        predecessor = normalize_fingerprint(predecessor_fingerprint)
        if predecessor != owner_fingerprint:
            raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
        if not isinstance(local_trust, LocalTrust):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        _require_old_owner_trust(owner_fingerprint, local_trust)
        timestamp = _validated_now(now)
        selected_id = _validate_transition_id(transition_id)
        selected_nonce = _validate_nonce(nonce)
        selected_scope = _validate_scope(scope)
        selected_reason = _validate_text(reason)
        selected_revocation = _validate_text(revocation_instructions)
        selected_rollback = _validate_text(rollback_instructions)
        selected_epoch = _validate_epoch(epoch)
        selected_effective, selected_overlap = _validate_time_window(
            effective_at, overlap_seconds, timestamp
        )
        _require_unseen(
            selected_id,
            selected_nonce,
            known_transition_ids=known_transition_ids,
            known_nonces=known_nonces,
        )
        executable = _validated_gpg_binary(gpg_binary)
        old_inspection, old_facts = _inspect_owner_certificate(
            old_owner_certificate, executable
        )
        proposed_inspection, proposed_facts = _inspect_owner_certificate(
            proposed_owner_certificate, executable
        )
        if old_inspection.primary_fingerprint != owner_fingerprint:
            raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
        if proposed_inspection.primary_fingerprint == owner_fingerprint:
            raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
        _validate_owner_usable(
            old_inspection,
            now=timestamp,
            effective_at=selected_effective,
            overlap_seconds=selected_overlap,
        )
        _validate_owner_usable(
            proposed_inspection,
            now=timestamp,
            effective_at=selected_effective,
            overlap_seconds=selected_overlap,
        )
        record_object = {
            "format": _RECORD_FORMAT,
            "schema_version": _RECORD_SCHEMA_VERSION,
            "transition_id": selected_id,
            "nonce": selected_nonce,
            "old_owner": _owner_record(old_owner_certificate, old_facts),
            "proposed_owner": _owner_record(
                proposed_owner_certificate, proposed_facts
            ),
            "effective_at": selected_effective,
            "overlap_seconds": selected_overlap,
            "scope": list(selected_scope),
            "predecessor_fingerprint": predecessor,
            "epoch": selected_epoch,
            "reason": selected_reason,
            "revocation_instructions": selected_revocation,
            "rollback_instructions": selected_rollback,
        }
        record = _canonical_json(record_object)
        if len(record) > _MAX_RECORD_BYTES:
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        signature = _sign_detached(
            record,
            fingerprint=proposed_inspection.primary_fingerprint,
            gnupg_home=proposed_owner_home,
            passphrase_reference=passphrase_reference,
            secret_resolver=secret_resolver,
            gpg_binary=executable,
        )
        _verify_detached_signature(
            record,
            signature,
            certificate=proposed_owner_certificate,
            inspection=proposed_inspection,
            expected_fingerprint=proposed_inspection.primary_fingerprint,
            valid_until=selected_effective + selected_overlap,
            gpg_binary=executable,
        )
        return TransitionProposal(record, signature)
    except TransitionError:
        raise
    except OpenPGPContractError as exc:
        if exc.code.value == "UNTRUSTED_SIGNER":
            code = TransitionErrorCode.UNTRUSTED_OLD_OWNER
        else:
            code = TransitionErrorCode.INVALID_INPUT
        raise TransitionError(code) from None
    except KeyInspectionError:
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    except Exception:
        # Drop references in this frame before returning a safe error.
        secret = None
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    finally:
        secret = None


def encrypt_transition_proposal(
    proposal: TransitionProposal | bytes,
    *,
    old_owner_fingerprint: str,
    gnupg_home: str | os.PathLike[str],
    local_trust: LocalTrust,
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    now: int | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> bytes:
    """Seal a candidate-signed proposal to exactly the old owner.

    The verified-envelope implementation signs the encrypted package with the
    old owner's enrolled key and uses one explicit recipient: that same full
    fingerprint.  No candidate-only package is consumer authorization.
    """
    try:
        owner_fingerprint = normalize_fingerprint(old_owner_fingerprint)
        if not isinstance(local_trust, LocalTrust):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        _require_old_owner_trust(owner_fingerprint, local_trust)
        selected = (
            TransitionProposal.from_bytes(proposal)
            if type(proposal) is bytes
            else proposal
        )
        if not isinstance(selected, TransitionProposal):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        timestamp = _validated_now(now)
        executable = _validated_gpg_binary(gpg_binary)
        record_object = _record_object(selected.record)
        old_entry = record_object["old_owner"]
        if old_entry["fingerprint"] != owner_fingerprint:
            raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
        _validate_proposal_record(
            record_object,
            selected.proposed_signature,
            now=timestamp,
            gpg_binary=executable,
            known_transition_ids=(),
            known_nonces=(),
        )
        payload = selected.to_bytes()
        with _managed_transition_agent(gnupg_home) as home:
            return seal_envelope(
                payload,
                gnupg_home=home,
                signer_fingerprint=owner_fingerprint,
                recipient_fingerprints=(owner_fingerprint,),
                local_trust=local_trust,
                passphrase_reference=passphrase_reference,
                secret_resolver=secret_resolver,
            )
    except TransitionError:
        raise
    except (OpenPGPContractError, EnvelopeError, OSError, ValueError, TypeError):
        raise TransitionError(TransitionErrorCode.DELIVERY_FAILED) from None
    except Exception:
        raise TransitionError(TransitionErrorCode.DELIVERY_FAILED) from None


def approve_transition_proposal(
    encrypted_proposal: bytes,
    *,
    old_owner_fingerprint: str,
    gnupg_home: str | os.PathLike[str],
    local_trust: LocalTrust,
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    known_transition_ids: Iterable[str],
    known_nonces: Iterable[str],
    now: int | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> ApprovedTransitionRecord:
    """Open an old-owner-only package and explicitly add old-owner approval."""
    secret: object | None = None
    try:
        owner_fingerprint = normalize_fingerprint(old_owner_fingerprint)
        if not isinstance(local_trust, LocalTrust):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        _require_old_owner_trust(owner_fingerprint, local_trust)
        timestamp = _validated_now(now)
        executable = _validated_gpg_binary(gpg_binary)
        from .contracts import EnvelopePolicy

        with _managed_transition_agent(gnupg_home) as home:
            payload = open_envelope(
                encrypted_proposal,
                gnupg_home=home,
                policy=EnvelopePolicy(
                    expected_signers=(owner_fingerprint,),
                    expected_recipients=(owner_fingerprint,),
                    owner_fingerprint=owner_fingerprint,
                ),
                local_trust=local_trust,
                passphrase_reference=passphrase_reference,
                secret_resolver=secret_resolver,
            )
        proposal = TransitionProposal.from_bytes(payload)
        record_object = _record_object(proposal.record)
        valid_until = _transition_window_end(record_object)
        if record_object["old_owner"]["fingerprint"] != owner_fingerprint:
            raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
        old_inspection, _proposed_inspection, _old_facts, _proposed_facts = (
            _validate_proposal_record(
                record_object,
                proposal.proposed_signature,
                now=timestamp,
                gpg_binary=executable,
                known_transition_ids=known_transition_ids,
                known_nonces=known_nonces,
            )
        )
        owner_signature = _sign_detached(
            proposal.record,
            fingerprint=owner_fingerprint,
            gnupg_home=gnupg_home,
            passphrase_reference=passphrase_reference,
            secret_resolver=secret_resolver,
            gpg_binary=executable,
        )
        old_certificate = _certificate_from_owner_record(record_object["old_owner"])
        _verify_detached_signature(
            proposal.record,
            owner_signature,
            certificate=old_certificate,
            inspection=old_inspection,
            expected_fingerprint=owner_fingerprint,
            valid_until=valid_until,
            gpg_binary=executable,
        )
        approval_payload = proposal.to_bytes()
        approval_signature = _sign_detached(
            approval_payload,
            fingerprint=owner_fingerprint,
            gnupg_home=gnupg_home,
            passphrase_reference=passphrase_reference,
            secret_resolver=secret_resolver,
            gpg_binary=executable,
        )
        _verify_detached_signature(
            approval_payload,
            approval_signature,
            certificate=old_certificate,
            inspection=old_inspection,
            expected_fingerprint=owner_fingerprint,
            valid_until=valid_until,
            gpg_binary=executable,
        )
        return ApprovedTransitionRecord(
            proposal.record,
            proposal.proposed_signature,
            owner_signature,
            approval_signature,
        )
    except TransitionError:
        raise
    except EnvelopeOpenError:
        secret = None
        raise TransitionError(TransitionErrorCode.APPROVAL_FAILED) from None
    except OpenPGPContractError as exc:
        code = (
            TransitionErrorCode.UNTRUSTED_OLD_OWNER
            if exc.code.value == "UNTRUSTED_SIGNER"
            else TransitionErrorCode.INVALID_INPUT
        )
        raise TransitionError(code) from None
    except KeyInspectionError:
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    except Exception:
        secret = None
        raise TransitionError(TransitionErrorCode.APPROVAL_FAILED) from None
    finally:
        secret = None


def verify_approved_transition(
    approved: ApprovedTransitionRecord | bytes,
    *,
    local_trust: LocalTrust,
    known_transition_ids: Iterable[str],
    known_nonces: Iterable[str],
    current_owner_fingerprint: str | None = None,
    current_epoch: int | None = None,
    allow_historical: bool = False,
    now: int | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> VerifiedTransition:
    """Authenticate both owners' record proofs and the old-owner approval.

    Optional chain inputs are validation-only. The returned object never
    changes ``local_trust`` or activates the proposed key. Replay prevention
    is caller state: both accepted-ID and nonce histories are required.
    """
    try:
        selected = (
            ApprovedTransitionRecord.from_bytes(approved)
            if type(approved) is bytes
            else approved
        )
        if not isinstance(selected, ApprovedTransitionRecord):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        if not selected.old_owner_signature or not selected.approval_signature:
            raise TransitionError(TransitionErrorCode.INVALID_AUTHORIZATION)
        if not isinstance(local_trust, LocalTrust):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        if type(allow_historical) is not bool:
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        timestamp = _validated_now(now)
        executable = _validated_gpg_binary(gpg_binary)
        record_object = _record_object(selected.record)
        old_entry = record_object["old_owner"]
        proposed_entry = record_object["proposed_owner"]
        valid_until = _transition_window_end(record_object)
        old_fingerprint = old_entry["fingerprint"]
        proposed_fingerprint = proposed_entry["fingerprint"]
        _require_old_owner_trust(old_fingerprint, local_trust)
        if (current_owner_fingerprint is None) != (current_epoch is None):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        if current_owner_fingerprint is not None:
            current_owner = normalize_fingerprint(current_owner_fingerprint)
            selected_epoch = _validate_epoch(current_epoch)
            if (
                current_owner != old_fingerprint
                or record_object["predecessor_fingerprint"] != current_owner
                or record_object["epoch"] != selected_epoch + 1
            ):
                raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
        old_inspection, proposed_inspection, old_facts, proposed_facts = (
            _validate_proposal_record(
                record_object,
                selected.proposed_signature,
                now=timestamp,
                gpg_binary=executable,
                known_transition_ids=known_transition_ids,
                known_nonces=known_nonces,
                allow_historical=allow_historical,
                verification_window=True,
            )
        )
        _verify_detached_signature(
            selected.record,
            selected.old_owner_signature,
            certificate=_certificate_from_owner_record(old_entry),
            inspection=old_inspection,
            expected_fingerprint=old_fingerprint,
            valid_until=valid_until,
            gpg_binary=executable,
        )
        _verify_detached_signature(
            TransitionProposal(
                selected.record, selected.proposed_signature
            ).to_bytes(),
            selected.approval_signature,
            certificate=_certificate_from_owner_record(old_entry),
            inspection=old_inspection,
            expected_fingerprint=old_fingerprint,
            valid_until=valid_until,
            gpg_binary=executable,
        )
        scope = tuple(record_object["scope"])
        return VerifiedTransition(
            transition_id=record_object["transition_id"],
            nonce=record_object["nonce"],
            old_owner_fingerprint=old_fingerprint,
            proposed_owner_fingerprint=proposed_fingerprint,
            predecessor_fingerprint=record_object["predecessor_fingerprint"],
            epoch=record_object["epoch"],
            effective_at=record_object["effective_at"],
            overlap_seconds=record_object["overlap_seconds"],
            scope=scope,
            old_owner_identities=tuple(
                identity["text"] for identity in old_facts["identities"]
            ),
            proposed_owner_identities=tuple(
                identity["text"] for identity in proposed_facts["identities"]
            ),
            proposed_capabilities=proposed_inspection.capabilities,
            reason=record_object["reason"],
            revocation_instructions=record_object["revocation_instructions"],
            rollback_instructions=record_object["rollback_instructions"],
            approved_record=selected.to_bytes(),
        )
    except TransitionError:
        raise
    except (KeyInspectionError, OpenPGPContractError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    except Exception:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None


def _validate_proposal_record(
    record_object: Mapping[str, object],
    proposed_signature: bytes,
    *,
    now: int,
    gpg_binary: str,
    known_transition_ids: Iterable[str],
    known_nonces: Iterable[str],
    allow_historical: bool = False,
    verification_window: bool = False,
) -> tuple[KeyInspection, KeyInspection, dict[str, object], dict[str, object]]:
    _validate_record_shape(record_object)
    if type(proposed_signature) is not bytes or not (
        1 <= len(proposed_signature) <= _MAX_SIGNATURE_BYTES
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    old_entry = record_object["old_owner"]
    proposed_entry = record_object["proposed_owner"]
    assert isinstance(old_entry, dict) and isinstance(proposed_entry, dict)
    old_inspection, old_facts = _inspect_owner_from_record(old_entry, gpg_binary)
    proposed_inspection, proposed_facts = _inspect_owner_from_record(
        proposed_entry, gpg_binary
    )
    old_fingerprint = old_entry["fingerprint"]
    proposed_fingerprint = proposed_entry["fingerprint"]
    if (
        old_inspection.primary_fingerprint != old_fingerprint
        or proposed_inspection.primary_fingerprint != proposed_fingerprint
        or old_fingerprint == proposed_fingerprint
        or record_object["predecessor_fingerprint"] != old_fingerprint
    ):
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
    effective_at, overlap = _validate_time_window(
        record_object["effective_at"], record_object["overlap_seconds"], now,
        allow_historical=allow_historical,
        allow_overlap_verification=verification_window,
    )
    _validate_owner_usable(
        old_inspection, now=now, effective_at=effective_at, overlap_seconds=overlap
    )
    _validate_owner_usable(
        proposed_inspection,
        now=now,
        effective_at=effective_at,
        overlap_seconds=overlap,
    )
    _require_unseen(
        record_object["transition_id"],
        record_object["nonce"],
        known_transition_ids=known_transition_ids,
        known_nonces=known_nonces,
    )
    _verify_detached_signature(
        _canonical_json(dict(record_object)),
        proposed_signature,
        certificate=_certificate_from_owner_record(proposed_entry),
        inspection=proposed_inspection,
        expected_fingerprint=proposed_fingerprint,
        valid_until=effective_at + overlap,
        gpg_binary=gpg_binary,
    )
    return old_inspection, proposed_inspection, old_facts, proposed_facts
