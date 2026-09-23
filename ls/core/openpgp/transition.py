"""Authenticated routine OpenPGP owner transitions.

A candidate owner signs the canonical transition record. The verified envelope
delivers that proposal only to the current owner, who signs the same record
and separately signs the exact candidate-signed proposal as approval.
Consumers verify all proofs without changing local trust or activating the
proposed authority.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Final, Iterable, Iterator, Mapping
from urllib.parse import unquote_to_bytes

from . import envelope as _envelope
from . import keys as _keys
from . import recovery as _recovery
from .contracts import (
    KeyCapability,
    LocalTrust,
    OpenPGPContractError,
    RSA_4096_TWO_YEAR_PROFILE,
    normalize_fingerprint,
    validate_key_profile,
)
from .envelope import EnvelopeError, EnvelopeErrorCode, seal_envelope
from .keys import KeyInspection, KeyInspectionError, KeyRecord, inspect_key
from .opening import EnvelopeOpenError, open_envelope
from .secrets import SecretReference, SecretResolver

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


@contextmanager
def _managed_transition_agent(
    gnupg_home: str | os.PathLike[str],
) -> Iterator[Path]:
    """Start a selected-home agent while preserving existing agent state.

    Transition crypto uses the envelope runner's ``--no-autostart`` policy.
    Explicitly launch only the caller-selected home so freshly generated keys
    can be used without enabling ambient GnuPG agent discovery.
    """
    home = _envelope._validated_gnupg_home(gnupg_home)
    try:
        with _recovery._managed_home_agents((home,)):
            if not _recovery._home_agent_is_running(home):
                gpgconf = shutil.which("gpgconf")
                if not gpgconf:
                    raise EnvelopeError(EnvelopeErrorCode.GPG_NOT_FOUND)
                try:
                    result = subprocess.run(
                        (
                            gpgconf,
                            "--homedir",
                            os.fspath(home),
                            "--launch",
                            "gpg-agent",
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                        env=_recovery._gpg_environment(home),
                        timeout=_GPG_AGENT_START_TIMEOUT_SECONDS,
                        check=False,
                    )
                except (OSError, ValueError, subprocess.SubprocessError):
                    raise EnvelopeError(
                        EnvelopeErrorCode.SIGN_ENCRYPT_FAILED
                    ) from None
                if result.returncode != 0 or not _recovery._home_agent_is_running(home):
                    raise EnvelopeError(EnvelopeErrorCode.SIGN_ENCRYPT_FAILED)
            yield home
    except _recovery.RecoveryError:
        raise EnvelopeError(EnvelopeErrorCode.SIGN_ENCRYPT_FAILED) from None


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


def _inspect_owner_certificate(
    certificate: bytes, gpg_binary: str
) -> tuple[KeyInspection, dict[str, object]]:
    if type(certificate) is not bytes or not (
        1 <= len(certificate) <= _MAX_CERTIFICATE_BYTES
    ):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    try:
        inspection = inspect_key(certificate=certificate, gpg_binary=gpg_binary)
        identities = _read_identities(
            certificate, inspection.primary_fingerprint, gpg_binary
        )
    except (KeyInspectionError, OpenPGPContractError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    return inspection, _inspection_facts(inspection, identities)


def _read_identities(
    certificate: bytes, fingerprint: str, executable: str
) -> list[dict[str, str]]:
    try:
        with tempfile.TemporaryDirectory(prefix="localsetup-transition-") as path:
            home = Path(path)
            os.chmod(home, 0o700)
            certificate_path = home / "owner.asc"
            _keys._write_private_file(certificate_path, certificate)
            _keys._run_gpg(executable, home, ("--import", os.fspath(certificate_path)))
            output = _keys._run_gpg(
                executable,
                home,
                (
                    "--with-colons",
                    "--fixed-list-mode",
                    "--list-keys",
                    fingerprint,
                ),
            )
    except (KeyInspectionError, OSError, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    identities: list[dict[str, str]] = []
    primary_count = 0
    in_primary = False
    total_bytes = 0
    try:
        for line in output.splitlines():
            fields = line.split(b":")
            if fields[0] == b"pub":
                primary_count += 1
                in_primary = primary_count == 1
            elif fields[0] == b"uid" and in_primary:
                if len(fields) <= 9:
                    raise ValueError
                validity = fields[1].decode("ascii")
                if validity not in {
                    "",
                    "-",
                    "o",
                    "q",
                    "n",
                    "m",
                    "f",
                    "u",
                    "w",
                    "e",
                    "r",
                    "d",
                    "i",
                }:
                    raise ValueError
                identity_bytes = unquote_to_bytes(fields[9].decode("utf-8"))
                identity = identity_bytes.decode("utf-8")
                encoded_length = len(identity_bytes)
                if (
                    not identity
                    or encoded_length > _MAX_IDENTITY_BYTES
                    or len(identities) >= _MAX_IDENTITIES
                    or "\x00" in identity
                    or unicodedata.normalize("NFC", identity) != identity
                    or any(
                        unicodedata.category(character) == "Cc"
                        for character in identity
                    )
                ):
                    raise ValueError
                total_bytes += encoded_length
                if total_bytes > _MAX_IDENTITY_TOTAL_BYTES:
                    raise ValueError
                identities.append({"text": identity, "validity": validity})
    except (UnicodeDecodeError, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    if primary_count != 1 or not identities or not any(
        identity["validity"] not in {"r", "i", "d"} for identity in identities
    ):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    identities.sort(key=lambda identity: (identity["text"], identity["validity"]))
    if len({(item["text"], item["validity"]) for item in identities}) != len(identities):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    return identities


def _inspection_facts(
    inspection: KeyInspection, identities: list[dict[str, str]]
) -> dict[str, object]:
    return {
        "fingerprint": inspection.primary_fingerprint,
        "identities": identities,
        "records": [
            {
                "fingerprint": record.fingerprint,
                "primary": record.is_primary,
                "algorithm": record.algorithm,
                "bits": record.bits,
                "capabilities": sorted(
                    capability.value for capability in record.capabilities
                ),
                "direct_capabilities": sorted(
                    capability.value for capability in record.direct_capabilities
                ),
                "aggregate_capabilities": sorted(
                    capability.value for capability in record.aggregate_capabilities
                ),
                "created_on": record.created_on.isoformat(),
                "expires_on": (
                    None if record.expires_on is None else record.expires_on.isoformat()
                ),
                "expires_epoch": record.expires_epoch,
                "revoked": record.revoked,
                "disabled": record.disabled,
            }
            for record in inspection.records
        ],
    }


def _owner_record(
    certificate: bytes, facts: dict[str, object]
) -> dict[str, object]:
    return {
        "fingerprint": facts["fingerprint"],
        "certificate": base64.b64encode(certificate).decode("ascii"),
        "inspection": facts,
    }


def _inspect_owner_from_record(
    owner: Mapping[str, object], executable: str
) -> tuple[KeyInspection, dict[str, object]]:
    _validate_record_owner_shape(owner)
    certificate = _certificate_from_owner_record(owner)
    inspection, facts = _inspect_owner_certificate(certificate, executable)
    if (
        owner.get("fingerprint") != inspection.primary_fingerprint
        or owner.get("inspection") != facts
    ):
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
    return inspection, facts


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


def _certificate_from_owner_record(owner: Mapping[str, object]) -> bytes:
    encoded = owner.get("certificate")
    if (
        not isinstance(encoded, str)
        or len(encoded) > ((_MAX_CERTIFICATE_BYTES + 2) // 3) * 4
    ):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    try:
        certificate = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE) from None
    if (
        not certificate
        or len(certificate) > _MAX_CERTIFICATE_BYTES
        or base64.b64encode(certificate).decode("ascii") != encoded
    ):
        raise TransitionError(TransitionErrorCode.INVALID_CERTIFICATE)
    return certificate


def _sign_detached(
    payload: bytes,
    *,
    fingerprint: str,
    gnupg_home: str | os.PathLike[str],
    passphrase_reference: SecretReference,
    secret_resolver: SecretResolver,
    gpg_binary: str,
) -> bytes:
    passphrase: bytearray | None = None
    secret: object | None = None
    encoded: bytes | None = None
    try:
        if (
            type(payload) is not bytes
            or len(payload) > _MAX_PACKAGE_BYTES
            or not isinstance(passphrase_reference, SecretReference)
            or not isinstance(secret_resolver, SecretResolver)
        ):
            raise TransitionError(TransitionErrorCode.INVALID_INPUT)
        home = _envelope._validated_gnupg_home(gnupg_home)
        try:
            secret = secret_resolver.resolve(passphrase_reference)
            encoded = _envelope._encode_passphrase(secret)
        except Exception:
            secret = None
            encoded = None
            raise TransitionError(TransitionErrorCode.PASSPHRASE_UNAVAILABLE) from None
        passphrase = bytearray(encoded)
        secret = None
        encoded = None
        with _managed_transition_agent(home):
            try:
                signature, _diagnostics = _envelope._run_gpg(
                    gpg_binary,
                    home,
                    (
                        "--pinentry-mode",
                        "loopback",
                        "--passphrase-fd",
                        "{passphrase_fd}",
                        "--no-armor",
                        "--local-user",
                        normalize_fingerprint(fingerprint),
                        "--output",
                        "-",
                        "--detach-sign",
                        "-",
                    ),
                    stdin_data=payload,
                    passphrase=passphrase,
                    failure_code=_envelope.EnvelopeErrorCode.SIGN_ENCRYPT_FAILED,
                )
            except EnvelopeError:
                raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE) from None
        if not signature or len(signature) > _MAX_SIGNATURE_BYTES:
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
        return signature
    except TransitionError:
        raise
    except EnvelopeError:
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    except Exception:
        secret = None
        encoded = None
        raise TransitionError(TransitionErrorCode.INVALID_INPUT) from None
    finally:
        secret = None
        encoded = None
        if passphrase is not None:
            passphrase[:] = b"\x00" * len(passphrase)
        passphrase = None


def _verify_detached_signature(
    payload: bytes,
    signature: bytes,
    *,
    certificate: bytes,
    inspection: KeyInspection,
    expected_fingerprint: str,
    valid_until: int | None = None,
    gpg_binary: str,
) -> None:
    if (
        type(payload) is not bytes
        or len(payload) > _MAX_PACKAGE_BYTES
        or type(signature) is not bytes
        or not (1 <= len(signature) <= _MAX_SIGNATURE_BYTES)
        or type(certificate) is not bytes
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    expected = normalize_fingerprint(expected_fingerprint)
    if inspection.primary_fingerprint != expected:
        raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
    if valid_until is not None and (
        type(valid_until) is not int or not (0 <= valid_until <= 2**63 - 1)
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
    try:
        with tempfile.TemporaryDirectory(prefix="localsetup-transition-verify-") as path:
            home = Path(path)
            os.chmod(home, 0o700)
            certificate_path = home / "owner.asc"
            signature_path = home / "record.sig"
            payload_path = home / "record.json"
            _keys._write_private_file(certificate_path, certificate)
            _keys._write_private_file(signature_path, signature)
            _keys._write_private_file(payload_path, payload)
            _keys._run_gpg(
                gpg_binary, home, ("--import", os.fspath(certificate_path))
            )
            status = _keys._run_gpg(
                gpg_binary,
                home,
                (
                    "--status-fd=1",
                    "--verify",
                    os.fspath(signature_path),
                    os.fspath(payload_path),
                ),
            )
    except (KeyInspectionError, OSError, ValueError):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE) from None
    valid: list[tuple[str, str]] = []
    rejected = {
        b"BADSIG",
        b"ERRSIG",
        b"EXPSIG",
        b"EXPKEYSIG",
        b"REVKEYSIG",
        b"NO_PUBKEY",
        b"NODATA",
        b"FAILURE",
    }
    for line in status.splitlines():
        if not line.startswith(_GPG_VERIFY_STATUS_PREFIX):
            continue
        fields = line[len(_GPG_VERIFY_STATUS_PREFIX) :].split()
        if not fields:
            continue
        if fields[0] in rejected:
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
        if fields[0] != b"VALIDSIG":
            continue
        if len(fields) not in {10, 11}:
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)
        try:
            signing_fingerprint = normalize_fingerprint(fields[1].decode("ascii"))
            primary_fingerprint = (
                normalize_fingerprint(fields[10].decode("ascii"))
                if len(fields) == 11
                else signing_fingerprint
            )
        except (UnicodeDecodeError, OpenPGPContractError):
            raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE) from None
        if valid_until is not None:
            _validate_signing_component(
                inspection, signing_fingerprint, valid_until=valid_until
            )
        valid.append((signing_fingerprint, primary_fingerprint))
    if (
        len(valid) != 1
        or valid[0][1] != expected
        or valid[0][0] not in inspection.signing_fingerprints
    ):
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)


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
