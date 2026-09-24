from __future__ import annotations

import hashlib
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


def _version_one_store(tmp_path: Path) -> Path:
    """A fixture with the exact tables and persisted data shipped by L11."""
    private = tmp_path / "version-one"
    private.mkdir(mode=0o700)
    path = private / "trust.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                store_id TEXT NOT NULL, scope TEXT NOT NULL,
                revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
                last_observed INTEGER NOT NULL,
                owner_fp TEXT, publisher_fp TEXT
            );
            CREATE TABLE keys (fingerprint TEXT PRIMARY KEY, certificate BLOB NOT NULL);
            CREATE TABLE events (
                transition_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK(role IN ('owner','publisher')),
                old_fp TEXT NOT NULL, new_fp TEXT NOT NULL,
                epoch INTEGER NOT NULL, effective_at INTEGER NOT NULL,
                overlap_end INTEGER NOT NULL, status TEXT NOT NULL,
                record BLOB NOT NULL, record_sha256 TEXT NOT NULL
            );
            CREATE UNIQUE INDEX one_pending ON events(status) WHERE status='pending';
            CREATE TABLE consumed (kind TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(kind,value));
            CREATE TABLE receipts (
                ciphertext_sha256 TEXT PRIMARY KEY, signer_fp TEXT NOT NULL,
                role TEXT NOT NULL, epoch INTEGER NOT NULL,
                event_id TEXT, accepted_at INTEGER NOT NULL
            );
            CREATE TABLE revocations (
                role TEXT NOT NULL CHECK(role IN ('owner','publisher')),
                fingerprint TEXT NOT NULL, revoked_at INTEGER NOT NULL,
                PRIMARY KEY(role,fingerprint)
            );
            PRAGMA user_version=1;
        """)
        db.execute("INSERT INTO state VALUES (1,?,?,?,?,?,?,?)", (
            "a" * 64, SCOPE[0], 4, 2, 1_000_000_000, NEXT_OWNER, PUBLISHER,
        ))
        db.executemany("INSERT INTO keys VALUES (?,?)", (
            (OWNER, b"original owner"), (NEXT_OWNER, b"current owner"),
            (PUBLISHER, b"publisher"),
        ))
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "prior-owner-rotation", "prior-nonce", "owner", OWNER, NEXT_OWNER,
            2, 999_999_990, 1_000_000_100, "active", b"signed record", "b" * 64,
        ))
        db.executemany("INSERT INTO consumed VALUES (?,?)", (
            ("id", "prior-owner-rotation"), ("nonce", "prior-nonce"),
        ))
        db.execute("INSERT INTO revocations VALUES (?,?,?)", (
            "publisher", "F" * 40, 999_999_980,
        ))
        db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?)", (
            hashlib.sha256(b"accepted v1 ciphertext").hexdigest(),
            OWNER, "owner", 1, "prior-owner-rotation", 999_999_995,
        ))
    path.chmod(0o600)
    return path


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


def test_version_one_migration_preserves_authority_and_history(tmp_path, monkeypatch):
    path = _version_one_store(tmp_path)
    monkeypatch.setattr(trust_state, "_clock", lambda: 1_000_000_000)
    preserved = ("state", "keys", "events", "consumed", "revocations", "receipts")
    with sqlite3.connect(path) as db:
        before = {table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                  for table in preserved}
    snapshot = trust_state.load_trust_state(path, expected_scope=SCOPE)
    assert snapshot.store_id == "a" * 64
    assert snapshot.revision == 4 and snapshot.epoch == 2
    assert snapshot.owner.fingerprint == NEXT_OWNER
    assert snapshot.publisher.fingerprint == PUBLISHER
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        for table in preserved:
            assert db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == before[table]
        assert db.execute("SELECT count(*) FROM recovery_keys").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM recovery_challenges").fetchone()[0] == 0
        columns = db.execute("PRAGMA table_info(events)").fetchall()
        assert columns[3][1] == "old_fp" and columns[3][3] == 0
    historical = trust_state.authorize_historical_content(
        path, b"accepted v1 ciphertext", signer_fingerprint=OWNER,
        role="owner", expected_scope=SCOPE,
    )
    assert historical.certificate == b"original owner"
    assert historical.accepted_epoch == 1


def test_version_one_migration_rejects_unknown_schema_without_mutation(tmp_path, monkeypatch):
    path = _version_one_store(tmp_path)
    monkeypatch.setattr(trust_state, "_clock", lambda: 1_000_000_000)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unexpected (value TEXT)")
    before = path.read_bytes()
    with pytest.raises(trust_state.TrustStateError, match="schema"):
        trust_state.load_trust_state(path, expected_scope=SCOPE)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_version_one_migration_rolls_back_mid_upgrade(tmp_path, monkeypatch):
    path = _version_one_store(tmp_path)
    monkeypatch.setattr(trust_state, "_clock", lambda: 1_000_000_000)
    before = path.read_bytes()

    def fail_after_event_rebuild(_db):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(trust_state, "_install_recovery_schema", fail_after_event_rebuild)
    with pytest.raises(RuntimeError, match="injected"):
        trust_state.load_trust_state(path, expected_scope=SCOPE)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert db.execute("PRAGMA table_info(events)").fetchall()[3][3] == 1
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='events_v2'").fetchone()[0] == 0


def test_competing_version_one_openers_complete_one_migration(tmp_path, monkeypatch):
    path = _version_one_store(tmp_path)
    monkeypatch.setattr(trust_state, "_clock", lambda: 1_000_000_000)
    gate = Barrier(2)

    def load():
        gate.wait(timeout=5)
        return trust_state.load_trust_state(path, expected_scope=SCOPE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = (pool.submit(load), pool.submit(load))
        snapshots = [future.result() for future in results]
    assert snapshots[0] == snapshots[1]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM recovery_keys").fetchone()[0] == 0
