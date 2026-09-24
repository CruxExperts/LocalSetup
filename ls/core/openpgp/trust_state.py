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
from typing import Literal

from . import transition, publishing_transition
from .contracts import LocalTrust, normalize_fingerprint
from .keys import inspect_key


_SCHEMA = 2
_ROLE = Literal["owner", "publisher"]
_MAX_CIPHERTEXT = 32 * 1024 * 1024
_TABLES = frozenset({
    "state", "keys", "events", "consumed", "receipts", "revocations",
    "recovery_keys", "recovery_challenges",
})
_V1_TABLES = frozenset({"state", "keys", "events", "consumed", "receipts", "revocations"})
_V1_SQL = {
    "state": """CREATE TABLE state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        store_id TEXT NOT NULL, scope TEXT NOT NULL,
        revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
        last_observed INTEGER NOT NULL,
        owner_fp TEXT, publisher_fp TEXT
    )""",
    "keys": "CREATE TABLE keys (fingerprint TEXT PRIMARY KEY, certificate BLOB NOT NULL)",
    "events": """CREATE TABLE events (
        transition_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL CHECK(role IN ('owner','publisher')),
        old_fp TEXT NOT NULL, new_fp TEXT NOT NULL,
        epoch INTEGER NOT NULL, effective_at INTEGER NOT NULL,
        overlap_end INTEGER NOT NULL, status TEXT NOT NULL,
        record BLOB NOT NULL, record_sha256 TEXT NOT NULL
    )""",
    "one_pending": "CREATE UNIQUE INDEX one_pending ON events(status) WHERE status='pending'",
    "consumed": "CREATE TABLE consumed (kind TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(kind,value))",
    "receipts": """CREATE TABLE receipts (
        ciphertext_sha256 TEXT PRIMARY KEY, signer_fp TEXT NOT NULL,
        role TEXT NOT NULL, epoch INTEGER NOT NULL,
        event_id TEXT, accepted_at INTEGER NOT NULL
    )""",
    "revocations": """CREATE TABLE revocations (
        role TEXT NOT NULL CHECK(role IN ('owner','publisher')),
        fingerprint TEXT NOT NULL, revoked_at INTEGER NOT NULL,
        PRIMARY KEY(role,fingerprint)
    )""",
}


def _normalized_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


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


