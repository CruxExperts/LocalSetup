from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier

import pytest

from ls.core.openpgp import publishing_transition, transition, trust_state


SCOPE = ("agentq:fixture",)
OWNER = "A" * 40
PUBLISHER = "B" * 40
NEXT_OWNER = "C" * 40
NEXT_PUBLISHER = "D" * 40


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "trust.db"
    clock = [1_000_000_000]
    monkeypatch.setattr(trust_state, "_clock", lambda: clock[0])

    fingerprints = {b"owner": OWNER, b"publisher": PUBLISHER}
    monkeypatch.setattr(
        trust_state, "inspect_key",
        lambda *, certificate, **_: SimpleNamespace(primary_fingerprint=fingerprints[certificate]),
    )
    monkeypatch.setattr(transition, "_validate_owner_usable", lambda *_, **__: None)
    monkeypatch.setattr(publishing_transition, "_validate_party", lambda *_, **__: None)
    initial = trust_state.initialize_trust_state(
        path, scope=SCOPE, owner_certificate=b"owner", publisher_certificate=b"publisher"
    )
    return path, clock, initial


def _owner_proof(monkeypatch: pytest.MonkeyPatch, *, old: str = OWNER,
                 new: str = NEXT_OWNER, epoch: int = 2, effective: int = 1_000_000_000,
                 overlap: int = 30, transition_id: str = "id-1", nonce: str = "nonce-1") -> None:
    def verify(serialized: bytes, *, local_trust, current_owner_fingerprint, current_epoch, **_):
        assert serialized == b"signed-owner"
        assert current_owner_fingerprint == old
        assert current_epoch == epoch - 1
        assert old in local_trust.fingerprints
        return SimpleNamespace(
            scope=SCOPE, proposed_owner_fingerprint=new, epoch=epoch,
            effective_at=effective, overlap_seconds=overlap,
            transition_id=transition_id, nonce=nonce,
        )

    monkeypatch.setattr(transition, "verify_approved_transition", verify)
    monkeypatch.setattr(
        transition.ApprovedTransitionRecord, "from_bytes",
        classmethod(lambda cls, _: SimpleNamespace(record=b"record")),
    )
    monkeypatch.setattr(transition, "_record_object", lambda _: {"proposed_owner": {}})
    monkeypatch.setattr(transition, "_certificate_from_owner_record", lambda _: b"next-owner")


def _publisher_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    def verify(serialized: bytes, *, local_trust, current_owner_fingerprint,
               current_publisher_fingerprint, current_epoch, **_):
        assert serialized == b"signed-publisher"
        assert current_owner_fingerprint == OWNER
        assert current_publisher_fingerprint == PUBLISHER
        assert current_epoch == 1
        assert local_trust.fingerprints == frozenset({OWNER, PUBLISHER})
        return SimpleNamespace(
            scope=SCOPE, proposed_publisher_fingerprint=NEXT_PUBLISHER,
            epoch=2, effective_at=1_000_000_000, overlap_seconds=30,
            transition_id="pub-1", nonce="pub-nonce-1",
        )

    monkeypatch.setattr(publishing_transition, "verify_approved_publishing_transition", verify)
    monkeypatch.setattr(
        publishing_transition.ApprovedPublishingTransitionRecord, "from_bytes",
        classmethod(lambda cls, _: SimpleNamespace(record=b"publisher-record")),
    )
    monkeypatch.setattr(publishing_transition, "_record_object", lambda _: {"proposed_publisher": {}})
    monkeypatch.setattr(transition, "_certificate_from_owner_record", lambda _: b"next-publisher")


def test_initialize_once_and_scope_binding(store):
    path, _, initial = store
    assert initial.owner.fingerprint == OWNER
    assert initial.publisher.fingerprint == PUBLISHER
    assert initial.revision == initial.epoch == 1
    assert trust_state.load_trust_state(path, expected_scope=SCOPE) == initial
    with pytest.raises(trust_state.TrustStateError, match="scope"):
        trust_state.load_trust_state(path, expected_scope=("agentq:other",))
    with pytest.raises(trust_state.TrustStateError, match="already exists"):
        trust_state.initialize_trust_state(
            path, scope=SCOPE, owner_certificate=b"owner", publisher_certificate=b"publisher"
        )


