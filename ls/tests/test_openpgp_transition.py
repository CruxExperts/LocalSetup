from __future__ import annotations

import base64
import hashlib
import json
import shutil
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from ls.core.openpgp.contracts import KeyCapability, KeyProfile, LocalTrust
from ls.core.openpgp.generation import KeyIdentity, generate_key
from ls.core.openpgp.keys import KeyInspection, KeyRecord, inspect_key
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver
from ls.core.openpgp.transition import (
    ApprovedTransitionRecord,
    TransitionError,
    TransitionErrorCode,
    TransitionProposal,
    verify_approved_transition,
)
from ls.core.openpgp import recovery as recovery_api
from ls.core.openpgp import transition as transition_api


OLD = "A" * 40
PROPOSED = "B" * 40
HISTORICAL = "C" * 40
OLD_CERT = b"synthetic old public certificate"
PROPOSED_CERT = b"synthetic proposed public certificate"
NOW = int(datetime(2026, 9, 23, 12, tzinfo=timezone.utc).timestamp())
TRANSITION_ID = "123e4567-e89b-42d3-a456-426614174000"
NONCE = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


def _epoch(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


def _inspection(primary: str, subkey: str) -> KeyInspection:
    created = date(2025, 1, 1)
    expires = date(2027, 1, 1)
    return KeyInspection(
        primary=KeyRecord(
            fingerprint=primary,
            algorithm="RSA",
            bits=4096,
            capabilities=frozenset(
                {KeyCapability.SIGN, KeyCapability.CERTIFY, KeyCapability.ENCRYPT}
            ),
            direct_capabilities=frozenset(
                {KeyCapability.SIGN, KeyCapability.CERTIFY}
            ),
            aggregate_capabilities=frozenset(
                {KeyCapability.SIGN, KeyCapability.CERTIFY, KeyCapability.ENCRYPT}
            ),
            created_on=created,
            expires_on=expires,
            expires_epoch=_epoch(expires),
            is_primary=True,
        ),
        subkeys=(
            KeyRecord(
                fingerprint=subkey,
                algorithm="RSA",
                bits=4096,
                capabilities=frozenset({KeyCapability.ENCRYPT}),
                direct_capabilities=frozenset({KeyCapability.ENCRYPT}),
                created_on=created,
                expires_on=expires,
                expires_epoch=_epoch(expires),
                is_primary=False,
            ),
        ),
    )


def _facts(fingerprint: str) -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "identities": [{"text": f"Owner {fingerprint[0]}", "validity": "u"}],
        "records": [],
    }


def _signature(fingerprint: str, payload: bytes) -> bytes:
    return fingerprint.encode("ascii") + b":" + hashlib.sha256(payload).digest()