def _install_recovery_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE recovery_keys (
        fingerprint TEXT PRIMARY KEY, certificate BLOB NOT NULL,
        enrolled_at INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE recovery_challenges (
        challenge_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE,
        record BLOB NOT NULL, record_sha256 TEXT NOT NULL,
        store_id TEXT NOT NULL, scope TEXT NOT NULL,
        revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('owner','publisher')),
        current_fp TEXT, successor_fp TEXT NOT NULL,
        successor_certificate BLOB NOT NULL,
        successor_certificate_sha256 TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        candidate_signature BLOB, local_authorized_at INTEGER,
        used_at INTEGER
    )""")


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
) -> TrustSnapshot:
    """Create a new private store exactly once; never overwrite one."""
    if type(owner_certificate) is not bytes or type(publisher_certificate) is not bytes:
        raise TrustStateError("certificates must be bytes")
    selected_scope = transition._validate_scope(scope)
    now = _clock()
    owner = inspect_key(certificate=owner_certificate, gpg_binary=gpg_binary)
    publisher = inspect_key(certificate=publisher_certificate, gpg_binary=gpg_binary)
    transition._validate_owner_usable(owner, now=now, effective_at=now, overlap_seconds=0)
    publishing_transition._validate_party(publisher, now=now, valid_until=now)
    owner_fp = owner.primary_fingerprint
    publisher_fp = publisher.primary_fingerprint
    if owner_fp == publisher_fp:
        raise TrustStateError("owner and publisher must be distinct")
    if type(recovery_certificates) is not tuple or len(recovery_certificates) > 8:
        raise TrustStateError("invalid recovery enrollment")
    recovery_pins: dict[str, bytes] = {}
    for certificate in recovery_certificates:
        if type(certificate) is not bytes:
            raise TrustStateError("invalid recovery certificate")
        inspection = inspect_key(certificate=certificate, gpg_binary=gpg_binary)
        publishing_transition._validate_party(inspection, now=now, valid_until=now)
        fingerprint = inspection.primary_fingerprint
        if fingerprint in {owner_fp, publisher_fp} or fingerprint in recovery_pins:
            raise TrustStateError("recovery key must be independent")
        recovery_pins[fingerprint] = certificate
    selected = _path(path, create=True)
    try:
        fd = os.open(selected, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except OSError as exc:
        raise TrustStateError("store already exists or cannot be created") from exc
    try:
        db = sqlite3.connect(selected, timeout=5.0)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
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
                    old_fp TEXT, new_fp TEXT NOT NULL,
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
                CREATE TABLE recovery_keys (
                    fingerprint TEXT PRIMARY KEY, certificate BLOB NOT NULL,
                    enrolled_at INTEGER NOT NULL
                );
                CREATE TABLE recovery_challenges (
                    challenge_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE,
                    record BLOB NOT NULL, record_sha256 TEXT NOT NULL,
                    store_id TEXT NOT NULL, scope TEXT NOT NULL,
                    revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('owner','publisher')),
                    current_fp TEXT, successor_fp TEXT NOT NULL,
                    successor_certificate BLOB NOT NULL,
                    successor_certificate_sha256 TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    candidate_signature BLOB, local_authorized_at INTEGER,
                    used_at INTEGER
                );
                PRAGMA user_version=2;
            """)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO state VALUES (1,?,?,?,?,?,?,?)",
                (secrets.token_hex(32), "\n".join(selected_scope), 1, 1, now, owner_fp, publisher_fp),
            )
            db.executemany("INSERT INTO keys VALUES (?,?)", (
                (owner_fp, owner_certificate), (publisher_fp, publisher_certificate)
            ))
            db.executemany("INSERT INTO recovery_keys VALUES (?,?,?)", (
                (fingerprint, certificate, now)
                for fingerprint, certificate in recovery_pins.items()
            ))
            db.commit()
            return _snapshot(db, _state(db))
        finally:
            db.close()
    except Exception:
        selected.unlink(missing_ok=True)
        raise


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
    with _open(path) as db:
        state, now = _begin_fresh(db)
        _require_scope(state, expected_scope)
        state = _activate_due(db, now)
        if state["store_id"] != expected_store_id or state["revision"] != expected_revision:
            raise TrustStateError("stale receipt authority")
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
            event_id = event[0]
            event_epoch = event["epoch"] - 1
        else:
            event = db.execute(
                "SELECT transition_id, epoch FROM events WHERE role=? AND new_fp=? "
                "AND status='active' ORDER BY epoch DESC LIMIT 1",
                (role, signer),
            ).fetchone()
            event_id = event[0] if event else None
            event_epoch = event["epoch"] if event else 1
        existing = db.execute("SELECT * FROM receipts WHERE ciphertext_sha256=?", (digest,)).fetchone()
        if existing is not None:
            if existing["signer_fp"] != signer or existing["role"] != role:
                raise TrustStateError("receipt collision")
            return digest
        db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                   (digest, signer, role, event_epoch, event_id, now))
        return digest


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
    with _open(path) as db:
        state, now = _begin_fresh(db)
        _require_scope(state, expected_scope)
        state = _activate_due(db, now)
        receipt = db.execute(
            "SELECT * FROM receipts WHERE ciphertext_sha256=? AND signer_fp=? AND role=?",
            (digest, fp, role),
        ).fetchone()
        if receipt is None:
            raise TrustStateError("no exact accepted-content receipt")
        if receipt["event_id"] is not None:
            event = db.execute("SELECT * FROM events WHERE transition_id=?", (receipt["event_id"],)).fetchone()
            valid_chain = event is not None and event["role"] == role and (
                (event["new_fp"] == fp and receipt["epoch"] == event["epoch"])
                or (event["old_fp"] == fp and receipt["epoch"] == event["epoch"] - 1)
            )
            if not valid_chain:
                raise TrustStateError("broken historical chain")
        elif db.execute("SELECT 1 FROM keys WHERE fingerprint=?", (fp,)).fetchone() is None:
            raise TrustStateError("missing original pin")
        return _authority(db, state, role, fp, accepted_epoch=receipt["epoch"])  # type: ignore[return-value]


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