def test_scheduled_activation_overlap_and_historical_receipt(store, monkeypatch):
    path, clock, initial = store
    _owner_proof(monkeypatch, effective=clock[0] + 10, overlap=20)
    pending = trust_state.apply_owner_transition(
        path, b"signed-owner", expected_store_id=initial.store_id,
        expected_revision=initial.revision,
    )
    assert pending.pending_transition_id == "id-1"
    assert pending.owner.fingerprint == OWNER
    assert pending.epoch == 1 and pending.revision == 2
    with pytest.raises(trust_state.TrustStateError, match="stale"):
        trust_state.apply_owner_transition(
            path, b"signed-owner", expected_store_id=initial.store_id,
            expected_revision=initial.revision,
        )
    clock[0] += 10
    active = trust_state.load_trust_state(path, expected_scope=SCOPE)
    assert active.pending_transition_id is None
    assert active.owner.fingerprint == NEXT_OWNER
    assert active.epoch == 2 and active.revision == 3
    assert trust_state.authorize_outbound(path, expected_scope=SCOPE, role="owner").fingerprint == NEXT_OWNER
    assert trust_state.authorize_inbound_peer(
        path, expected_scope=SCOPE, role="owner", fingerprint=OWNER
    ).fingerprint == OWNER
    digest = trust_state.record_accepted_content(
        path, b"exact ciphertext", signer_fingerprint=OWNER, role="owner",
        expected_scope=SCOPE, expected_store_id=active.store_id,
        expected_revision=active.revision,
    )
    assert len(digest) == 64
    clock[0] += 20
    with pytest.raises(trust_state.TrustStateError):
        trust_state.authorize_inbound_peer(
            path, expected_scope=SCOPE, role="owner", fingerprint=OWNER
        )
    assert trust_state.authorize_historical_content(
        path, b"exact ciphertext", signer_fingerprint=OWNER, role="owner",
        expected_scope=SCOPE,
    ).accepted_epoch == 1
    with pytest.raises(trust_state.TrustStateError, match="receipt"):
        trust_state.authorize_historical_content(
            path, b"different ciphertext", signer_fingerprint=OWNER, role="owner",
            expected_scope=SCOPE,
        )


def test_publisher_proof_uses_both_pins_and_its_own_record_parser(store, monkeypatch):
    path, _, initial = store
    _publisher_proof(monkeypatch)
    active = trust_state.apply_publisher_transition(
        path, b"signed-publisher", expected_store_id=initial.store_id,
        expected_revision=initial.revision,
    )
    assert active.publisher.fingerprint == NEXT_PUBLISHER
    assert active.owner.fingerprint == OWNER
    assert active.epoch == 2


def test_invalid_payload_replay_and_failed_commit_keep_state(store, monkeypatch):
    path, _, initial = store
    _owner_proof(monkeypatch)
    with pytest.raises(trust_state.TrustStateError, match="signed record"):
        trust_state.apply_owner_transition(
            path, SimpleNamespace(), expected_store_id=initial.store_id,
            expected_revision=initial.revision,
        )
    first = trust_state.apply_owner_transition(
        path, b"signed-owner", expected_store_id=initial.store_id,
        expected_revision=initial.revision,
    )
    assert first.owner.fingerprint == NEXT_OWNER
    _owner_proof(monkeypatch, old=NEXT_OWNER, new="E" * 40, epoch=3,
                 transition_id="id-1", nonce="fresh-nonce")
    with pytest.raises(trust_state.TrustStateError, match="replay"):
        trust_state.apply_owner_transition(
            path, b"signed-owner", expected_store_id=first.store_id,
            expected_revision=first.revision,
        )
    assert trust_state.load_trust_state(path, expected_scope=SCOPE) == first
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM consumed").fetchone()[0] == 2


def test_clock_advance_during_crypto_cannot_admit_expired_record(store, monkeypatch):
    path, clock, initial = store
    _owner_proof(monkeypatch, effective=clock[0], overlap=1)
    observations = iter((clock[0], clock[0] + 2))
    monkeypatch.setattr(trust_state, "_clock", lambda: next(observations))
    with pytest.raises(transition.TransitionError):
        trust_state.apply_owner_transition(
            path, b"signed-owner", expected_store_id=initial.store_id,
            expected_revision=initial.revision,
        )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM consumed").fetchone()[0] == 0
        assert db.execute("SELECT revision FROM state").fetchone()[0] == 1


def test_targeted_predecessor_revocation_keeps_current_and_history(store, monkeypatch):
    path, clock, initial = store
    _owner_proof(monkeypatch, overlap=100)
    active = trust_state.apply_owner_transition(
        path, b"signed-owner", expected_store_id=initial.store_id,
        expected_revision=initial.revision,
    )
    trust_state.record_accepted_content(
        path, b"already accepted", signer_fingerprint=OWNER, role="owner",
        expected_scope=SCOPE, expected_store_id=active.store_id,
        expected_revision=active.revision,
    )
    revoked = trust_state.revoke_authority(
        path, role="owner", fingerprint=OWNER, expected_scope=SCOPE,
        expected_store_id=active.store_id, expected_revision=active.revision,
    )
    assert revoked.owner.fingerprint == NEXT_OWNER
    assert revoked.epoch == 3 and revoked.revision == 3
    assert trust_state.authorize_outbound(path, expected_scope=SCOPE, role="owner").fingerprint == NEXT_OWNER
    with pytest.raises(trust_state.TrustStateError, match="revoked"):
        trust_state.authorize_inbound_peer(
            path, expected_scope=SCOPE, role="owner", fingerprint=OWNER,
        )
    historical = trust_state.authorize_historical_content(
        path, b"already accepted", signer_fingerprint=OWNER, role="owner",
        expected_scope=SCOPE,
    )
    assert historical.epoch == 3 and historical.accepted_epoch == 1
    _owner_proof(monkeypatch, old=NEXT_OWNER, new=OWNER, epoch=4,
                 transition_id="id-2", nonce="nonce-2")
    with pytest.raises(trust_state.TrustStateError, match="revoked"):
        trust_state.apply_owner_transition(
            path, b"signed-owner", expected_store_id=revoked.store_id,
            expected_revision=revoked.revision,
        )


