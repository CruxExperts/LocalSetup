from __future__ import annotations

import base64
import binascii
import json
import math
import re
import uuid
from typing import Any

from .common import ContextIndexError, Runtime, sha256_bytes, sha256_text, stable_json_hash
from .embeddings import unpack_vector


BUNDLE_FORMAT = "localsetup-context-memory"


BUNDLE_VERSION = 1


MAX_CONTENT_BYTES = 64 * 1024


MAX_RECORDS = 1000


MAX_TOTAL_CONTENT_BYTES = 3 * 1024 * 1024


MAX_SOURCE_REF_BYTES = 2048


MAX_INPUT_BYTES = MAX_CONTENT_BYTES * 6 + MAX_SOURCE_REF_BYTES * 6 + 8192


MAX_QUERY_BYTES = 16 * 1024


MAX_RECORD_LINEAGE_ENTRIES = 100_000


MAX_TOTAL_RECORD_LINEAGE_ENTRIES = 100_000


MAX_BUNDLE_BYTES = 16 * 1024 * 1024


MAX_EMBEDDING_PREFIX_BYTES = 2048


MAX_EMBEDDING_RESPONSE_BYTES = 1024 * 1024


MAX_TOP_K = 50


SNAPSHOT_TRUST_WARNING = (
    "Snapshot hashes detect integrity changes, not sender authenticity; transfer only through an "
    "operator-approved authenticated channel."
)


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


_SOURCE_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


_RECORD_FIELDS = {
    "memory_id",
    "context_key",
    "revision",
    "sequence",
    "is_deleted",
    "content",
    "content_hash",
    "source_type",
    "source_ref",
    "source_hash",
    "previous_record_hash",
    "record_hash",
    "record_hash_version",
    "record_hash_lineage",
    "vector",
    "vector_hash",
    "created_at",
    "updated_at",
}


_RECORD_LEGACY_FIELDS = _RECORD_FIELDS - {"record_hash_version", "record_hash_lineage"}


def memory_context(rt: Runtime) -> dict[str, str]:
    context = dict(rt.context)
    identity = rt.config["context_index"].get("identity", {})
    memory_uuid = identity.get("memory_uuid") if isinstance(identity, dict) else None
    try:
        if not isinstance(memory_uuid, str):
            raise ValueError
        memory_uuid = str(uuid.UUID(memory_uuid))
        if str(identity.get("memory_uuid")).lower() != memory_uuid:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ContextIndexError(
            "MEMORY_IDENTITY_REQUIRED",
            "Memory operations require an initialized per-repository memory UUID.",
            "Run `context-index config init` for this repository before using memory.",
        ) from None
    context["corpus_slug"] = memory_uuid
    context["scope_slug"] = "memory"
    context["context_key"] = "/".join(
        (context["tenant_slug"], context["namespace_slug"], memory_uuid, "memory")
    )
    return context


def _profile(rt: Runtime) -> dict[str, Any]:
    config = rt.config["context_index"].get("embeddings", {})
    provider = str(config.get("provider") or "local_hash").lower().replace("-", "_")
    model = str(config.get("model") or "localsetup-hash-v1")
    dimensions = int(config.get("dimensions") or 64)
    if dimensions < 1 or dimensions > 4096:
        raise ContextIndexError("MEMORY_PROFILE_INVALID", "Configured embedding profile has invalid dimensions.")
    return {
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
        "fingerprint": stable_json_hash(
            {
                "provider": provider,
                "model": model,
                "dimensions": dimensions,
                "document_prefix": str(config.get("document_prefix") or ""),
                "query_prefix": str(config.get("query_prefix") or ""),
            }
        ),
    }


def strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _validate_hash(value: Any, code: str = "MEMORY_PROVENANCE_INVALID") -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ContextIndexError(code, "Expected a declared SHA-256 value.")
    return value


