"""Independent, locally authorized replacement of a lost OpenPGP role key.

``authorize_recovery_locally`` and ``enroll_recovery_key`` are trusted operator
entry points. Their caller must already have authenticated the clean-system or
provider-console administrator; this library does not authenticate a provider
and accepts no caller-supplied assertion of remote authentication.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import publishing_transition, transition, trust_state
from .contracts import normalize_fingerprint
from .secrets import SecretReference, SecretResolver


_ROLE = Literal["owner", "publisher"]
_MAX_CHALLENGES = 16
_MAX_RECOVERY_KEYS = 8
_MAX_TTL = 3600


@dataclass(frozen=True, slots=True)
class RecoveryChallenge:
    challenge_id: str
    record: bytes
    sha256: str
    store_id: str
    revision: int
    epoch: int
    role: _ROLE
    current_fingerprint: str | None
    successor_fingerprint: str
    successor_certificate_sha256: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class LocalRecoveryReceipt:
    """Local audit evidence; it is not portable authorization for another store."""

    challenge_id: str
    sha256: str
    store_id: str
    revision: int
    authorized_at: int


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _inspection(certificate: bytes, *, role: _ROLE | Literal["recovery"],
                now: int, valid_until: int, gpg_binary: str | os.PathLike[str]):
    if type(certificate) is not bytes or not (1 <= len(certificate) <= 1_048_576):
        raise trust_state.TrustStateError("invalid recovery certificate")
    executable = transition._validated_gpg_binary(gpg_binary)
    inspection, _facts = transition._inspect_owner_certificate(certificate, executable)
    if role == "owner":
        transition._validate_owner_usable(
            inspection, now=now, effective_at=valid_until, overlap_seconds=0
        )
    else:
        publishing_transition._validate_party(
            inspection, now=now, valid_until=valid_until
        )
    return inspection


def _challenge(row: sqlite3.Row) -> RecoveryChallenge:
    return RecoveryChallenge(
        row["challenge_id"], bytes(row["record"]), row["record_sha256"],
        row["store_id"], row["revision"], row["epoch"], row["role"],
        row["current_fp"], row["successor_fp"],
        row["successor_certificate_sha256"], row["expires_at"],
    )


def _row(db: sqlite3.Connection, challenge_id: str) -> sqlite3.Row:
    if type(challenge_id) is not str or len(challenge_id) != 36:
        raise trust_state.TrustStateError("invalid challenge ID")
    row = db.execute(
        "SELECT * FROM recovery_challenges WHERE challenge_id=?", (challenge_id,)
    ).fetchone()
    if row is None:
        raise trust_state.TrustStateError("unknown recovery challenge")
    if _digest(bytes(row["record"])) != row["record_sha256"]:
        raise trust_state.TrustStateError("corrupt recovery challenge")
    if _digest(bytes(row["successor_certificate"])) != row["successor_certificate_sha256"]:
        raise trust_state.TrustStateError("corrupt successor certificate")
    return row


def _live(db: sqlite3.Connection, state: sqlite3.Row, row: sqlite3.Row, now: int,
          *, expected_store_id: str, expected_revision: int,
          expected_scope: tuple[str, ...]) -> None:
    trust_state._require_scope(state, expected_scope)
    if (
        state["store_id"] != expected_store_id
        or state["revision"] != expected_revision
        or row["store_id"] != state["store_id"]
        or row["scope"] != state["scope"]
        or row["revision"] != state["revision"]
        or row["epoch"] != state["epoch"]
        or row["current_fp"] != state[row["role"] + "_fp"]
        or row["used_at"] is not None
    ):
        raise trust_state.TrustStateError("stale recovery challenge")
    if now >= row["expires_at"]:
        raise trust_state.TrustStateError("expired recovery challenge")
    if trust_state._is_revoked(db, row["role"], row["successor_fp"]):
        raise trust_state.TrustStateError("revoked recovery successor")


def create_recovery_challenge(
    path: str | os.PathLike[str], *, role: _ROLE, successor_certificate: bytes,
    reason: str, expected_scope: tuple[str, ...], expected_store_id: str,
    expected_revision: int, ttl_seconds: int = 900,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> RecoveryChallenge:
    """Persist an expiring challenge; this does not authorize replacement."""
    if role not in {"owner", "publisher"}:
        raise trust_state.TrustStateError("invalid recovery role")
    if type(ttl_seconds) is not int or not (1 <= ttl_seconds <= _MAX_TTL):
        raise trust_state.TrustStateError("invalid recovery lifetime")
    selected_reason = transition._validate_text(reason, max_bytes=1024)
    before = trust_state._clock()
    inspection = _inspection(
        successor_certificate, role=role, now=before,
        valid_until=before + ttl_seconds, gpg_binary=gpg_binary,
    )
    successor = inspection.primary_fingerprint
    with trust_state._open(path) as db:
        state, now = trust_state._begin_fresh(db)
        state = trust_state._activate_due(db, now)
        trust_state._require_scope(state, expected_scope)
        if state["store_id"] != expected_store_id or state["revision"] != expected_revision:
            raise trust_state.TrustStateError("stale recovery request")
        if now < before:
            raise trust_state.TrustStateError("clock rollback during inspection")
        if role == "owner":
            transition._validate_owner_usable(
                inspection, now=now, effective_at=now + ttl_seconds,
                overlap_seconds=0,
            )
        else:
            publishing_transition._validate_party(
                inspection, now=now, valid_until=now + ttl_seconds,
            )
        if successor == state[role + "_fp"] or successor == state[("publisher" if role == "owner" else "owner") + "_fp"]:
            raise trust_state.TrustStateError("successor must be independent")
        if db.execute("SELECT 1 FROM revocations WHERE fingerprint=?", (successor,)).fetchone():
            raise trust_state.TrustStateError("revoked recovery successor")
        if db.execute("SELECT 1 FROM recovery_keys WHERE fingerprint=?", (successor,)).fetchone():
            raise trust_state.TrustStateError("recovery signer cannot be successor")
        if db.execute("SELECT 1 FROM keys WHERE fingerprint=?", (successor,)).fetchone():
            raise trust_state.TrustStateError("successor was already an operational pin")
        count = db.execute(
            "SELECT count(*) FROM recovery_challenges WHERE used_at IS NULL AND expires_at>?",
            (now,),
        ).fetchone()[0]
        if count >= _MAX_CHALLENGES:
            raise trust_state.TrustStateError("too many live recovery challenges")
        challenge_id = str(uuid.uuid4())
        nonce = secrets.token_urlsafe(32)
        expiry = now + ttl_seconds
        record = transition._canonical_json({
            "format": "localsetup.openpgp.recovery-challenge",
            "schema_version": 1,
            "challenge_id": challenge_id,
            "nonce": nonce,
            "store_id": state["store_id"],
            "scope": list(trust_state._scope(state["scope"])),
            "revision": state["revision"],
            "epoch": state["epoch"],
            "role": role,
            "current_fingerprint": state[role + "_fp"],
            "successor_fingerprint": successor,
            "successor_certificate_sha256": _digest(successor_certificate),
            "action": "replace-" + role,
            "reason": selected_reason,
            "created_at": now,
            "expires_at": expiry,
        })
        digest = _digest(record)
        db.execute(
            "INSERT INTO recovery_challenges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (challenge_id, nonce, record, digest, state["store_id"], state["scope"],
             state["revision"], state["epoch"], role, state[role + "_fp"],
             successor, successor_certificate, _digest(successor_certificate),
             expiry, None, None, None),
        )
        return RecoveryChallenge(
            challenge_id, record, digest, state["store_id"], state["revision"],
            state["epoch"], role, state[role + "_fp"], successor,
            _digest(successor_certificate), expiry,
        )


def sign_recovery_challenge(
    challenge: RecoveryChallenge, *, signing_fingerprint: str,
    gnupg_home: str | os.PathLike[str], passphrase_reference: SecretReference,
    secret_resolver: SecretResolver, gpg_binary: str | os.PathLike[str] = "gpg",
) -> bytes:
    """Sign canonical challenge bytes with the candidate or offline recovery key."""
    if not isinstance(challenge, RecoveryChallenge) or _digest(challenge.record) != challenge.sha256:
        raise trust_state.TrustStateError("invalid recovery challenge")
    return transition._sign_detached(
        challenge.record, fingerprint=normalize_fingerprint(signing_fingerprint),
        gnupg_home=gnupg_home, passphrase_reference=passphrase_reference,
        secret_resolver=secret_resolver,
        gpg_binary=transition._validated_gpg_binary(gpg_binary),
    )


def submit_candidate_proof(
    path: str | os.PathLike[str], *, challenge_id: str, candidate_signature: bytes,
    expected_scope: tuple[str, ...], expected_store_id: str,
    expected_revision: int, gpg_binary: str | os.PathLike[str] = "gpg",
) -> RecoveryChallenge:
    """Authenticate and persist candidate signing control of the exact challenge."""
    signature = transition._validated_signature(candidate_signature)
    with trust_state._open(path) as db:
        row = _row(db, challenge_id)
        record = bytes(row["record"])
        certificate = bytes(row["successor_certificate"])
        now = trust_state._clock()
        if now >= row["expires_at"]:
            raise trust_state.TrustStateError("expired recovery challenge")
    inspection = _inspection(
        certificate, role=row["role"], now=now,
        valid_until=row["expires_at"], gpg_binary=gpg_binary,
    )
    if inspection.primary_fingerprint != row["successor_fp"]:
        raise trust_state.TrustStateError("successor pin mismatch")
    transition._verify_detached_signature(
        record, signature, certificate=certificate, inspection=inspection,
        expected_fingerprint=row["successor_fp"], valid_until=row["expires_at"],
        gpg_binary=transition._validated_gpg_binary(gpg_binary),
    )
    with trust_state._open(path) as db:
        state, now = trust_state._begin_fresh(db)
        state = trust_state._activate_due(db, now)
        fresh = _row(db, challenge_id)
        _live(db, state, fresh, now, expected_store_id=expected_store_id,
              expected_revision=expected_revision, expected_scope=expected_scope)
        if bytes(fresh["record"]) != record or bytes(fresh["successor_certificate"]) != certificate:
            raise trust_state.TrustStateError("challenge changed during candidate proof")
        if fresh["candidate_signature"] is not None:
            raise trust_state.TrustStateError("candidate proof already submitted")
        db.execute(
            "UPDATE recovery_challenges SET candidate_signature=? WHERE challenge_id=?",
            (signature, challenge_id),
        )
        return _challenge(fresh)


def authorize_recovery_locally(
    path: str | os.PathLike[str], *, challenge_id: str,
    challenge_sha256: str, expected_scope: tuple[str, ...],
    expected_store_id: str, expected_revision: int,
) -> LocalRecoveryReceipt:
    """Trusted operator only: bind an already-authenticated console decision.

    The caller must compare the full digest through its trusted console or QR
    exchange before invoking this method. Model/RPC input must not invoke it.
    """
    if type(challenge_sha256) is not str or len(challenge_sha256) != 64:
        raise trust_state.TrustStateError("invalid challenge digest")
    with trust_state._open(path) as db:
        state, now = trust_state._begin_fresh(db)
        state = trust_state._activate_due(db, now)
        row = _row(db, challenge_id)
        _live(db, state, row, now, expected_store_id=expected_store_id,
              expected_revision=expected_revision, expected_scope=expected_scope)
        if row["record_sha256"] != challenge_sha256:
            raise trust_state.TrustStateError("challenge digest mismatch")
        if row["candidate_signature"] is None:
            raise trust_state.TrustStateError("candidate proof missing")
        if row["local_authorized_at"] is not None:
            raise trust_state.TrustStateError("challenge already authorized")
        db.execute(
            "UPDATE recovery_challenges SET local_authorized_at=? WHERE challenge_id=?",
            (now, challenge_id),
        )
        return LocalRecoveryReceipt(
            challenge_id, challenge_sha256, state["store_id"], state["revision"], now
        )


def enroll_recovery_key(
    path: str | os.PathLike[str], *, certificate: bytes,
    expected_scope: tuple[str, ...], expected_store_id: str,
    expected_revision: int, gpg_binary: str | os.PathLike[str] = "gpg",
) -> trust_state.TrustSnapshot:
    """Trusted local operator enrollment of an independent offline signing pin."""
    before = trust_state._clock()
    inspection = _inspection(
        certificate, role="recovery", now=before,
        valid_until=before + _MAX_TTL, gpg_binary=gpg_binary,
    )
    fingerprint = inspection.primary_fingerprint
    with trust_state._open(path) as db:
        state, now = trust_state._begin_fresh(db)
        state = trust_state._activate_due(db, now)
        trust_state._require_scope(state, expected_scope)
        if state["store_id"] != expected_store_id or state["revision"] != expected_revision:
            raise trust_state.TrustStateError("stale recovery enrollment")
        if now < before:
            raise trust_state.TrustStateError("clock rollback during inspection")
        publishing_transition._validate_party(
            inspection, now=now, valid_until=now + _MAX_TTL,
        )
        if fingerprint in {state["owner_fp"], state["publisher_fp"]}:
            raise trust_state.TrustStateError("recovery signer must be independent")
        if db.execute("SELECT 1 FROM keys WHERE fingerprint=?", (fingerprint,)).fetchone():
            raise trust_state.TrustStateError("operational key cannot be recovery signer")
        if db.execute("SELECT count(*) FROM recovery_keys").fetchone()[0] >= _MAX_RECOVERY_KEYS:
            raise trust_state.TrustStateError("too many recovery pins")
        try:
            db.execute("INSERT INTO recovery_keys VALUES (?,?,?)", (fingerprint, certificate, now))
        except sqlite3.IntegrityError as exc:
            raise trust_state.TrustStateError("recovery pin already enrolled") from exc
        db.execute("UPDATE events SET status='cancelled' WHERE status='pending'")
        db.execute("UPDATE state SET revision=revision+1, epoch=epoch+1 WHERE singleton=1")
        return trust_state._snapshot(db, trust_state._state(db))


def apply_recovery_transition(
    path: str | os.PathLike[str], *, challenge_id: str,
    expected_scope: tuple[str, ...], expected_store_id: str,
    expected_revision: int, offline_recovery_fingerprint: str | None = None,
    offline_signature: bytes | None = None,
    gpg_binary: str | os.PathLike[str] = "gpg",
) -> trust_state.TrustSnapshot:
    """Atomically replace a role after independent authorization and proof."""
    if (offline_recovery_fingerprint is None) != (offline_signature is None):
        raise trust_state.TrustStateError("incomplete offline authorization")
    with trust_state._open(path) as db:
        row = _row(db, challenge_id)
        record = bytes(row["record"])
        certificate = bytes(row["successor_certificate"])
        candidate_signature = row["candidate_signature"]
        expiry = row["expires_at"]
        role = row["role"]
        successor = row["successor_fp"]
        now = trust_state._clock()
        if now >= expiry:
            raise trust_state.TrustStateError("expired recovery challenge")
        if candidate_signature is None:
            raise trust_state.TrustStateError("candidate proof missing")
        offline_cert: bytes | None = None
        if offline_signature is not None:
            recovery_fp = normalize_fingerprint(offline_recovery_fingerprint)
            signature = transition._validated_signature(offline_signature)
            pin = db.execute(
                "SELECT certificate FROM recovery_keys WHERE fingerprint=?", (recovery_fp,)
            ).fetchone()
            if pin is None:
                raise trust_state.TrustStateError("offline signer not enrolled")
            offline_cert = bytes(pin[0])
    candidate_inspection = _inspection(
        certificate, role=role, now=now, valid_until=expiry, gpg_binary=gpg_binary,
    )
    if candidate_inspection.primary_fingerprint != successor:
        raise trust_state.TrustStateError("successor pin mismatch")
    transition._verify_detached_signature(
        record, bytes(candidate_signature), certificate=certificate,
        inspection=candidate_inspection, expected_fingerprint=successor,
        valid_until=expiry, gpg_binary=transition._validated_gpg_binary(gpg_binary),
    )
    if offline_cert is not None:
        inspection = _inspection(
            offline_cert, role="recovery", now=now,
            valid_until=expiry, gpg_binary=gpg_binary,
        )
        if inspection.primary_fingerprint != recovery_fp:
            raise trust_state.TrustStateError("offline signer pin mismatch")
        transition._verify_detached_signature(
            record, signature, certificate=offline_cert, inspection=inspection,
            expected_fingerprint=recovery_fp, valid_until=expiry,
            gpg_binary=transition._validated_gpg_binary(gpg_binary),
        )
    with trust_state._open(path) as db:
        state, now = trust_state._begin_fresh(db)
        state = trust_state._activate_due(db, now)
        row = _row(db, challenge_id)
        _live(db, state, row, now, expected_store_id=expected_store_id,
              expected_revision=expected_revision, expected_scope=expected_scope)
        if (
            bytes(row["record"]) != record
            or bytes(row["successor_certificate"]) != certificate
            or row["candidate_signature"] != candidate_signature
        ):
            raise trust_state.TrustStateError("challenge changed during verification")
        if row["candidate_signature"] is None:
            raise trust_state.TrustStateError("candidate proof missing")
        if row["local_authorized_at"] is None and offline_cert is None:
            raise trust_state.TrustStateError("independent recovery authorization missing")
        if offline_cert is not None:
            pin = db.execute(
                "SELECT certificate FROM recovery_keys WHERE fingerprint=?", (recovery_fp,)
            ).fetchone()
            if pin is None or bytes(pin[0]) != offline_cert:
                raise trust_state.TrustStateError("offline signer enrollment changed")
        role = row["role"]
        successor = row["successor_fp"]
        certificate = bytes(row["successor_certificate"])
        old = row["current_fp"]
        try:
            db.execute("INSERT INTO consumed VALUES ('id',?)", (challenge_id,))
            db.execute("INSERT INTO consumed VALUES ('nonce',?)", (row["nonce"],))
        except sqlite3.IntegrityError as exc:
            raise trust_state.TrustStateError("recovery challenge replay") from exc
        db.execute("INSERT OR IGNORE INTO keys VALUES (?,?)", (successor, certificate))
        if trust_state._certificate(db, successor) != certificate:
            raise trust_state.TrustStateError("successor certificate conflicts with pin")
        if old is not None:
            db.execute("INSERT OR IGNORE INTO revocations VALUES (?,?,?)", (role, old, now))
        db.execute("UPDATE events SET status='cancelled' WHERE status='pending'")
        next_epoch = state["epoch"] + 1
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (challenge_id, row["nonce"], role, old, successor, next_epoch,
             now, now, "active", bytes(row["record"]), row["record_sha256"]),
        )
        db.execute(
            f"UPDATE state SET {role}_fp=?, epoch=?, revision=revision+1 WHERE singleton=1",
            (successor, next_epoch),
        )
        db.execute(
            "UPDATE recovery_challenges SET used_at=? WHERE challenge_id=?",
            (now, challenge_id),
        )
        return trust_state._snapshot(db, trust_state._state(db))