def test_offline_pending_activation_and_targeted_cancellation(store, monkeypatch):
    path, clock, initial = store
    _owner_proof(monkeypatch, effective=clock[0] + 10, overlap=20)
    pending = trust_state.apply_owner_transition(
        path, b"signed-owner", expected_store_id=initial.store_id,
        expected_revision=initial.revision,
    )
    clock[0] += 100
    active = trust_state.load_trust_state(path, expected_scope=SCOPE)
    assert active.owner.fingerprint == NEXT_OWNER
    with pytest.raises(trust_state.TrustStateError):
        trust_state.authorize_inbound_peer(
            path, expected_scope=SCOPE, role="owner", fingerprint=OWNER,
        )

    # A second scheduled transition is consumed, then its proposed pin is
    # revoked before activation. The consumed ID and nonce remain durable.
    _owner_proof(monkeypatch, old=NEXT_OWNER, new="E" * 40, epoch=3,
                 effective=clock[0] + 10, transition_id="id-2", nonce="nonce-2")
    pending = trust_state.apply_owner_transition(
        path, b"signed-owner", expected_store_id=active.store_id,
        expected_revision=active.revision,
    )
    revoked = trust_state.revoke_authority(
        path, role="owner", fingerprint="E" * 40, expected_scope=SCOPE,
        expected_store_id=pending.store_id, expected_revision=pending.revision,
    )
    clock[0] += 100
    final = trust_state.load_trust_state(path, expected_scope=SCOPE)
    assert final.owner.fingerprint == NEXT_OWNER
    assert final.pending_transition_id is None
    assert final == revoked
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM consumed").fetchone()[0] == 4
        assert db.execute("SELECT status FROM events WHERE transition_id='id-2'").fetchone()[0] == "cancelled"


def test_competing_apply_serializes_one_winner(store, monkeypatch):
    path, _, initial = store
    _owner_proof(monkeypatch)
    gate = Barrier(2)
    original = transition.verify_approved_transition

    def competing_verify(*args, **kwargs):
        result = original(*args, **kwargs)
        gate.wait(timeout=5)
        return result

    monkeypatch.setattr(transition, "verify_approved_transition", competing_verify)

    def apply():
        return trust_state.apply_owner_transition(
            path, b"signed-owner", expected_store_id=initial.store_id,
            expected_revision=initial.revision,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(apply)
        second = pool.submit(apply)
        outcomes = []
        for future in (first, second):
            try:
                outcomes.append(future.result())
            except trust_state.TrustStateError:
                outcomes.append(None)
    assert sum(outcome is not None for outcome in outcomes) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM consumed").fetchone()[0] == 2


def test_revocation_clock_rollback_and_unsafe_storage(store, monkeypatch, tmp_path):
    path, clock, initial = store
    revoked = trust_state.revoke_authority(
        path, role="publisher", expected_scope=SCOPE,
        expected_store_id=initial.store_id, expected_revision=initial.revision,
    )
    assert revoked.publisher is None and revoked.epoch == 2
    with pytest.raises(trust_state.TrustStateError):
        trust_state.authorize_outbound(path, expected_scope=SCOPE)
    clock[0] -= 1
    with pytest.raises(trust_state.TrustStateError, match="clock rollback"):
        trust_state.load_trust_state(path, expected_scope=SCOPE)
    linked = path.parent / "link.db"
    linked.symlink_to(path)
    with pytest.raises(trust_state.TrustStateError, match="symlink"):
        trust_state.load_trust_state(linked, expected_scope=SCOPE)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(trust_state.TrustStateError, match="schema"):
        trust_state.load_trust_state(path, expected_scope=SCOPE)


def test_uri_metacharacters_are_literal_and_shared_parent_refused(store, monkeypatch, tmp_path):
    path, _, initial = store
    special = path.parent / "trust?#.db"
    special.write_bytes(path.read_bytes())
    special.chmod(0o600)
    assert trust_state.load_trust_state(special, expected_scope=SCOPE).store_id == initial.store_id
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    with pytest.raises(trust_state.TrustStateError, match="writable store ancestor"):
        trust_state.load_trust_state(shared / "trust.db", expected_scope=SCOPE)