def _record_and_approval() -> tuple[bytes, ApprovedTransitionRecord]:
    old_entry = {
        "fingerprint": OLD,
        "certificate": base64.b64encode(OLD_CERT).decode("ascii"),
        "inspection": _facts(OLD),
    }
    proposed_entry = {
        "fingerprint": PROPOSED,
        "certificate": base64.b64encode(PROPOSED_CERT).decode("ascii"),
        "inspection": _facts(PROPOSED),
    }
    record_object = {
        "format": "localsetup.openpgp-owner-transition",
        "schema_version": 1,
        "transition_id": TRANSITION_ID,
        "nonce": NONCE,
        "old_owner": old_entry,
        "proposed_owner": proposed_entry,
        "effective_at": NOW + 3600,
        "overlap_seconds": 3600,
        "scope": ["openpgp-owner"],
        "predecessor_fingerprint": OLD,
        "epoch": 2,
        "reason": "Planned owner-key rotation",
        "revocation_instructions": "Revoke the prior authority after overlap.",
        "rollback_instructions": "Restore the prior authority only by a new approved record.",
    }
    record = json.dumps(
        record_object, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    proposed_signature = _signature(PROPOSED, record)
    proposal = TransitionProposal(record, proposed_signature)
    approved = ApprovedTransitionRecord(
        record=record,
        proposed_signature=proposed_signature,
        old_owner_signature=_signature(OLD, record),
        approval_signature=_signature(OLD, proposal.to_bytes()),
    )
    return record, approved


def _approval_for_record(record_object: dict[str, object]) -> ApprovedTransitionRecord:
    record = json.dumps(
        record_object, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    proposal = TransitionProposal(record, _signature(PROPOSED, record))
    return ApprovedTransitionRecord(
        record,
        proposal.proposed_signature,
        _signature(OLD, record),
        _signature(OLD, proposal.to_bytes()),
    )


def _configure_verification(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allow_alternate_candidate_signature: bool = False,
) -> bytes:
    record, _approved = _record_and_approval()
    inspections = {
        OLD: (_inspection(OLD, "D" * 40), _facts(OLD), OLD_CERT),
        PROPOSED: (_inspection(PROPOSED, "E" * 40), _facts(PROPOSED), PROPOSED_CERT),
    }

    def inspect_owner(owner, _executable):
        fingerprint = owner["fingerprint"]
        inspection, facts, certificate = inspections[fingerprint]
        if (
            owner["certificate"] != base64.b64encode(certificate).decode("ascii")
            or owner["inspection"] != facts
        ):
            raise TransitionError(TransitionErrorCode.RECORD_MISMATCH)
        return inspection, facts

    def verify_signature(payload, signature, *, expected_fingerprint, **_kwargs):
        expected = _signature(expected_fingerprint, payload)
        if signature == expected:
            return
        if (
            allow_alternate_candidate_signature
            and expected_fingerprint == PROPOSED
            and payload == record
            and signature == b"candidate-alternative-valid-signature"
        ):
            return
        raise TransitionError(TransitionErrorCode.INVALID_SIGNATURE)

    monkeypatch.setattr(transition_api, "_inspect_owner_from_record", inspect_owner)
    monkeypatch.setattr(transition_api, "_verify_detached_signature", verify_signature)
    return record


def test_both_owner_proofs_verify_without_enrolling_or_dropping_historical_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()
    trust = LocalTrust(frozenset({HISTORICAL, OLD}))

    verified = verify_approved_transition(
        approved,
        local_trust=trust,
        known_transition_ids=(),
        known_nonces=(),
        now=NOW,
    )

    assert verified.old_owner_fingerprint == OLD
    assert verified.proposed_owner_fingerprint == PROPOSED
    assert verified.predecessor_fingerprint == OLD
    assert verified.epoch == 2
    assert verified.proposed_owner_identities == ("Owner B",)
    assert trust.fingerprints == frozenset({HISTORICAL, OLD})
    assert PROPOSED not in trust.fingerprints


def test_verification_accepts_activation_and_overlap_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()
    record = json.loads(approved.record)
    effective_at = record["effective_at"]
    overlap_seconds = record["overlap_seconds"]
    assert type(effective_at) is int and type(overlap_seconds) is int

    for timestamp in (effective_at - 1, effective_at, effective_at + overlap_seconds):
        verify_approved_transition(
            approved,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            now=timestamp,
        )

    with pytest.raises(TransitionError) as expired_window:
        verify_approved_transition(
            approved,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            now=effective_at + overlap_seconds + 1,
        )
    assert expired_window.value.code is TransitionErrorCode.UNSAFE_TIME

    verify_approved_transition(
        approved,
        local_trust=LocalTrust(frozenset({OLD})),
        known_transition_ids=(),
        known_nonces=(),
        allow_historical=True,
        now=effective_at + overlap_seconds + 1,
    )


def test_zero_overlap_can_be_verified_at_effective_time_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()
    record_object = json.loads(approved.record)
    record_object["overlap_seconds"] = 0
    zero_overlap_approval = _approval_for_record(record_object)
    effective_at = record_object["effective_at"]
    assert type(effective_at) is int

    verify_approved_transition(
        zero_overlap_approval,
        local_trust=LocalTrust(frozenset({OLD})),
        known_transition_ids=(),
        known_nonces=(),
        now=effective_at,
    )

    with pytest.raises(TransitionError) as after_effective:
        verify_approved_transition(
            zero_overlap_approval,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            now=effective_at + 1,
        )
    assert after_effective.value.code is TransitionErrorCode.UNSAFE_TIME


def test_supplied_owner_epoch_must_match_the_record_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()

    verified = verify_approved_transition(
        approved,
        local_trust=LocalTrust(frozenset({OLD})),
        known_transition_ids=(),
        known_nonces=(),
        current_owner_fingerprint=OLD,
        current_epoch=1,
        now=NOW,
    )
    assert verified.epoch == 2

    with pytest.raises(TransitionError) as mismatched_epoch:
        verify_approved_transition(
            approved,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            current_owner_fingerprint=OLD,
            current_epoch=2,
            now=NOW,
        )
    assert mismatched_epoch.value.code is TransitionErrorCode.RECORD_MISMATCH

    with pytest.raises(TransitionError) as mismatched_owner:
        verify_approved_transition(
            approved,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            current_owner_fingerprint=HISTORICAL,
            current_epoch=1,
            now=NOW,
        )
    assert mismatched_owner.value.code is TransitionErrorCode.RECORD_MISMATCH


def test_candidate_only_proof_cannot_authorize_a_consumer_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()
    candidate_only = ApprovedTransitionRecord(
        approved.record, approved.proposed_signature, b"", b""
    )

    with pytest.raises(TransitionError) as error:
        verify_approved_transition(
            candidate_only,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            now=NOW,
        )

    assert error.value.code is TransitionErrorCode.INVALID_AUTHORIZATION


def test_record_tampering_invalidates_the_candidate_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()
    changed = json.loads(approved.record)
    changed["reason"] = "Different rotation reason"
    tampered_record = json.dumps(
        changed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    tampered = ApprovedTransitionRecord(
        tampered_record,
        approved.proposed_signature,
        approved.old_owner_signature,
        approved.approval_signature,
    )

    with pytest.raises(TransitionError) as error:
        verify_approved_transition(
            tampered,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            now=NOW,
        )

    assert error.value.code is TransitionErrorCode.INVALID_SIGNATURE


def test_approved_owner_signature_binds_the_exact_candidate_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch, allow_alternate_candidate_signature=True)
    _record, approved = _record_and_approval()
    alternate_proof = ApprovedTransitionRecord(
        approved.record,
        b"candidate-alternative-valid-signature",
        approved.old_owner_signature,
        approved.approval_signature,
    )

    with pytest.raises(TransitionError) as error:
        verify_approved_transition(
            alternate_proof,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            now=NOW,
        )

    assert error.value.code is TransitionErrorCode.INVALID_SIGNATURE


def test_unenrolled_prior_authority_and_replayed_transition_are_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()

    with pytest.raises(TransitionError) as untrusted:
        verify_approved_transition(
            approved,
            local_trust=LocalTrust(frozenset({HISTORICAL})),
            known_transition_ids=(),
            known_nonces=(),
            now=NOW,
        )
    assert untrusted.value.code is TransitionErrorCode.UNTRUSTED_OLD_OWNER

    with pytest.raises(TransitionError) as replayed:
        verify_approved_transition(
            approved,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(TRANSITION_ID,),
            known_nonces=(),
            now=NOW,
        )
    assert replayed.value.code is TransitionErrorCode.REPLAY

    with pytest.raises(TransitionError) as nonce_replay:
        verify_approved_transition(
            approved,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(NONCE,),
            now=NOW,
        )
    assert nonce_replay.value.code is TransitionErrorCode.REPLAY


def test_embedded_certificate_must_match_the_inspected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()
    changed = json.loads(approved.record)
    changed["proposed_owner"]["certificate"] = base64.b64encode(
        b"substituted proposed certificate"
    ).decode("ascii")
    mismatched_record = json.dumps(
        changed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    mismatched = ApprovedTransitionRecord(
        mismatched_record,
        approved.proposed_signature,
        approved.old_owner_signature,
        approved.approval_signature,
    )

    with pytest.raises(TransitionError) as error:
        verify_approved_transition(
            mismatched,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            now=NOW,
        )

    assert error.value.code is TransitionErrorCode.RECORD_MISMATCH


def test_transition_cannot_outlive_either_owner_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_verification(monkeypatch)
    _record, approved = _record_and_approval()

    changed = json.loads(approved.record)
    changed["effective_at"] = NOW + 200 * 24 * 60 * 60
    late_record = json.dumps(
        changed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    late_approval = ApprovedTransitionRecord(
        late_record,
        approved.proposed_signature,
        approved.old_owner_signature,
        approved.approval_signature,
    )

    with pytest.raises(TransitionError) as error:
        verify_approved_transition(
            late_approval,
            local_trust=LocalTrust(frozenset({OLD})),
            known_transition_ids=(),
            known_nonces=(),
            now=NOW,
        )
    assert error.value.code is TransitionErrorCode.UNSAFE_EXPIRY


@pytest.mark.parametrize(
    ("bits", "created", "expires", "valid_until"),
    (
        (2048, date(2025, 1, 1), date(2027, 1, 1), NOW),
        (4096, date(2024, 1, 1), date(2026, 1, 1), _epoch(date(2026, 1, 2))),
    ),
)
def test_validsig_signing_component_must_match_transition_profile_and_window(
    monkeypatch: pytest.MonkeyPatch,
    bits: int,
    created: date,
    expires: date,
    valid_until: int,
) -> None:
    signer = "F" * 40
    primary = _inspection(PROPOSED, "E" * 40).primary
    signing_component = KeyRecord(
        fingerprint=signer,
        algorithm="RSA",
        bits=bits,
        capabilities=frozenset({KeyCapability.SIGN}),
        direct_capabilities=frozenset({KeyCapability.SIGN}),
        created_on=created,
        expires_on=expires,
        expires_epoch=_epoch(expires),
        is_primary=False,
    )
    inspection = KeyInspection(primary=primary, subkeys=(signing_component,))
    status = (
        f"[GNUPG:] VALIDSIG {signer} 20260923 1790164800 0 4 0 1 10 00 {PROPOSED}\n"
    ).encode("ascii")

    def synthetic_gpg(_binary: str, _home: Path, arguments: tuple[str, ...]) -> bytes:
        return b"" if "--import" in arguments else status

    monkeypatch.setattr(transition_api._keys, "_run_gpg", synthetic_gpg)

    with pytest.raises(TransitionError) as error:
        transition_api._verify_detached_signature(
            b"transition record",
            b"synthetic detached signature",
            certificate=b"synthetic certificate",
            inspection=inspection,
            expected_fingerprint=PROPOSED,
            valid_until=valid_until,
            gpg_binary="synthetic-gpg",
        )

    assert error.value.code is TransitionErrorCode.UNSAFE_EXPIRY


_REAL_KEY_SECRET = SecretReference(SecretProvider.ENV, "L09_TRANSITION_TEST_SECRET")


class _RealKeyResolver(SecretResolver):
    def resolve(self, reference: SecretReference) -> str:
        if reference != _REAL_KEY_SECRET:
            raise RuntimeError
        return "synthetic-transition-test-passphrase"


@pytest.mark.skipif(
    any(
        shutil.which(name) is None
        for name in ("gpg", "gpgconf", "gpg-connect-agent")
    ),
    reason="GnuPG executables are unavailable",
)
def test_generated_candidate_signs_without_leaking_or_killing_agent(
    tmp_path: Path,
) -> None:
    resolver = _RealKeyResolver()
    home = tmp_path / "candidate-home"
    candidate = generate_key(
        identity=KeyIdentity(
            name="Synthetic transition candidate",
            email="candidate@example.invalid",
        ),
        profile=KeyProfile(),
        passphrase_reference=_REAL_KEY_SECRET,
        secret_resolver=resolver,
        keyring_home=home,
    )

    assert not recovery_api._home_agent_is_running(home)
    payload = b"canonical transition record for real-key signer regression"
    signature = transition_api._sign_detached(
        payload,
        fingerprint=candidate.primary_fingerprint,
        gnupg_home=home,
        passphrase_reference=_REAL_KEY_SECRET,
        secret_resolver=resolver,
        gpg_binary=shutil.which("gpg") or "gpg",
    )
    inspection = inspect_key(certificate=candidate.public_certificate)
    transition_api._verify_detached_signature(
        payload,
        signature,
        certificate=candidate.public_certificate,
        inspection=inspection,
        expected_fingerprint=candidate.primary_fingerprint,
        gpg_binary=shutil.which("gpg") or "gpg",
    )
    assert not recovery_api._home_agent_is_running(home)
