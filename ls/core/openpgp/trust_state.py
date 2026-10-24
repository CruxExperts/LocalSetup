"""Private, persistent authority for signed OpenPGP role transitions.

The application calling ``record_accepted_content`` is trusted to call it only
after successfully opening and authenticating the exact ciphertext. This
module does not infer that fact from a ciphertext digest.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from . import transition, publishing_transition
from .contracts import LocalTrust, normalize_fingerprint
from .keys import inspect_key, _MAX_CERTIFICATE_BYTES
from .trust_schema import (
    _SCHEMA, _TABLES, _V1_SQL, _V1_TABLES,
    _install_recovery_schema, _install_initial_schema, _normalized_sql,
)


if TYPE_CHECKING:
    from .native_input import NativeInputContext
    from .trust_initialization import PreparedAuthority


_ROLE = Literal["owner", "publisher"]
_MAX_CIPHERTEXT = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _ContainerCipherFile:
    """Still-open private ciphertext file plus authenticated expected digest."""

    source: object
    expected_sha256: str
    max_bytes: int
    budget: object


def _validate_v1(db: sqlite3.Connection) -> None:
    entries = db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL"
    ).fetchall()
    if {row["name"] for row in entries if row["type"] == "table"} != _V1_TABLES:
        raise TrustStateError("invalid version 1 trust schema")
    if {row["name"] for row in entries} != set(_V1_SQL):
        raise TrustStateError("unknown version 1 schema object")
    for row in entries:
        expected_type = "index" if row["name"] == "one_pending" else "table"
        if row["type"] != expected_type or _normalized_sql(row["sql"]) != _normalized_sql(_V1_SQL[row["name"]]):
            raise TrustStateError("malformed version 1 trust schema")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise TrustStateError("corrupt version 1 trust store")
    state = db.execute("SELECT * FROM state").fetchall()
    if len(state) != 1 or state[0]["singleton"] != 1:
        raise TrustStateError("invalid version 1 authority")
    selected = state[0]
    if (
        type(selected["store_id"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", selected["store_id"])
        or any(type(selected[key]) is not int or selected[key] < 1 for key in ("revision", "epoch"))
        or type(selected["last_observed"]) is not int or selected["last_observed"] < 0
    ):
        raise TrustStateError("invalid version 1 authority")
    try:
        if _scope(selected["scope"]) != transition._validate_scope(_scope(selected["scope"])):
            raise TrustStateError("invalid version 1 scope")
    except (TypeError, AttributeError, transition.TransitionError) as exc:
        raise TrustStateError("invalid version 1 scope") from exc
    for role in ("owner_fp", "publisher_fp"):
        fingerprint = selected[role]
        if fingerprint is not None and db.execute(
            "SELECT 1 FROM keys WHERE fingerprint=?", (fingerprint,)
        ).fetchone() is None:
            raise TrustStateError("missing version 1 role certificate")



def _migrate_v1(db: sqlite3.Connection) -> None:
    """Upgrade the exact shipped v1 store under one SQLite writer lock."""
    if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
        raise TrustStateError("unsupported version 1 journal mode")
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == _SCHEMA:
            db.commit()  # Another process completed the migration while waiting.
            return
        if version != 1:
            raise TrustStateError("unknown trust schema")
        _validate_v1(db)
        count = db.execute("SELECT count(*) FROM events").fetchone()[0]
        db.execute("""CREATE TABLE events_v2 (
            transition_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK(role IN ('owner','publisher')),
            old_fp TEXT, new_fp TEXT NOT NULL,
            epoch INTEGER NOT NULL, effective_at INTEGER NOT NULL,
            overlap_end INTEGER NOT NULL, status TEXT NOT NULL,
            record BLOB NOT NULL, record_sha256 TEXT NOT NULL
        )""")
        db.execute("INSERT INTO events_v2 SELECT * FROM events")
        if db.execute("SELECT count(*) FROM events_v2").fetchone()[0] != count:
            raise TrustStateError("version 1 event copy mismatch")
        db.execute("DROP TABLE events")
        db.execute("ALTER TABLE events_v2 RENAME TO events")
        db.execute("CREATE UNIQUE INDEX one_pending ON events(status) WHERE status='pending'")
        _install_recovery_schema(db)
        db.execute("PRAGMA user_version=2")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise TrustStateError("migrated trust store failed integrity check")
        db.commit()
    except Exception:
        db.rollback()
        raise


class TrustStateError(RuntimeError):
    """A private store is unsafe, stale, corrupt, or denies an operation."""


@dataclass(frozen=True, slots=True)
class Authority:
    """Pinned role authority; epoch is the store's current governance epoch.

    ``accepted_epoch`` is set only for an exact historical ciphertext receipt.
    It identifies the epoch in which that signer was accepted, which can differ
    from the current store epoch after unrelated role transitions.
    """

    role: _ROLE
    fingerprint: str
    certificate: bytes
    store_id: str
    revision: int
    epoch: int
    scope: tuple[str, ...]
    accepted_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class TrustSnapshot:
    store_id: str
    scope: tuple[str, ...]
    revision: int
    epoch: int
    owner: Authority | None
    publisher: Authority | None
    pending_transition_id: str | None


def _clock() -> int:
    return int(time.time())


def _path(path: str | os.PathLike[str], *, create: bool = False) -> Path:
    selected = Path(path)
    if not selected.is_absolute() or selected.name in {"", ".", ".."}:
        raise TrustStateError("store path must be absolute")
    # Refuse symlinked ancestors and a shared parent. SQLite needs permission
    # to create a rollback journal in this directory.
    for ancestor in selected.parents:
        try:
            ancestor_info = ancestor.lstat()
        except OSError as exc:
            raise TrustStateError("store ancestor unavailable") from exc
        mode = ancestor_info.st_mode
        if not stat.S_ISDIR(mode) or ancestor_info.st_uid not in (0, os.geteuid()):
            raise TrustStateError("unsafe store ancestor")
        if stat.S_IMODE(mode) & 0o022 and not (
            ancestor_info.st_uid == 0 and mode & stat.S_ISVTX
        ):
            raise TrustStateError("writable store ancestor")
    parent = selected.parent
    try:
        info = parent.lstat()
    except OSError as exc:
        raise TrustStateError("store parent unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise TrustStateError("store parent must be private and owned")
    if selected.is_symlink():
        raise TrustStateError("store symlink refused")
    if not create:
        try:
            info = selected.lstat()
        except OSError as exc:
            raise TrustStateError("store unavailable") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise TrustStateError("store must be a private regular file")
    return selected


def _connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    selected = _path(path)
    try:
        db = sqlite3.connect(selected.as_uri() + "?mode=rw", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            _migrate_v1(db)
            version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != _SCHEMA:
            raise TrustStateError("unknown trust schema")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if tables != _TABLES:
            raise TrustStateError("invalid trust schema or database")
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
            raise TrustStateError("unsupported trust journal mode")
        return db
    except Exception:
        if "db" in locals():
            db.close()
        raise


@contextmanager
def _open(path: str | os.PathLike[str]):
    db = _connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def _state(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute("SELECT * FROM state WHERE singleton=1").fetchone()
    if row is None:
        raise TrustStateError("missing trust state")
    return row


def _scope(raw: str) -> tuple[str, ...]:
    return tuple(raw.split("\n"))


def _require_scope(state: sqlite3.Row, expected_scope: tuple[str, ...]) -> None:
    if _scope(state["scope"]) != transition._validate_scope(expected_scope):
        raise TrustStateError("store scope mismatch")


def _certificate(db: sqlite3.Connection, fingerprint: str) -> bytes:
    row = db.execute("SELECT certificate FROM keys WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row is None:
        raise TrustStateError("missing pinned certificate")
    return bytes(row[0])


def _authority(db: sqlite3.Connection, state: sqlite3.Row, role: _ROLE,
               fingerprint: str | None, *, accepted_epoch: int | None = None) -> Authority | None:
    if fingerprint is None:
        return None
    return Authority(role, fingerprint, _certificate(db, fingerprint), state["store_id"],
                     state["revision"], state["epoch"], _scope(state["scope"]), accepted_epoch)


def _snapshot(db: sqlite3.Connection, state: sqlite3.Row) -> TrustSnapshot:
    pending = db.execute("SELECT transition_id FROM events WHERE status='pending'").fetchone()
    return TrustSnapshot(
        state["store_id"], _scope(state["scope"]), state["revision"], state["epoch"],
        _authority(db, state, "owner", state["owner_fp"]),
        _authority(db, state, "publisher", state["publisher_fp"]),
        pending[0] if pending else None,
    )


def _begin_fresh(db: sqlite3.Connection) -> tuple[sqlite3.Row, int]:
    """Read trusted time only after acquiring the SQLite writer lock."""
    db.execute("BEGIN IMMEDIATE")
    observed = _clock()
    return _observe(db, observed), observed


def _observe(db: sqlite3.Connection, observed: int) -> sqlite3.Row:
    state = _state(db)
    if observed < state["last_observed"]:
        raise TrustStateError("clock rollback")
    if observed > state["last_observed"]:
        db.execute("UPDATE state SET last_observed=? WHERE singleton=1", (observed,))
    return _state(db)


def _activate_due(db: sqlite3.Connection, now: int) -> sqlite3.Row:
    state = _state(db)
    event = db.execute("SELECT * FROM events WHERE status='pending'").fetchone()
    if event is None or event["effective_at"] > now:
        return state
    if event["epoch"] != state["epoch"] + 1 or state[event["role"] + "_fp"] != event["old_fp"]:
        raise TrustStateError("pending chain mismatch")
    if _is_revoked(db, event["role"], event["new_fp"]):
        raise TrustStateError("pending successor revoked")
    db.execute(
        f"UPDATE state SET {event['role']}_fp=?, epoch=?, revision=revision+1 WHERE singleton=1",
        (event["new_fp"], event["epoch"]),
    )
    db.execute("UPDATE events SET status='active' WHERE transition_id=?", (event["transition_id"],))
    return _state(db)


def _is_revoked(db: sqlite3.Connection, role: _ROLE, fingerprint: str) -> bool:
    return db.execute(
        "SELECT 1 FROM revocations WHERE role=? AND fingerprint=?", (role, fingerprint)
    ).fetchone() is not None


def _transactional_snapshot(path: str | os.PathLike[str], expected_scope: tuple[str, ...]) -> TrustSnapshot:
    with _open(path) as db:
        state, now = _begin_fresh(db)
        _require_scope(state, expected_scope)
        state = _activate_due(db, now)
        return _snapshot(db, state)


def initialize_trust_state(
    path: str | os.PathLike[str], *, scope: tuple[str, ...],
    owner_certificate: bytes, publisher_certificate: bytes,
    recovery_certificates: tuple[bytes, ...] = (),
    gpg_binary: str | os.PathLike[str] = "gpg",
    native_input: NativeInputContext | None = None,
) -> TrustSnapshot:
    """Create a private store once, optionally through exact native confirmation."""
    from .trust_initialization import initialize

    return initialize(path, scope=scope, owner_certificate=owner_certificate,
                      publisher_certificate=publisher_certificate,
                      recovery_certificates=recovery_certificates,
                      gpg_binary=gpg_binary, native_input=native_input)


def _prepare_initial_authority(
    *, scope: tuple[str, ...], owner_certificate: bytes, publisher_certificate: bytes,
    recovery_certificates: tuple[bytes, ...], gpg_binary: str | os.PathLike[str],
    checkpoint: Callable[[], None] = lambda: None,
) -> PreparedAuthority:
    from .trust_initialization import PreparedAuthority

    if any(type(certificate) is not bytes or not 0 < len(certificate) <= _MAX_CERTIFICATE_BYTES
           for certificate in (owner_certificate, publisher_certificate)):
        raise TrustStateError("certificates must be bounded nonempty bytes")
    if type(recovery_certificates) is not tuple or len(recovery_certificates) > 8:
        raise TrustStateError("invalid recovery enrollment")
    if any(type(certificate) is not bytes or not 0 < len(certificate) <= _MAX_CERTIFICATE_BYTES
           for certificate in recovery_certificates):
        raise TrustStateError("invalid recovery certificate")
    selected_scope = transition._validate_scope(scope)
    now = _clock()
    checkpoint()
    owner = inspect_key(certificate=owner_certificate, gpg_binary=gpg_binary)
    checkpoint()
    publisher = inspect_key(certificate=publisher_certificate, gpg_binary=gpg_binary)
    transition._validate_owner_usable(owner, now=now, effective_at=now, overlap_seconds=0)
    publishing_transition._validate_party(publisher, now=now, valid_until=now)
    owner_fp = owner.primary_fingerprint
    publisher_fp = publisher.primary_fingerprint
    if owner_fp == publisher_fp:
        raise TrustStateError("owner and publisher must be distinct")
    recovery_pins: dict[str, bytes] = {}
    recovery_inspections = []
    for certificate in recovery_certificates:
        checkpoint()
        inspection = inspect_key(certificate=certificate, gpg_binary=gpg_binary)
        publishing_transition._validate_party(inspection, now=now, valid_until=now)
        fingerprint = inspection.primary_fingerprint
        if fingerprint in {owner_fp, publisher_fp} or fingerprint in recovery_pins:
            raise TrustStateError("recovery key must be independent")
        recovery_pins[fingerprint] = certificate
        recovery_inspections.append(inspection)
    checkpoint()
    return PreparedAuthority(selected_scope, now, owner_certificate, publisher_certificate,
                             recovery_certificates, owner, publisher, tuple(recovery_inspections))


def _initial_database(
    scope: tuple[str, ...], now: int, owner_fp: str, publisher_fp: str,
    owner_certificate: bytes, publisher_certificate: bytes,
    recovery_pins: dict[str, bytes],
) -> tuple[TrustSnapshot, bytes]:
    """Serialize one committed schema without opening the destination pathname."""
    if not callable(getattr(sqlite3.Connection, "serialize", None)):
        raise TrustStateError("SQLite serialization unavailable")
    db = sqlite3.connect(":memory:", timeout=5.0)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        _install_initial_schema(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO state VALUES (1,?,?,?,?,?,?,?)",
            (secrets.token_hex(32), "\n".join(scope), 1, 1, now, owner_fp, publisher_fp),
        )
        db.executemany("INSERT INTO keys VALUES (?,?)", (
            (owner_fp, owner_certificate), (publisher_fp, publisher_certificate)
        ))
        db.executemany("INSERT INTO recovery_keys VALUES (?,?,?)", (
            (fingerprint, certificate, now)
            for fingerprint, certificate in recovery_pins.items()
        ))
        db.commit()
        return _snapshot(db, _state(db)), db.serialize()
    finally:
        db.close()


def load_trust_state(path: str | os.PathLike[str], *, expected_scope: tuple[str, ...]) -> TrustSnapshot:
    """Load current authority and resolve any due scheduled transition."""
    return _transactional_snapshot(path, expected_scope)


def _apply(
    path: str | os.PathLike[str], serialized: bytes, *, role: _ROLE,
    expected_store_id: str, expected_revision: int,
    gpg_binary: str | os.PathLike[str],
) -> TrustSnapshot:
    if type(serialized) is not bytes or not serialized or len(serialized) > 5_242_880:
        raise TrustStateError("invalid signed record")
    if type(expected_store_id) is not str or type(expected_revision) is not int:
        raise TrustStateError("expected store and revision required")
    now = _clock()
    # Snapshot read precedes expensive GnuPG work. The write transaction below
    # repeats every authority and revision check before consuming the record.
    with _open(path) as db:
        state = _state(db)
        if now < state["last_observed"]:
            raise TrustStateError("clock rollback")
        if state["store_id"] != expected_store_id or state["revision"] != expected_revision:
            raise TrustStateError("stale authority")
        if db.execute("SELECT 1 FROM events WHERE status='pending'").fetchone():
            raise TrustStateError("pending transition")
        if role == "owner":
            current = state["owner_fp"]
            if current is None:
                raise TrustStateError("owner revoked")
            verified = transition.verify_approved_transition(
                serialized, local_trust=LocalTrust({current}),
                known_transition_ids=(), known_nonces=(),
                current_owner_fingerprint=current, current_epoch=state["epoch"],
                now=now, gpg_binary=gpg_binary,
            )
            new = verified.proposed_owner_fingerprint
        else:
            current = state["publisher_fp"]
            owner = state["owner_fp"]
            if current is None or owner is None:
                raise TrustStateError("required role revoked")
            verified = publishing_transition.verify_approved_publishing_transition(
                serialized, current_owner_fingerprint=owner,
                current_publisher_fingerprint=current, current_epoch=state["epoch"],
                expected_scope=_scope(state["scope"]), local_trust=LocalTrust({owner, current}),
                known_transition_ids=(), known_nonces=(), now=now, gpg_binary=gpg_binary,
            )
            new = verified.proposed_publisher_fingerprint
        if tuple(verified.scope) != _scope(state["scope"]):
            raise TrustStateError("scope mismatch")
        if new == current:
            raise TrustStateError("unchanged role")
        if _is_revoked(db, role, new):
            raise TrustStateError("proposed role was revoked")
        if db.execute("SELECT 1 FROM recovery_keys WHERE fingerprint=?", (new,)).fetchone():
            raise TrustStateError("recovery signer cannot become a routine role")
        # The authenticated canonical record contains the complete new pin.
        entry = transition._record_object(
            transition.ApprovedTransitionRecord.from_bytes(serialized).record
        ) if role == "owner" else publishing_transition._record_object(
            publishing_transition.ApprovedPublishingTransitionRecord.from_bytes(serialized).record
        )
        key_name = "proposed_owner" if role == "owner" else "proposed_publisher"
        cert = transition._certificate_from_owner_record(entry[key_name])
        with _open(path) as write_db:
            try:
                fresh, transaction_now = _begin_fresh(write_db)
                if transaction_now < now:
                    raise TrustStateError("clock rollback during verification")
                fresh = _activate_due(write_db, transaction_now)
                if (fresh["store_id"] != expected_store_id
                    or fresh["revision"] != expected_revision
                    or fresh["epoch"] + 1 != verified.epoch
                    or fresh[role + "_fp"] != current
                    or (role == "publisher" and fresh["owner_fp"] != owner)
                    or write_db.execute("SELECT 1 FROM events WHERE status='pending'").fetchone()):
                    raise TrustStateError("stale transition")
                # The signed record was authenticated before locking. Recheck
                # its admission window against time observed under the lock.
                transition._validate_time_window(
                    verified.effective_at, verified.overlap_seconds,
                    transaction_now, allow_overlap_verification=True,
                )
                if _is_revoked(write_db, role, new):
                    raise TrustStateError("proposed role was revoked")
                if write_db.execute("SELECT 1 FROM recovery_keys WHERE fingerprint=?", (new,)).fetchone():
                    raise TrustStateError("recovery signer cannot become a routine role")
                write_db.execute("INSERT INTO consumed VALUES ('id',?)", (verified.transition_id,))
                write_db.execute("INSERT INTO consumed VALUES ('nonce',?)", (verified.nonce,))
                write_db.execute("INSERT OR IGNORE INTO keys VALUES (?,?)", (new, cert))
                if _certificate(write_db, new) != cert:
                    raise TrustStateError("pinned certificate mismatch")
                pending = verified.effective_at > transaction_now
                write_db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (verified.transition_id, verified.nonce, role, current, new, verified.epoch,
                     verified.effective_at, verified.effective_at + verified.overlap_seconds,
                     "pending" if pending else "active", serialized, hashlib.sha256(serialized).hexdigest()),
                )
                if pending:
                    write_db.execute("UPDATE state SET revision=revision+1 WHERE singleton=1")
                else:
                    write_db.execute(
                        f"UPDATE state SET {role}_fp=?, epoch=?, revision=revision+1 WHERE singleton=1",
                        (new, verified.epoch),
                    )
                return _snapshot(write_db, _state(write_db))
            except sqlite3.IntegrityError as exc:
                raise TrustStateError("transition replay or conflict") from exc


def apply_owner_transition(path: str | os.PathLike[str], serialized: bytes, *,
                           expected_store_id: str, expected_revision: int,
                           gpg_binary: str | os.PathLike[str] = "gpg") -> TrustSnapshot:
    return _apply(path, serialized, role="owner", expected_store_id=expected_store_id,
                  expected_revision=expected_revision, gpg_binary=gpg_binary)


def apply_publisher_transition(path: str | os.PathLike[str], serialized: bytes, *,
                               expected_store_id: str, expected_revision: int,
                               gpg_binary: str | os.PathLike[str] = "gpg") -> TrustSnapshot:
    return _apply(path, serialized, role="publisher", expected_store_id=expected_store_id,
                  expected_revision=expected_revision, gpg_binary=gpg_binary)


def authorize_outbound(path: str | os.PathLike[str], *, expected_scope: tuple[str, ...],
                       role: _ROLE = "publisher") -> Authority:
    snapshot = load_trust_state(path, expected_scope=expected_scope)
    result = snapshot.owner if role == "owner" else snapshot.publisher if role == "publisher" else None
    if result is None:
        raise TrustStateError("role has no active authority")
    return result


def authorize_inbound_peer(path: str | os.PathLike[str], *, role: _ROLE,
                           fingerprint: str, expected_scope: tuple[str, ...]) -> Authority:
    """Authorize active or immediate recorded predecessor during overlap."""
    if role not in {"owner", "publisher"}:
        raise TrustStateError("invalid role")
    fp = normalize_fingerprint(fingerprint)
    with _open(path) as db:
        state, now = _begin_fresh(db)
        _require_scope(state, expected_scope)
        state = _activate_due(db, now)
        active = state[role + "_fp"]
        if _is_revoked(db, role, fp):
            raise TrustStateError("peer authority revoked")
        if active == fp:
            return _authority(db, state, role, fp)  # type: ignore[return-value]
        event = db.execute(
            "SELECT old_fp FROM events WHERE role=? AND new_fp=? AND status='active' "
            "AND effective_at<=? AND overlap_end>? ORDER BY epoch DESC LIMIT 1",
            (role, active, now, now),
        ).fetchone()
        if event is None or event["old_fp"] != fp:
            raise TrustStateError("peer is not current or overlapping predecessor")
        return _authority(db, state, role, fp)  # type: ignore[return-value]


def record_accepted_content(path: str | os.PathLike[str], ciphertext: bytes, *,
                            signer_fingerprint: str, role: _ROLE,
                            expected_store_id: str, expected_revision: int,
                            expected_scope: tuple[str, ...]) -> str:
    """Record exact authenticated ciphertext after a trusted opener accepts it."""
    if type(ciphertext) is not bytes or not ciphertext or len(ciphertext) > _MAX_CIPHERTEXT:
        raise TrustStateError("invalid ciphertext")
    if role not in {"owner", "publisher"}:
        raise TrustStateError("invalid role")
    signer = normalize_fingerprint(signer_fingerprint)
    digest = hashlib.sha256(ciphertext).hexdigest()
    return _record_accepted_content_digest(
        path, digest, signer=signer, role=role,
        expected_store_id=expected_store_id, expected_revision=expected_revision,
        expected_scope=expected_scope,
    )



def _record_accepted_content_digest(
    path: str | os.PathLike[str], digest: str, *, signer: str, role: _ROLE,
    expected_store_id: str, expected_revision: int, expected_scope: tuple[str, ...],
    verified_source: object | None = None, max_bytes: int | None = None,
    release_payload: Callable[[], None] | None = None,
    rollback_payload: Callable[[], None] | None = None,
    budget: object | None = None,
) -> str:
    # The digest is private to trusted byte verification or an anchored file
    # rehashed below. Never expose this helper as an untrusted digest API.
    if verified_source is not None:
        from .envelope_io import AnchoredFile

        if not isinstance(verified_source, AnchoredFile) or type(max_bytes) is not int:
            raise TrustStateError("invalid verified source")
        if not verified_source.unchanged():
            raise TrustStateError("verified source changed")
    if (release_payload is None) != (rollback_payload is None):
        raise TrustStateError("incomplete output callback")
    released = False
    try:
        with _open(path) as db:
            state, now = _begin_fresh(db)
            _require_scope(state, expected_scope)
            state = _activate_due(db, now)
            if verified_source is not None and not verified_source.unchanged():
                raise TrustStateError("verified source changed")
            if state["store_id"] != expected_store_id or state["revision"] != expected_revision:
                raise TrustStateError("stale receipt authority")
            event_id, event_epoch = _accepted_receipt_authority(
                db, state, now, role=role, signer=signer
            )
            existing = db.execute("SELECT * FROM receipts WHERE ciphertext_sha256=?", (digest,)).fetchone()
            if existing is not None and (
                existing["signer_fp"] != signer or existing["role"] != role
            ):
                raise TrustStateError("receipt collision")
            if budget is not None:
                budget.checkpoint()
            if release_payload is not None:
                release_payload()
                released = True
            if budget is not None:
                budget.checkpoint()
            if existing is None:
                db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                           (digest, signer, role, event_epoch, event_id, now))
        return digest
    except Exception:
        if released and rollback_payload is not None:
            try:
                rollback_payload()
            except Exception:
                pass
        raise



def _record_accepted_content_file(
    path: str | os.PathLike[str], source: object, *,
    signer_fingerprint: str, role: _ROLE, expected_store_id: str,
    expected_revision: int, expected_scope: tuple[str, ...], max_bytes: int,
    release_payload: Callable[[], None], rollback_payload: Callable[[], None],
    budget: object,
) -> str:
    """Receipt a still-anchored file after a trusted opener verified its bytes."""
    from . import envelope as _envelope
    from .envelope_io import AnchoredFile, OperationBudget, hash_file

    if (
        role not in {"owner", "publisher"}
        or not isinstance(source, AnchoredFile)
        or not isinstance(budget, OperationBudget)
    ):
        raise TrustStateError("invalid verified source")
    try:
        digest, length = hash_file(source, offset=0, maximum=max_bytes, budget=budget)
    except _envelope.EnvelopeError:
        raise
    except Exception:
        raise TrustStateError("verified source changed") from None
    if length == 0:
        raise TrustStateError("invalid verified source")
    return _record_accepted_content_digest(
        path, digest, signer=normalize_fingerprint(signer_fingerprint), role=role,
        expected_store_id=expected_store_id, expected_revision=expected_revision,
        expected_scope=expected_scope, verified_source=source, max_bytes=max_bytes,
        release_payload=release_payload, rollback_payload=rollback_payload,
        budget=budget,
    )



def _accepted_receipt_authority(
    db: sqlite3.Connection,
    state: sqlite3.Row,
    now: int,
    *,
    role: _ROLE,
    signer: str,
) -> tuple[str | None, int]:
    if _is_revoked(db, role, signer):
        raise TrustStateError("signer authority revoked")
    if signer != state[role + "_fp"]:
        event = db.execute(
            "SELECT transition_id, epoch FROM events WHERE role=? AND new_fp=? AND old_fp=? "
            "AND status='active' AND effective_at<=? AND overlap_end>? "
            "ORDER BY epoch DESC LIMIT 1",
            (role, state[role + "_fp"], signer, now, now),
        ).fetchone()
        if event is None:
            raise TrustStateError("signer is not authorized")
        return event["transition_id"], event["epoch"] - 1
    event = db.execute(
        "SELECT transition_id, epoch FROM events WHERE role=? AND new_fp=? "
        "AND status='active' ORDER BY epoch DESC LIMIT 1",
        (role, signer),
    ).fetchone()
    return (event["transition_id"], event["epoch"]) if event else (None, 1)


def authorize_historical_content(path: str | os.PathLike[str], ciphertext: bytes, *,
                                 signer_fingerprint: str, role: _ROLE,
                                 expected_scope: tuple[str, ...]) -> Authority:
    """Authorize only an exact ciphertext with an existing accepted receipt."""
    if type(ciphertext) is not bytes or not ciphertext or len(ciphertext) > _MAX_CIPHERTEXT:
        raise TrustStateError("invalid ciphertext")
    if role not in {"owner", "publisher"}:
        raise TrustStateError("invalid role")
    fp = normalize_fingerprint(signer_fingerprint)
    digest = hashlib.sha256(ciphertext).hexdigest()
    return _authorize_historical_digest(
        path, digest, fingerprint=fp, role=role, expected_scope=expected_scope,
    )



def _authorize_historical_digest(
    path: str | os.PathLike[str], digest: str, *, fingerprint: str,
    role: _ROLE, expected_scope: tuple[str, ...],
) -> Authority:
    with _open(path) as db:
        state, now = _begin_fresh(db)
        _require_scope(state, expected_scope)
        state = _activate_due(db, now)
        receipt = db.execute(
            "SELECT * FROM receipts WHERE ciphertext_sha256=? AND signer_fp=? AND role=?",
            (digest, fingerprint, role),
        ).fetchone()
        if receipt is None:
            raise TrustStateError("no exact accepted-content receipt")
        if receipt["event_id"] is not None:
            event = db.execute("SELECT * FROM events WHERE transition_id=?", (receipt["event_id"],)).fetchone()
            valid_chain = event is not None and event["role"] == role and (
                (event["new_fp"] == fingerprint and receipt["epoch"] == event["epoch"])
                or (event["old_fp"] == fingerprint and receipt["epoch"] == event["epoch"] - 1)
            )
            if not valid_chain:
                raise TrustStateError("broken historical chain")
        elif db.execute("SELECT 1 FROM keys WHERE fingerprint=?", (fingerprint,)).fetchone() is None:
            raise TrustStateError("missing original pin")
        return _authority(db, state, role, fingerprint, accepted_epoch=receipt["epoch"])  # type: ignore[return-value]



def _authorize_historical_content_file(
    path: str | os.PathLike[str], ciphertext_path: str | os.PathLike[str], *,
    signer_fingerprint: str, role: _ROLE, expected_scope: tuple[str, ...],
    max_bytes: int, budget: object,
) -> tuple[Authority, str]:
    from . import envelope as _envelope
    from .envelope_io import AnchoredFile, OperationBudget, hash_file

    if role not in {"owner", "publisher"} or not isinstance(budget, OperationBudget):
        raise TrustStateError("invalid role")
    try:
        with AnchoredFile(ciphertext_path) as source:
            digest, length = hash_file(source, offset=0, maximum=max_bytes, budget=budget)
            if length == 0:
                raise TrustStateError("invalid ciphertext")
            authority = _authorize_historical_digest(
                path, digest, fingerprint=normalize_fingerprint(signer_fingerprint),
                role=role, expected_scope=expected_scope,
            )
            if not source.unchanged():
                raise TrustStateError("historical source changed")
            budget.checkpoint()
            return authority, digest
    except (TrustStateError, _envelope.EnvelopeError):
        raise
    except Exception:
        raise TrustStateError("invalid historical source") from None


def _record_accepted_container(
    path: str | os.PathLike[str], *,
    identity_prefix: str,
    metadata_identity: str | None,
    layers: tuple[tuple[bytes | _ContainerCipherFile, _ROLE, str], ...],
    expected_store_id: str,
    expected_revision: int,
    expected_epoch: int,
    expected_scope: tuple[str, ...],
    created_at: int,
    expires_at: int,
    admit_from: int | None = None,
    admit_until: int | None = None,
    max_content_age_seconds: int | None = None,
    max_content_lifetime_seconds: int | None = None,
    release_payload: Callable[[], None],
    rollback_payload: Callable[[], None],
) -> tuple[str, str]:
    """Record metadata or a selected message under one fresh writer lock.

    The consumed identity binds exact protected layers. A selected message also
    requires its metadata observation in this same local store. No carrier
    locator or caller-provided verified-metadata object grants authority.
    """
    if (
        type(identity_prefix) is not str or not identity_prefix
        or len(identity_prefix) > 512 or any(c not in "0123456789abcdef:" for c in identity_prefix)
        or (metadata_identity is not None and (type(metadata_identity) is not str or not metadata_identity))
        or type(layers) is not tuple or not 1 <= len(layers) <= 3
        or type(created_at) is not int or type(expires_at) is not int
        or created_at < 0 or expires_at <= created_at
        or (any(value is not None for value in (
            admit_from, admit_until, max_content_age_seconds, max_content_lifetime_seconds,
        )) and not all(type(value) is int for value in (
            admit_from, admit_until, max_content_age_seconds, max_content_lifetime_seconds,
        )))
        or not callable(release_payload) or not callable(rollback_payload)
    ):
        raise TrustStateError("invalid container acceptance")
    from .envelope_io import AnchoredFile, OperationBudget, hash_file

    normalized: list[tuple[str, _ROLE, str, _ContainerCipherFile | None]] = []
    for ciphertext, role, fingerprint in layers:
        if role not in {"owner", "publisher"}:
            raise TrustStateError("invalid container acceptance")
        source_ref: _ContainerCipherFile | None = None
        if type(ciphertext) is bytes:
            if not ciphertext or len(ciphertext) > _MAX_CIPHERTEXT:
                raise TrustStateError("invalid container acceptance")
            digest = hashlib.sha256(ciphertext).hexdigest()
        elif isinstance(ciphertext, _ContainerCipherFile):
            if (not isinstance(ciphertext.source, AnchoredFile)
                    or not isinstance(ciphertext.budget, OperationBudget)
                    or type(ciphertext.max_bytes) is not int or ciphertext.max_bytes <= 0
                    or re.fullmatch(r"[0-9a-f]{64}", ciphertext.expected_sha256) is None):
                raise TrustStateError("invalid container acceptance")
            digest = ciphertext.expected_sha256
            source_ref = ciphertext
        else:
            raise TrustStateError("invalid container acceptance")
        try:
            signer = normalize_fingerprint(fingerprint)
        except Exception:
            raise TrustStateError("invalid container acceptance") from None
        normalized.append((digest, role, signer, source_ref))
    kind = "container-selected-v1" if metadata_identity is not None else "container-meta-v1"
    identity = identity_prefix + ":" + ":".join(item[0] for item in normalized)
    released = False
    try:
        with _open(path) as db:
            state, now = _begin_fresh(db)
            _require_scope(state, expected_scope)
            state = _activate_due(db, now)
            if not created_at <= now < expires_at:
                raise TrustStateError("container outside freshness window")
            if admit_from is not None:
                assert admit_until is not None
                assert max_content_age_seconds is not None
                assert max_content_lifetime_seconds is not None
                if not (admit_from <= created_at <= now < admit_until
                        and now - created_at <= max_content_age_seconds
                        and expires_at - created_at <= max_content_lifetime_seconds):
                    raise TrustStateError("container outside discovery admission")
            if (state["store_id"] != expected_store_id or state["revision"] != expected_revision
                    or state["epoch"] != expected_epoch):
                raise TrustStateError("stale container authority")
            for digest, _role, _signer, source_ref in normalized:
                if source_ref is not None:
                    actual, length = hash_file(source_ref.source, offset=0,
                                               maximum=source_ref.max_bytes,
                                               budget=source_ref.budget)
                    if not length or actual != digest:
                        raise TrustStateError("selected ciphertext changed")
            if metadata_identity is not None and db.execute(
                "SELECT 1 FROM consumed WHERE kind='container-meta-v1' AND value=?",
                (metadata_identity,),
            ).fetchone() is None:
                raise TrustStateError("metadata receipt missing")
            existing = db.execute(
                "SELECT value FROM consumed WHERE kind=? AND substr(value,1,?)=?",
                (kind, len(identity_prefix) + 1, identity_prefix + ":"),
            ).fetchone()
            if existing is not None and existing["value"] != identity:
                raise TrustStateError("container replay conflict")
            for digest, role, signer, _source_ref in normalized:
                if state[role + "_fp"] != signer:
                    raise TrustStateError("container signer is not current")
                event_id, accepted_epoch = _accepted_receipt_authority(
                    db, state, now, role=role, signer=signer,
                )
                receipt = db.execute(
                    "SELECT signer_fp,role FROM receipts WHERE ciphertext_sha256=?", (digest,)
                ).fetchone()
                if receipt is not None and (receipt["signer_fp"] != signer or receipt["role"] != role):
                    raise TrustStateError("container receipt collision")
                if receipt is None:
                    db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                               (digest, signer, role, accepted_epoch, event_id, now))
            if existing is None or metadata_identity is not None:
                release_payload()
                released = True
            for _digest, _role, _signer, source_ref in normalized:
                if source_ref is not None and not source_ref.source.unchanged():
                    raise TrustStateError("selected ciphertext changed")
            if existing is None:
                db.execute("INSERT INTO consumed VALUES (?,?)", (kind, identity))
        return ("already_accepted" if existing is not None else "accepted", identity)
    except Exception:
        if released:
            try:
                rollback_payload()
            except Exception:
                pass
        raise


def _has_container_receipts(
    path: str | os.PathLike[str], *, expected_scope: tuple[str, ...],
    metadata_identity: str, selected_identity: str,
    layers: tuple[tuple[bytes | str, _ROLE, str], ...],
) -> None:
    """Require exact metadata and selected identities for historical opening."""
    with _open(path) as db:
        state, now = _begin_fresh(db)
        _require_scope(state, expected_scope)
        _activate_due(db, now)
        for kind, identity in (("container-meta-v1", metadata_identity),
                               ("container-selected-v1", selected_identity)):
            if db.execute("SELECT 1 FROM consumed WHERE kind=? AND value=?",
                          (kind, identity)).fetchone() is None:
                raise TrustStateError("exact container receipt missing")
    for ciphertext, role, fingerprint in layers:
        if type(ciphertext) is bytes:
            authorize_historical_content(
                path, ciphertext, signer_fingerprint=fingerprint, role=role,
                expected_scope=expected_scope,
            )
        elif type(ciphertext) is str and re.fullmatch(r"[0-9a-f]{64}", ciphertext):
            _authorize_historical_digest(
                path, ciphertext, fingerprint=normalize_fingerprint(fingerprint),
                role=role, expected_scope=expected_scope,
            )
        else:
            raise TrustStateError("invalid historical container layer")


def _authorize_cached_container_metadata(
    path: str | os.PathLike[str], *, identity_prefix: str,
    layers: tuple[tuple[bytes, _ROLE, str], ...],
    expected_store_id: str, expected_revision: int, expected_epoch: int,
    expected_scope: tuple[str, ...], created_at: int, expires_at: int,
    admit_from: int | None = None, admit_until: int | None = None,
    max_content_age_seconds: int | None = None,
    max_content_lifetime_seconds: int | None = None,
) -> None:
    """Reauthorize exact prior metadata receipts under the current writer clock.

    This lookup never creates receipts from caller-supplied cached claims.
    """
    if (type(identity_prefix) is not str or not identity_prefix
            or len(identity_prefix) > 512
            or any(c not in "0123456789abcdef:" for c in identity_prefix)
            or type(layers) is not tuple or len(layers) not in {1, 2}
            or type(created_at) is not int or type(expires_at) is not int
            or created_at < 0 or expires_at <= created_at):
        raise TrustStateError("invalid cached container metadata")
    normalized: list[tuple[str, _ROLE, str]] = []
    for ciphertext, role, fingerprint in layers:
        if (type(ciphertext) is not bytes or not ciphertext
                or len(ciphertext) > _MAX_CIPHERTEXT
                or role not in {"owner", "publisher"}):
            raise TrustStateError("invalid cached container layer")
        normalized.append((hashlib.sha256(ciphertext).hexdigest(), role,
                           normalize_fingerprint(fingerprint)))
    if [item[1] for item in normalized] not in (["publisher"], ["owner", "publisher"]):
        raise TrustStateError("invalid cached container layer order")
    identity = identity_prefix + ":" + ":".join(item[0] for item in normalized)
    with _open(path) as db:
        state, now = _begin_fresh(db)
        _require_scope(state, expected_scope)
        state = _activate_due(db, now)
        if not created_at <= now < expires_at:
            raise TrustStateError("cached metadata expired")
        if admit_from is not None:
            if (not all(type(value) is int for value in (
                    admit_until, max_content_age_seconds, max_content_lifetime_seconds))
                    or not (admit_from <= created_at <= now < admit_until
                            and now - created_at <= max_content_age_seconds
                            and expires_at - created_at <= max_content_lifetime_seconds)):
                raise TrustStateError("cached metadata outside discovery admission")
        if (state["store_id"] != expected_store_id
                or state["revision"] != expected_revision
                or state["epoch"] != expected_epoch):
            raise TrustStateError("stale cached metadata authority")
        if db.execute(
            "SELECT 1 FROM consumed WHERE kind='container-meta-v1' AND value=?",
            (identity,),
        ).fetchone() is None:
            raise TrustStateError("exact cached metadata receipt missing")
        for digest, role, signer in normalized:
            if state[role + "_fp"] != signer or _is_revoked(db, role, signer):
                raise TrustStateError("cached metadata signer is not current")
            receipt = db.execute(
                "SELECT 1 FROM receipts WHERE ciphertext_sha256=? AND signer_fp=? AND role=?",
                (digest, signer, role),
            ).fetchone()
            if receipt is None:
                raise TrustStateError("cached metadata layer receipt missing")


def revoke_authority(path: str | os.PathLike[str], *, role: _ROLE,
                     expected_store_id: str, expected_revision: int,
                     expected_scope: tuple[str, ...],
                     fingerprint: str | None = None) -> TrustSnapshot:
    """Trusted local operator action; callers must not expose it to model/RPC input."""
    if role not in {"owner", "publisher"}:
        raise TrustStateError("invalid role")
    with _open(path) as db:
        state, now = _begin_fresh(db)
        _require_scope(state, expected_scope)
        state = _activate_due(db, now)
        if state["store_id"] != expected_store_id or state["revision"] != expected_revision:
            raise TrustStateError("stale revocation")
        selected = (normalize_fingerprint(fingerprint) if fingerprint is not None
                    else state[role + "_fp"])
        if selected is None or _is_revoked(db, role, selected):
            raise TrustStateError("already revoked")
        known = selected == state[role + "_fp"] or db.execute(
            "SELECT 1 FROM events WHERE role=? AND (old_fp=? OR new_fp=?) LIMIT 1",
            (role, selected, selected),
        ).fetchone() is not None
        if not known:
            raise TrustStateError("fingerprint was never trusted for role")
        db.execute("INSERT INTO revocations VALUES (?,?,?)", (role, selected, now))
        db.execute("UPDATE events SET status='cancelled' WHERE status='pending'")
        if selected == state[role + "_fp"]:
            db.execute(f"UPDATE state SET {role}_fp=NULL, epoch=epoch+1, revision=revision+1 WHERE singleton=1")
        else:
            db.execute("UPDATE state SET epoch=epoch+1, revision=revision+1 WHERE singleton=1")
        return _snapshot(db, _state(db))
