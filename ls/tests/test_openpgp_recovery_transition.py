from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ls.core.openpgp import publishing_transition, recovery_transition, transition, trust_state


SCOPE = ("agentq:recovery-test",)
OWNER = "A" * 40
PUBLISHER = "B" * 40
RECOVERY = "C" * 40
NEW_OWNER = "D" * 40
NEW_PUBLISHER = "E" * 40


def _signature(fingerprint: str, record: bytes) -> bytes:
    return fingerprint.encode("ascii") + b":" + hashlib.sha256(record).digest()


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "authority.db"
    clock = [1_000_000_000]
    monkeypatch.setattr(trust_state, "_clock", lambda: clock[0])
    pins = {
        b"owner": OWNER,
        b"publisher": PUBLISHER,
        b"offline-recovery": RECOVERY,
        b"next-owner": NEW_OWNER,
        b"next-publisher": NEW_PUBLISHER,
    }
    monkeypatch.setattr(
        trust_state, "inspect_key",
        lambda *, certificate, **_: SimpleNamespace(primary_fingerprint=pins[certificate]),
    )
    monkeypatch.setattr(
        recovery_transition, "_inspection",
        lambda certificate, **_: SimpleNamespace(primary_fingerprint=pins[certificate]),
    )
    monkeypatch.setattr(transition, "_validate_owner_usable", lambda *_, **__: None)
    monkeypatch.setattr(publishing_transition, "_validate_party", lambda *_, **__: None)

    def verify(record, signature, *, expected_fingerprint, **_):
        if signature != _signature(expected_fingerprint, record):
            raise transition.TransitionError(transition.TransitionErrorCode.INVALID_SIGNATURE)

    monkeypatch.setattr(transition, "_verify_detached_signature", verify)
    initial = trust_state.initialize_trust_state(
        path, scope=SCOPE, owner_certificate=b"owner",
        publisher_certificate=b"publisher",
        recovery_certificates=(b"offline-recovery",),
    )
    return path, clock, initial


def _challenge(path, initial, *, role="owner", certificate=b"next-owner"):
    return recovery_transition.create_recovery_challenge(
        path, role=role, successor_certificate=certificate,
        reason="Lost operational key", expected_scope=SCOPE,
        expected_store_id=initial.store_id, expected_revision=initial.revision,
    )


def _candidate(path, initial, challenge):
    return recovery_transition.submit_candidate_proof(
        path, challenge_id=challenge.challenge_id,
        candidate_signature=_signature(challenge.successor_fingerprint, challenge.record),
        expected_scope=SCOPE, expected_store_id=initial.store_id,
        expected_revision=initial.revision,
    )


def _apply(path, initial, challenge, **extra):
    return recovery_transition.apply_recovery_transition(
        path, challenge_id=challenge.challenge_id,
        expected_scope=SCOPE, expected_store_id=initial.store_id,
        expected_revision=initial.revision, **extra,
    )


def test_local_recovery_requires_candidate_and_exact_console_digest(store):
    path, _, initial = store
    challenge = _challenge(path, initial)
    assert challenge.store_id == initial.store_id
    assert challenge.current_fingerprint == OWNER
    assert challenge.successor_fingerprint == NEW_OWNER
    assert challenge.successor_certificate_sha256 == hashlib.sha256(b"next-owner").hexdigest()
    assert challenge.sha256 == hashlib.sha256(challenge.record).hexdigest()
    with pytest.raises(trust_state.TrustStateError, match="candidate proof"):
        recovery_transition.authorize_recovery_locally(
            path, challenge_id=challenge.challenge_id,
            challenge_sha256=challenge.sha256, expected_scope=SCOPE,
            expected_store_id=initial.store_id, expected_revision=initial.revision,
        )
    with pytest.raises(transition.TransitionError):
        recovery_transition.submit_candidate_proof(
            path, challenge_id=challenge.challenge_id,
            candidate_signature=_signature(OWNER, challenge.record),
            expected_scope=SCOPE, expected_store_id=initial.store_id,
            expected_revision=initial.revision,
        )
    _candidate(path, initial, challenge)
    with pytest.raises(trust_state.TrustStateError, match="authorization"):
        _apply(path, initial, challenge)
    with pytest.raises(trust_state.TrustStateError, match="digest"):
        recovery_transition.authorize_recovery_locally(
            path, challenge_id=challenge.challenge_id,
            challenge_sha256="0" * 64, expected_scope=SCOPE,
            expected_store_id=initial.store_id, expected_revision=initial.revision,
        )
    receipt = recovery_transition.authorize_recovery_locally(
        path, challenge_id=challenge.challenge_id,
        challenge_sha256=challenge.sha256, expected_scope=SCOPE,
        expected_store_id=initial.store_id, expected_revision=initial.revision,
    )
    assert receipt.sha256 == challenge.sha256
    recovered = _apply(path, initial, challenge)
    assert recovered.owner.fingerprint == NEW_OWNER
    assert recovered.publisher.fingerprint == PUBLISHER
    assert recovered.epoch == 2 and recovered.revision == 2
    assert trust_state.authorize_outbound(
        path, expected_scope=SCOPE, role="owner"
    ).fingerprint == NEW_OWNER
    with pytest.raises(trust_state.TrustStateError):
        trust_state.authorize_inbound_peer(
            path, expected_scope=SCOPE, role="owner", fingerprint=OWNER,
        )
    with pytest.raises(trust_state.TrustStateError):
        _apply(path, initial, challenge)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM consumed").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM revocations WHERE role='owner'").fetchone()[0] == 1


def test_independent_offline_signature_works_without_local_approval(store):
    path, _, initial = store
    challenge = _challenge(path, initial, role="publisher", certificate=b"next-publisher")
    _candidate(path, initial, challenge)
    with pytest.raises(trust_state.TrustStateError, match="not enrolled"):
        _apply(
            path, initial, challenge, offline_recovery_fingerprint=OWNER,
            offline_signature=_signature(OWNER, challenge.record),
        )
    with pytest.raises(transition.TransitionError):
        _apply(
            path, initial, challenge, offline_recovery_fingerprint=RECOVERY,
            offline_signature=_signature(PUBLISHER, challenge.record),
        )
    recovered = _apply(
        path, initial, challenge, offline_recovery_fingerprint=RECOVERY,
        offline_signature=_signature(RECOVERY, challenge.record),
    )
    assert recovered.publisher.fingerprint == NEW_PUBLISHER
    assert recovered.owner.fingerprint == OWNER
    assert recovered.epoch == 2


def test_revoked_role_recovers_without_old_key_and_history_survives(store):
    path, _, initial = store
    trust_state.record_accepted_content(
        path, b"authenticated old ciphertext", signer_fingerprint=PUBLISHER,
        role="publisher", expected_scope=SCOPE,
        expected_store_id=initial.store_id, expected_revision=initial.revision,
    )
    revoked = trust_state.revoke_authority(
        path, role="publisher", expected_scope=SCOPE,
        expected_store_id=initial.store_id, expected_revision=initial.revision,
    )
    assert revoked.publisher is None
    challenge = _challenge(path, revoked, role="publisher", certificate=b"next-publisher")
    assert challenge.current_fingerprint is None
    _candidate(path, revoked, challenge)
    recovery_transition.authorize_recovery_locally(
        path, challenge_id=challenge.challenge_id, challenge_sha256=challenge.sha256,
        expected_scope=SCOPE, expected_store_id=revoked.store_id,
        expected_revision=revoked.revision,
    )
    recovered = _apply(path, revoked, challenge)
    assert recovered.publisher.fingerprint == NEW_PUBLISHER
    assert recovered.epoch == 3
    historical = trust_state.authorize_historical_content(
        path, b"authenticated old ciphertext", signer_fingerprint=PUBLISHER,
        role="publisher", expected_scope=SCOPE,
    )
    assert historical.accepted_epoch == 1
    assert historical.epoch == 3
    trust_state.record_accepted_content(
        path, b"new authenticated ciphertext", signer_fingerprint=NEW_PUBLISHER,
        role="publisher", expected_scope=SCOPE,
        expected_store_id=recovered.store_id, expected_revision=recovered.revision,
    )
    newer = trust_state.authorize_historical_content(
        path, b"new authenticated ciphertext", signer_fingerprint=NEW_PUBLISHER,
        role="publisher", expected_scope=SCOPE,
    )
    assert newer.accepted_epoch == 3