def _utf8_size(value: str, code: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ContextIndexError(code, "Input text is not valid Unicode.") from None


def _validate_source(source: Any) -> tuple[str, str, str]:
    if not isinstance(source, dict) or set(source) != {"type", "ref", "sha256"}:
        raise ContextIndexError("MEMORY_PROVENANCE_REQUIRED", "Declare source type, reference, and SHA-256 in the input.")
    source_type = source["type"]
    source_ref = source["ref"]
    source_hash = source["sha256"]
    if not isinstance(source_type, str) or not _SOURCE_TYPE.fullmatch(source_type):
        raise ContextIndexError("MEMORY_PROVENANCE_INVALID", "Source type must be a bounded identifier.")
    if (
        not isinstance(source_ref, str)
        or not source_ref.strip()
        or "\x00" in source_ref
        or any(ord(char) < 32 for char in source_ref)
        or _utf8_size(source_ref, "MEMORY_PROVENANCE_INVALID") > MAX_SOURCE_REF_BYTES
    ):
        raise ContextIndexError("MEMORY_PROVENANCE_INVALID", "Source reference is empty or exceeds its size limit.")
    return source_type, source_ref, _validate_hash(source_hash)


def validate_write_input(payload: Any) -> tuple[str, str, str, str]:
    if not isinstance(payload, dict) or set(payload) != {"content", "source"}:
        raise ContextIndexError("MEMORY_INPUT_INVALID", "Input must declare content and source provenance.")
    content = payload["content"]
    if (
        not isinstance(content, str)
        or not content.strip()
        or "\x00" in content
        or any(ord(char) < 32 and char not in "\t\n\r" for char in content)
    ):
        raise ContextIndexError("MEMORY_INPUT_INVALID", "Memory content must be non-empty text without control bytes.")
    if _utf8_size(content, "MEMORY_INPUT_INVALID") > MAX_CONTENT_BYTES:
        raise ContextIndexError("MEMORY_INPUT_TOO_LARGE", "Memory content exceeds the configured size limit.")
    source_type, source_ref, source_hash = _validate_source(payload["source"])
    return content, source_type, source_ref, source_hash


def _database_role(con: Any) -> tuple[str, str] | None:
    row = con.execute("SELECT role, writer_id FROM memory_database_state WHERE singleton=1").fetchone()
    return (str(row["role"]), str(row["writer_id"])) if row else None


def _record_core(record: dict[str, Any]) -> dict[str, Any]:
    fields = _RECORD_FIELDS - {"record_hash", "vector"}
    if record["record_hash_version"] == 1:
        fields -= {"record_hash_version", "record_hash_lineage"}
    return {key: record[key] for key in sorted(fields)}


def _record_hash(record: dict[str, Any]) -> str:
    return stable_json_hash(_record_core(record))


def _record_from_row(row: Any, dimensions: int | None = None) -> dict[str, Any]:
    vector = bytes(row["vector_blob"]) if row["vector_blob"] is not None else None
    vector_dimensions = _profile_dimensions_from_blob(vector)
    if dimensions is not None and vector is not None and vector_dimensions != dimensions:
        raise ContextIndexError("MEMORY_RECORD_INVALID", "Stored memory vector dimensions are invalid.")
    revision = int(row["revision"])
    previous_record_hash = row["previous_record_hash"]
    version = int(row["record_hash_version"])
    lineage_json = row["record_hash_lineage_json"]
    if lineage_json is None:
        lineage = [] if revision == 1 else [[revision - 1, previous_record_hash]]
    else:
        try:
            lineage = json.loads(str(lineage_json))
        except (TypeError, ValueError):
            raise ContextIndexError("MEMORY_RECORD_INVALID", "Stored memory lineage is invalid.") from None
    record = {
        "memory_id": str(row["memory_id"]),
        "context_key": str(row["context_key"]),
        "revision": revision,
        "sequence": int(row["sequence"]),
        "is_deleted": bool(row["is_deleted"]),
        "content": row["content"],
        "content_hash": str(row["content_hash"]),
        "source_type": str(row["source_type"]),
        "source_ref": str(row["source_ref"]),
        "source_hash": str(row["source_hash"]),
        "previous_record_hash": previous_record_hash,
        "record_hash": str(row["record_hash"]),
        "record_hash_version": version,
        "record_hash_lineage": lineage,
        "vector": base64.b64encode(vector).decode("ascii") if vector is not None else None,
        "vector_hash": row["vector_hash"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    _validate_record(record, dimensions or vector_dimensions)
    return record


def _profile_dimensions_from_blob(vector: bytes | None) -> int:
    return len(vector) // 4 if vector is not None else 0


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str) or len(value) > 32 or not value.endswith("Z"):
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle timestamp is invalid.")
    try:
        from datetime import datetime

        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle timestamp is invalid.") from exc


def _normalize_bundle_record(record: Any) -> Any:
    if isinstance(record, dict) and set(record) == _RECORD_LEGACY_FIELDS:
        normalized = dict(record)
        revision = normalized.get("revision")
        previous_hash = normalized.get("previous_record_hash")
        normalized["record_hash_version"] = 1
        normalized["record_hash_lineage"] = (
            [] if type(revision) is not int or revision == 1 else [[revision - 1, previous_hash]]
        )
        return normalized
    return record


def _validate_record(record: Any, dimensions: int) -> None:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle record has an unsupported shape.")
    try:
        uuid.UUID(str(record["memory_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle record identity is invalid.") from exc
    if not isinstance(record["context_key"], str) or not record["context_key"]:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle record context is invalid.")
    if type(record["revision"]) is not int or record["revision"] < 1:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle record revision is invalid.")
    if type(record["sequence"]) is not int or record["sequence"] < 1:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle record sequence is invalid.")
    if type(record["is_deleted"]) is not bool:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle tombstone flag is invalid.")
    source_type, source_ref, source_hash = _validate_source(
        {"type": record["source_type"], "ref": record["source_ref"], "sha256": record["source_hash"]}
    )
    if source_type != record["source_type"] or source_ref != record["source_ref"] or source_hash != record["source_hash"]:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle provenance is invalid.")
    _validate_hash(record["content_hash"], "MEMORY_BUNDLE_INVALID")
    if record["previous_record_hash"] is not None:
        _validate_hash(record["previous_record_hash"], "MEMORY_BUNDLE_INVALID")
    if (record["revision"] == 1) != (record["previous_record_hash"] is None):
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle revision chain metadata is invalid.")
    if type(record["record_hash_version"]) is not int or record["record_hash_version"] not in {1, 2}:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle record hash version is invalid.")
    lineage = record["record_hash_lineage"]
    if not isinstance(lineage, list) or len(lineage) > MAX_RECORD_LINEAGE_ENTRIES:
        raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Bundle record lineage exceeds the configured limit.")
    prior_revision = 0
    for entry in lineage:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or type(entry[0]) is not int
            or entry[0] <= prior_revision
            or entry[0] >= record["revision"]
        ):
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle record lineage is invalid.")
        _validate_hash(entry[1], "MEMORY_BUNDLE_INVALID")
        prior_revision = entry[0]
    if record["revision"] == 1:
        if lineage:
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Initial memory records cannot contain prior hashes.")
    elif not lineage or lineage[-1] != [record["revision"] - 1, record["previous_record_hash"]]:
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle lineage does not match its previous record hash.")
    _validate_hash(record["record_hash"], "MEMORY_BUNDLE_INVALID")
    _validate_timestamp(record["created_at"])
    _validate_timestamp(record["updated_at"])
    if record["is_deleted"]:
        if record["content"] is not None or record["vector"] is not None or record["vector_hash"] is not None:
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Tombstones must not contain memory content or vectors.")
        if record["content_hash"] != sha256_text(""):
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Tombstone content digest is invalid.")
    else:
        content = record["content"]
        if (
            not isinstance(content, str)
            or not content.strip()
            or "\x00" in content
            or any(ord(char) < 32 and char not in "\t\n\r" for char in content)
        ):
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle content is invalid.")
        if _utf8_size(content, "MEMORY_BUNDLE_INVALID") > MAX_CONTENT_BYTES or record["content_hash"] != sha256_text(content):
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle content integrity check failed.")
        _validate_hash(record["vector_hash"], "MEMORY_BUNDLE_INVALID")
        if not isinstance(record["vector"], str) or dimensions < 1:
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle vector is missing.")
        try:
            vector = base64.b64decode(record["vector"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle vector encoding is invalid.") from exc
        if len(vector) != dimensions * 4 or sha256_bytes(vector) != record["vector_hash"]:
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle vector integrity check failed.")
        values = unpack_vector(vector)
        norm_sq = sum(value * value for value in values)
        if not all(math.isfinite(value) for value in values) or not 0.9 <= norm_sq <= 1.1:
            raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle vector values are invalid.")
    if record["record_hash"] != _record_hash(record):
        raise ContextIndexError("MEMORY_BUNDLE_INVALID", "Bundle record integrity check failed.")


def _snapshot_records(con: Any, context_key: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
    total = con.execute(
        "SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0) AS total FROM memory_records WHERE context_key=? AND is_deleted=0",
        (context_key,),
    ).fetchone()["total"]
    if int(total) > MAX_TOTAL_CONTENT_BYTES:
        raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Memory snapshot exceeds the aggregate content limit.")
    rows = con.execute(
        "SELECT * FROM memory_records WHERE context_key=? ORDER BY memory_id",
        (context_key,),
    ).fetchall()
    if len(rows) > MAX_RECORDS:
        raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Memory snapshot exceeds the record limit.")
    records = []
    lineage_entries = 0
    for row in rows:
        if str(row["embedding_fingerprint"]) != profile["fingerprint"]:
            raise ContextIndexError("MEMORY_PROFILE_CONFLICT", "Stored memory uses a different embedding profile.")
        record = _record_from_row(row, profile["dimensions"])
        lineage_entries += len(record["record_hash_lineage"])
        if lineage_entries > MAX_TOTAL_RECORD_LINEAGE_ENTRIES:
            raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Memory snapshot lineage exceeds the configured limit.")
        records.append(record)
    return records


def _snapshot_hash(records: list[dict[str, Any]]) -> str:
    return stable_json_hash(records)


def _db_record_values(record: dict[str, Any], profile_id: str, fingerprint: str) -> tuple[Any, ...]:
    vector = base64.b64decode(record["vector"]) if record["vector"] is not None else None
    return (
        record["memory_id"],
        record["context_key"],
        record["revision"],
        record["sequence"],
        int(record["is_deleted"]),
        record["content"],
        record["content_hash"],
        record["source_type"],
        record["source_ref"],
        record["source_hash"],
        record["previous_record_hash"],
        record["record_hash"],
        record["record_hash_version"],
        json.dumps(record["record_hash_lineage"], separators=(",", ":")),
        profile_id,
        fingerprint,
        vector,
        record["vector_hash"],
        record["created_at"],
        record["updated_at"],
    )


def _next_record_hash_lineage(record: dict[str, Any]) -> list[list[Any]]:
    lineage = record["record_hash_lineage"]
    if len(lineage) >= MAX_RECORD_LINEAGE_ENTRIES:
        raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Memory record lineage exceeds the configured limit.")
    return [*lineage, [record["revision"], record["record_hash"]]]


def _create_record(
    *,
    memory_id: str,
    context_key: str,
    revision: int,
    sequence: int,
    content: str | None,
    source_type: str,
    source_ref: str,
    source_hash: str,
    previous_record_hash: str | None,
    vector: bytes | None,
    created_at: str,
    updated_at: str,
    record_hash_lineage: list[list[Any]] | None = None,
    record_hash_version: int = 2,
) -> dict[str, Any]:
    record = {
        "memory_id": memory_id,
        "context_key": context_key,
        "revision": revision,
        "sequence": sequence,
        "is_deleted": content is None,
        "content": content,
        "content_hash": sha256_text(content or ""),
        "source_type": source_type,
        "source_ref": source_ref,
        "source_hash": source_hash,
        "previous_record_hash": previous_record_hash,
        "record_hash_version": record_hash_version,
        "record_hash_lineage": record_hash_lineage or [],
        "record_hash": "",
        "vector": base64.b64encode(vector).decode("ascii") if vector is not None else None,
        "vector_hash": sha256_bytes(vector) if vector is not None else None,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    record["record_hash"] = _record_hash(record)
    return record


def _persist_record(con: Any, record: dict[str, Any], profile_id: str, fingerprint: str) -> None:
    values = _db_record_values(record, profile_id, fingerprint)
    con.execute(
        """INSERT OR REPLACE INTO memory_records(
             memory_id, context_key, revision, sequence, is_deleted, content, content_hash,
             source_type, source_ref, source_hash, previous_record_hash, record_hash,
             record_hash_version, record_hash_lineage_json,
             embedding_profile_id, embedding_fingerprint, vector_blob, vector_hash, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        values,
    )


def _sync_fts(con: Any, context_key: str, memory_id: str, content: str | None) -> None:
    rows = con.execute(
        "SELECT rowid FROM memory_fts WHERE memory_id=? AND context_key=?",
        (memory_id, context_key),
    ).fetchall()
    con.executemany("DELETE FROM memory_fts WHERE rowid=?", ((row["rowid"],) for row in rows))
    if content is not None:
        con.execute(
            "INSERT INTO memory_fts(content, memory_id, context_key) VALUES (?, ?, ?)",
            (content, memory_id, context_key),
        )


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": record["memory_id"],
        "context_key": record["context_key"],
        "revision": record["revision"],
        "sequence": record["sequence"],
        "deleted": record["is_deleted"],
        "content": record["content"],
        "content_hash": record["content_hash"],
        "source": {"type": record["source_type"], "ref": record["source_ref"], "sha256": record["source_hash"]},
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }
