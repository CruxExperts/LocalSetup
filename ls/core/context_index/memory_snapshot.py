from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .common import ContextIndexError, Runtime, stable_json_hash
from .embeddings import embedding_profile
from .memory_records import (
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    MAX_BUNDLE_BYTES,
    MAX_CONTENT_BYTES,
    MAX_RECORDS,
    MAX_RECORD_LINEAGE_ENTRIES,
    MAX_TOTAL_CONTENT_BYTES,
    MAX_TOTAL_RECORD_LINEAGE_ENTRIES,
    SNAPSHOT_TRUST_WARNING,
    _database_role,
    _normalize_bundle_record,
    _persist_record,
    _profile,
    _record_from_row,
    _snapshot_hash,
    _snapshot_records,
    _sync_fts,
    _utf8_size,
    _validate_hash,
    _validate_record,
    memory_context,
    strict_json_object,
)
from .storage import connect



def _bundle_payload(rt: Runtime, con: Any) -> dict[str, Any]:
    context = memory_context(rt)
    context_key = context["context_key"]
    role = _database_role(con)
    if role is None or role[0] != "writer":
        raise ContextIndexError("MEMORY_EXPORT_WRITER_REQUIRED", "Only the central writer can export a replica snapshot.")
    state = con.execute("SELECT * FROM memory_context_state WHERE context_key=?", (context_key,)).fetchone()
    if not state:
        raise ContextIndexError("MEMORY_NOT_FOUND", "No memory snapshot exists for this context.")
    profile = _profile(rt)
    if state["embedding_fingerprint"] != profile["fingerprint"]:
        raise ContextIndexError("MEMORY_PROFILE_CONFLICT", "Memory context is pinned to a different embedding profile.")
    records = _snapshot_records(con, context_key, profile)
    if any(record["context_key"] != context_key for record in records):
        raise ContextIndexError("MEMORY_STATE_INVALID", "Stored memory context is inconsistent.")
    sequence = int(state["sequence"])
    if not records or max(record["sequence"] for record in records) != sequence:
        raise ContextIndexError("MEMORY_STATE_INVALID", "Stored memory sequence is inconsistent.")
    payload = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "context": context,
        "embedding": profile,
        "writer_id": role[1],
        "sequence": sequence,
        "records": records,
        "snapshot_hash": _snapshot_hash(records),
    }
    payload["bundle_hash"] = stable_json_hash(payload)
    return payload