def test_expiry_scope_cross_store_stale_revision_and_certificate_tamper(store, tmp_path, monkeypatch):
    path, clock, initial = store
    challenge = recovery_transition.create_recovery_challenge(
        path, role="owner", successor_certificate=b"next-owner", reason="Lost",
        expected_scope=SCOPE, expected_store_id=initial.store_id,
        expected_revision=initial.revision, ttl_seconds=5,
    )
    _candidate(path, initial, challenge)
    with pytest.raises(trust_state.TrustStateError, match="scope"):
        recovery_transition.authorize_recovery_locally(
            path, challenge_id=challenge.challenge_id, challenge_sha256=challenge.sha256,
            expected_scope=("other",), expected_store_id=initial.store_id,
            expected_revision=initial.revision,
        )
    clock[0] += 5
    with pytest.raises(trust_state.TrustStateError, match="expired"):
        _apply(path, initial, challenge)
    clock[0] -= 5
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE recovery_challenges SET successor_certificate=? WHERE challenge_id=?",
            (b"wrong", challenge.challenge_id),
        )
    with pytest.raises(trust_state.TrustStateError, match="corrupt"):
        _apply(path, initial, challenge)


def test_recovery_cancels_pending_without_releasing_consumed_nonce(store, monkeypatch):
    path, clock, initial = store
    # A previously admitted routine rotation is pending when independent
    # recovery takes control. Its ID and nonce remain consumed.
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO consumed VALUES ('id','routine-id')")
        db.execute("INSERT INTO consumed VALUES ('nonce','routine-nonce')")
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "routine-id", "routine-nonce", "owner", OWNER, "F" * 40, 2,
            clock[0] + 100, clock[0] + 120, "pending", b"signed routine", "0" * 64,
        ))
    challenge = _challenge(path, initial)
    _candidate(path, initial, challenge)
    recovery_transition.authorize_recovery_locally(
        path, challenge_id=challenge.challenge_id, challenge_sha256=challenge.sha256,
        expected_scope=SCOPE, expected_store_id=initial.store_id,
        expected_revision=initial.revision,
    )
    recovered = _apply(path, initial, challenge)
    assert recovered.pending_transition_id is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM events WHERE transition_id='routine-id'").fetchone()[0] == "cancelled"
        assert db.execute("SELECT count(*) FROM consumed").fetchone()[0] == 4


def test_cross_store_challenge_and_operator_enrollment_boundary(store, monkeypatch):
    path, _, initial = store
    other = path.parent / "other.db"
    second = trust_state.initialize_trust_state(
        other, scope=SCOPE, owner_certificate=b"owner",
        publisher_certificate=b"publisher",
    )
    challenge = _challenge(path, initial)
    _candidate(path, initial, challenge)
    with pytest.raises(trust_state.TrustStateError, match="unknown"):
        recovery_transition.apply_recovery_transition(
            other, challenge_id=challenge.challenge_id, expected_scope=SCOPE,
            expected_store_id=second.store_id, expected_revision=second.revision,
        )
    with pytest.raises(trust_state.TrustStateError, match="independent"):
        recovery_transition.enroll_recovery_key(
            other, certificate=b"owner", expected_scope=SCOPE,
            expected_store_id=second.store_id, expected_revision=second.revision,
        )
    monkeypatch.setattr(
        recovery_transition, "_inspection",
        lambda certificate, **_: SimpleNamespace(
            primary_fingerprint=("F" * 40 if certificate == b"second-recovery" else OWNER)
        ),
    )
    enrolled = recovery_transition.enroll_recovery_key(
        other, certificate=b"second-recovery", expected_scope=SCOPE,
        expected_store_id=second.store_id, expected_revision=second.revision,
    )
    assert enrolled.epoch == 2 and enrolled.revision == 2
    with sqlite3.connect(other) as db:
        assert db.execute("SELECT fingerprint FROM recovery_keys").fetchone()[0] == "F" * 40
