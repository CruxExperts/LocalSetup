"""Schema inventory and creation for the private OpenPGP authority store."""

from __future__ import annotations

import re
import sqlite3


_SCHEMA = 2
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