def _write_private_file(path: Path, data: bytes) -> None:
    target = path.expanduser().absolute()
    try:
        parent = target.parent
        if not parent.is_dir():
            raise OSError
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OSError
        fd, temporary = tempfile.mkstemp(prefix=".context-memory-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError as exc:
        raise ContextIndexError("MEMORY_BUNDLE_IO", "Could not safely write the private memory snapshot.") from exc


def export_snapshot(rt: Runtime, path: Path) -> dict[str, Any]:
    memory_context(rt)
    con = connect(rt)
    try:
        payload = _bundle_payload(rt, con)
        data = (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_BUNDLE_BYTES:
            raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Memory snapshot exceeds the export size limit.")
        _write_private_file(path, data)
        return {
            "ok": True,
            "context_key": payload["context"]["context_key"],
            "sequence": payload["sequence"],
            "records": len(payload["records"]),
            "bundle_hash": payload["bundle_hash"],
            "path": str(path.expanduser().absolute()),
            "mode": "0600",
            "trust_model": SNAPSHOT_TRUST_WARNING,
        }
    finally:
        con.close()


def _read_private_file(path: Path) -> Any:
    target = path.expanduser()
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise OSError
            if info.st_size < 2 or info.st_size > MAX_BUNDLE_BYTES:
                raise OSError
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                data = stream.read(MAX_BUNDLE_BYTES + 1)
            if len(data) > MAX_BUNDLE_BYTES:
                raise OSError
            return json.loads(data.decode("utf-8"), object_pairs_hook=strict_json_object)
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContextIndexError("MEMORY_BUNDLE_IO", "Snapshot must be a bounded, owner-only private JSON file.") from None


def _validate_bundle(bundle: Any, rt: Runtime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    required = {"format", "version", "context", "embedding", "writer_id", "sequence", "records", "snapshot_hash", "bundle_hash"}
    if not isinstance(bundle, dict) or set(bundle) != required:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot envelope has an unsupported shape.")
    if bundle["format"] != BUNDLE_FORMAT or bundle["version"] != BUNDLE_VERSION:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot format or version is unsupported.")
    if not isinstance(bundle["context"], dict) or bundle["context"] != memory_context(rt):
        raise ContextIndexError("MEMORY_CONTEXT_MISMATCH", "Snapshot context identity does not match this memory context.")
    profile = _profile(rt)
    if bundle["embedding"] != profile:
        raise ContextIndexError("MEMORY_PROFILE_CONFLICT", "Snapshot embedding profile does not match local configuration.")
    try:
        uuid.UUID(bundle["writer_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot writer identity is invalid.") from exc
    if type(bundle["sequence"]) is not int or bundle["sequence"] < 1:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot sequence is invalid.")
    raw_records = bundle["records"]
    if not isinstance(raw_records, list) or len(raw_records) > MAX_RECORDS:
        raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Snapshot record count exceeds the configured limit.")
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    total_content = 0
    total_lineage_entries = 0
    for raw_record in raw_records:
        item = _normalize_bundle_record(raw_record)
        _validate_record(item, profile["dimensions"])
        total_lineage_entries += len(item["record_hash_lineage"])
        if total_lineage_entries > MAX_TOTAL_RECORD_LINEAGE_ENTRIES:
            raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Snapshot lineage exceeds the aggregate limit.")
        if not item["is_deleted"]:
            total_content += _utf8_size(item["content"], "MEMORY_BUNDLE_INVALID")
            if total_content > MAX_TOTAL_CONTENT_BYTES:
                raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Snapshot exceeds the aggregate content limit.")
        if item["context_key"] != bundle["context"]["context_key"]:
            raise ContextIndexError("MEMORY_CONTEXT_MISMATCH", "Snapshot contains a record from another context.")
        if item["memory_id"] in seen_ids or item["sequence"] in seen_sequences or item["sequence"] > bundle["sequence"]:
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot has duplicate or out-of-range record revisions.")
        seen_ids.add(item["memory_id"])
        seen_sequences.add(item["sequence"])
        records.append(item)
    if not records or max(item["sequence"] for item in records) != bundle["sequence"]:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot sequence does not match its current records.")
    if [item["memory_id"] for item in records] != sorted(item["memory_id"] for item in records):
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot records are not in canonical order.")
    _validate_hash(bundle["snapshot_hash"], "MEMORY_BUNDLE_INVALID")
    _validate_hash(bundle["bundle_hash"], "MEMORY_BUNDLE_INVALID")
    if bundle["snapshot_hash"] != _snapshot_hash(raw_records):
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot content integrity check failed.")
    unsigned = dict(bundle)
    del unsigned["bundle_hash"]
    if bundle["bundle_hash"] != stable_json_hash(unsigned):
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Snapshot envelope integrity check failed.")
    return records, profile


def _local_replica_state(con: Any, context_key: str, expected_writer_id: str, profile: dict[str, Any]) -> Any:
    role = _database_role(con)
    if role is None:
        if (
            con.execute("SELECT 1 FROM memory_records LIMIT 1").fetchone()
            or con.execute("SELECT 1 FROM memory_context_state LIMIT 1").fetchone()
        ):
            raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Untracked memory state conflicts with the incoming snapshot.")
        return None
    if role[0] != "replica" or role[1] != expected_writer_id:
        raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Snapshot writer conflicts with this database's memory role.")
    state = con.execute("SELECT * FROM memory_context_state WHERE context_key=?", (context_key,)).fetchone()
    if state is None:
        if con.execute("SELECT 1 FROM memory_records WHERE context_key=? LIMIT 1", (context_key,)).fetchone():
            raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Untracked replica records conflict with the incoming snapshot.")
        return None
    if state["source_writer_id"] != expected_writer_id or state["embedding_fingerprint"] != profile["fingerprint"]:
        raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Snapshot identity conflicts with the imported replica state.")
    current = _snapshot_records(con, context_key, profile)
    if state["replica_snapshot_hash"] != _snapshot_hash(current):
        raise ContextIndexError("MEMORY_REPLICA_MODIFIED", "Replica memory state changed outside an authenticated import.")
    return state


def _validate_replica_transition(
    con: Any,
    context_key: str,
    state: Any,
    incoming: list[dict[str, Any]],
    incoming_sequence: int,
    dimensions: int,
) -> None:
    if state is None:
        return
    current_rows = con.execute(
        "SELECT * FROM memory_records WHERE context_key=?",
        (context_key,),
    ).fetchall()
    current = {
        record["memory_id"]: record
        for record in (_record_from_row(row, dimensions) for row in current_rows)
    }
    incoming_by_id = {record["memory_id"]: record for record in incoming}
    if not set(current).issubset(incoming_by_id):
        raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Incoming snapshot omits an existing replica record.")
    prior_sequence = int(state["sequence"])
    for memory_id, old in current.items():
        new = incoming_by_id[memory_id]
        if new["revision"] < old["revision"] or new["sequence"] < old["sequence"]:
            raise ContextIndexError("MEMORY_STALE_BUNDLE", "Incoming snapshot regresses a record revision.")
        if new["revision"] == old["revision"]:
            if new["record_hash"] != old["record_hash"] or new["sequence"] != old["sequence"]:
                raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Incoming snapshot conflicts with an existing revision.")
            continue
        if old["is_deleted"] or new["sequence"] <= prior_sequence:
            raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Incoming snapshot has an invalid memory transition.")
        if new["record_hash_version"] == 2:
            if [old["revision"], old["record_hash"]] not in new["record_hash_lineage"]:
                raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Incoming revision does not descend from the replica record.")
        elif new["revision"] != old["revision"] + 1 or new["previous_record_hash"] != old["record_hash"]:
            raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Incoming legacy revision does not continue the replica record.")
    for memory_id, new in incoming_by_id.items():
        if memory_id not in current and (
            new["revision"] != 1
            or new["previous_record_hash"] is not None
            or new["sequence"] <= prior_sequence
        ):
            raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "Incoming snapshot introduces an invalid memory record.")
    if incoming_sequence <= prior_sequence:
        raise ContextIndexError("MEMORY_STALE_BUNDLE", "Snapshot sequence is not newer than the imported replica state.")


def import_snapshot(rt: Runtime, path: Path) -> dict[str, Any]:
    context_key = memory_context(rt)["context_key"]
    bundle = _read_private_file(path)
    records, profile = _validate_bundle(bundle, rt)
    con = connect(rt)
    try:
        con.execute("BEGIN IMMEDIATE")
        role = _database_role(con)
        if role and role[0] == "writer":
            raise ContextIndexError("MEMORY_IMPORT_CONFLICT", "A central writer database cannot be converted into a replica.")
        state = _local_replica_state(con, context_key, str(bundle["writer_id"]), profile)
        _validate_replica_transition(
            con,
            context_key,
            state,
            records,
            int(bundle["sequence"]),
            int(profile["dimensions"]),
        )
        profile_id = embedding_profile(rt, con)
        con.execute("DELETE FROM memory_records WHERE context_key=?", (context_key,))
        fts_rows = con.execute("SELECT rowid FROM memory_fts WHERE context_key=?", (context_key,)).fetchall()
        con.executemany("DELETE FROM memory_fts WHERE rowid=?", ((row["rowid"],) for row in fts_rows))
        for item in records:
            _persist_record(con, item, profile_id, profile["fingerprint"])
            if not item["is_deleted"]:
                _sync_fts(con, context_key, item["memory_id"], item["content"])
        if role is None:
            con.execute(
                "INSERT INTO memory_database_state(singleton, role, writer_id) VALUES (1, 'replica', ?)",
                (bundle["writer_id"],),
            )
        con.execute(
            """INSERT INTO memory_context_state(
                 context_key, sequence, embedding_fingerprint, replica_snapshot_hash, source_writer_id
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(context_key) DO UPDATE SET
                 sequence=excluded.sequence,
                 embedding_fingerprint=excluded.embedding_fingerprint,
                 replica_snapshot_hash=excluded.replica_snapshot_hash,
                 source_writer_id=excluded.source_writer_id""",
            (context_key, bundle["sequence"], profile["fingerprint"], _snapshot_hash(records), bundle["writer_id"]),
        )
        con.commit()
        return {
            "ok": True,
            "context_key": context_key,
            "sequence": bundle["sequence"],
            "records": len(records),
            "bundle_hash": bundle["bundle_hash"],
            "role": "replica",
            "trust_model": SNAPSHOT_TRUST_WARNING,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
