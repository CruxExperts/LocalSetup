from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from .common import ContextIndexError, Runtime, sha256_bytes, sha256_text, stable_json_hash, utc_now, uuid7
from .embeddings import cosine, embedding_vector, embedding_profile, pack_vector, safe_fts_query, unpack_vector
from .storage import connect

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


def _loopback_embedding(rt: Runtime, prepared: str, dimensions: int) -> list[float]:
    config = rt.config["context_index"].get("embeddings", {})
    endpoint = str(
        config.get("endpoint")
        or os.environ.get("LOCALSETUP_CONTEXT_INDEX_EMBEDDINGS_URL", "")
    ).strip()
    parsed = None
    hostname = ""
    invalid_endpoint = False
    try:
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").lower()
        parsed.port
    except ValueError:
        invalid_endpoint = True
    if invalid_endpoint or parsed is None:
        raise ContextIndexError(
            "MEMORY_LOCAL_EMBEDDING_REQUIRED",
            "Memory embedding endpoint must be a loopback HTTP(S) endpoint.",
        )
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ContextIndexError(
            "MEMORY_LOCAL_EMBEDDING_REQUIRED",
            "Memory embedding endpoint must be a loopback HTTP(S) endpoint.",
        )
    api_key_env = str(config.get("api_key_env") or "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = 30
    invalid_timeout = False
    try:
        timeout = max(1, min(int(config.get("timeout_seconds") or 30), 60))
    except (TypeError, ValueError):
        invalid_timeout = True
    if invalid_timeout:
        raise ContextIndexError("MEMORY_EMBEDDING_FAILED", "Memory embedding failed; provider details were redacted.")
    response = None
    failed = False
    vector: list[float] | None = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(
                endpoint,
                json={"model": str(config.get("model") or ""), "input": prepared, "encoding_format": "float"},
                headers=headers,
                timeout=(2, timeout),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code != 200:
                failed = True
            body = bytearray()
            if not failed:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        if len(body) + len(chunk) > MAX_EMBEDDING_RESPONSE_BYTES:
                            failed = True
                            break
                        body.extend(chunk)
            if not failed:
                data = json.loads(body, object_pairs_hook=strict_json_object)
                raw_vector = data["data"][0]["embedding"]
                if not isinstance(raw_vector, list) or len(raw_vector) != dimensions:
                    failed = True
                else:
                    vector = [float(value) for value in raw_vector]
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, OverflowError):
        failed = True
    finally:
        if response is not None:
            response.close()
    if failed or vector is None or not all(math.isfinite(value) for value in vector):
        raise ContextIndexError("MEMORY_EMBEDDING_FAILED", "Memory embedding failed; provider details were redacted.")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0 or not math.isfinite(norm):
        raise ContextIndexError("MEMORY_EMBEDDING_FAILED", "Memory embedding failed; provider details were redacted.")
    return [value / norm for value in vector]


def _embed(rt: Runtime, text: str, usage: str) -> list[float]:
    profile = _profile(rt)
    config = rt.config["context_index"].get("embeddings", {})
    prefix_key = "query_prefix" if usage == "query" else "document_prefix"
    prefix = str(config.get(prefix_key) or "")
    if _utf8_size(prefix, "MEMORY_PROFILE_INVALID") > MAX_EMBEDDING_PREFIX_BYTES:
        raise ContextIndexError("MEMORY_PROFILE_INVALID", "Embedding prefix exceeds the configured memory limit.")
    prepared = prefix + text
    if profile["provider"] == "local_hash":
        failed = False
        vector: list[float] | None = None
        try:
            vector = embedding_vector(rt, text, usage)
        except ContextIndexError:
            failed = True
        if failed or vector is None:
            raise ContextIndexError(
                "MEMORY_EMBEDDING_FAILED",
                "Memory embedding failed; provider details were redacted.",
            )
    elif profile["provider"] in {"openai_compatible", "openai", "llama_cpp", "llamacpp"}:
        vector = _loopback_embedding(rt, prepared, profile["dimensions"])
    else:
        raise ContextIndexError(
            "MEMORY_LOCAL_EMBEDDING_REQUIRED",
            "Memory embeddings require local_hash or an explicitly configured loopback endpoint.",
        )
    if len(vector) != profile["dimensions"] or not all(math.isfinite(value) for value in vector):
        raise ContextIndexError("MEMORY_EMBEDDING_FAILED", "Memory embedding failed; provider details were redacted.")
    return vector


def _is_central_database(rt: Runtime) -> bool:
    storage = rt.config["context_index"].get("storage", {})
    mode = str(storage.get("mode") or "")
    if mode in {"global", "central_sqlite"}:
        return True
    configured = storage.get("global_database", {}).get("path")
    if not configured:
        configured = rt.home / ".local/share/localsetup/context-index/context-index.sqlite3"
    return rt.db_path.expanduser().resolve() == Path(str(configured)).expanduser().resolve()


def _assert_can_mutate(rt: Runtime) -> None:
    if not _is_central_database(rt):
        raise ContextIndexError(
            "MEMORY_CENTRAL_REQUIRED",
            "Memory mutations require the configured global/central SQLite database.",
            "Select the global database or configure storage.mode as global or central_sqlite.",
        )
    con = connect(rt)
    try:
        role = _database_role(con)
        if role and role[0] == "replica":
            raise ContextIndexError("MEMORY_READ_ONLY_REPLICA", "Imported memory replicas are read-only.")
        if not role and (
            con.execute("SELECT 1 FROM memory_records LIMIT 1").fetchone()
            or con.execute("SELECT 1 FROM memory_context_state LIMIT 1").fetchone()
        ):
            raise ContextIndexError("MEMORY_STATE_CONFLICT", "Untracked memory state cannot be mutated.")
    finally:
        con.close()


def _database_role(con: Any) -> tuple[str, str] | None:
    row = con.execute("SELECT role, writer_id FROM memory_database_state WHERE singleton=1").fetchone()
    return (str(row["role"]), str(row["writer_id"])) if row else None


def _require_writer(con: Any, rt: Runtime) -> str:
    if not _is_central_database(rt):
        raise ContextIndexError(
            "MEMORY_CENTRAL_REQUIRED",
            "Memory mutations require the configured global/central SQLite database.",
            "Select the global database or configure storage.mode as global or central_sqlite.",
        )
    role = _database_role(con)
    if role and role[0] == "replica":
        raise ContextIndexError("MEMORY_READ_ONLY_REPLICA", "Imported memory replicas are read-only.")
    if role and role[0] != "writer":
        raise ContextIndexError("MEMORY_STATE_INVALID", "Memory database role is invalid.")
    if role:
        return role[1]
    if con.execute("SELECT 1 FROM memory_records LIMIT 1").fetchone():
        raise ContextIndexError("MEMORY_STATE_CONFLICT", "Memory records exist without writer metadata.")
    if con.execute("SELECT 1 FROM memory_context_state LIMIT 1").fetchone():
        raise ContextIndexError("MEMORY_STATE_CONFLICT", "Memory state exists without writer metadata.")
    writer_id = str(uuid.uuid4())
    con.execute(
        "INSERT INTO memory_database_state(singleton, role, writer_id) VALUES (1, 'writer', ?)",
        (writer_id,),
    )
    return writer_id


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


def _ensure_writer_context(con: Any, context_key: str, profile: dict[str, Any]) -> Any:
    state = con.execute("SELECT * FROM memory_context_state WHERE context_key=?", (context_key,)).fetchone()
    if state:
        if state["embedding_fingerprint"] != profile["fingerprint"]:
            raise ContextIndexError("MEMORY_PROFILE_CONFLICT", "Memory context is pinned to a different embedding profile.")
        return state
    con.execute(
        "INSERT INTO memory_context_state(context_key, sequence, embedding_fingerprint) VALUES (?, 0, ?)",
        (context_key, profile["fingerprint"]),
    )
    return con.execute("SELECT * FROM memory_context_state WHERE context_key=?", (context_key,)).fetchone()


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


def record(rt: Runtime, payload: Any) -> dict[str, Any]:
    context_key = memory_context(rt)["context_key"]
    _assert_can_mutate(rt)
    content, source_type, source_ref, source_hash = validate_write_input(payload)
    profile = _profile(rt)
    vector_blob = pack_vector(_embed(rt, content, "document"))
    con = connect(rt)
    try:
        con.execute("BEGIN IMMEDIATE")
        writer_id = _require_writer(con, rt)
        state = _ensure_writer_context(con, context_key, profile)
        count = con.execute("SELECT COUNT(*) AS count FROM memory_records WHERE context_key=?", (context_key,)).fetchone()["count"]
        if int(count) >= MAX_RECORDS:
            raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Memory context has reached its record limit.")
        total_content = con.execute(
            "SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0) AS total FROM memory_records WHERE context_key=? AND is_deleted=0",
            (context_key,),
        ).fetchone()["total"]
        if int(total_content) + _utf8_size(content, "MEMORY_INPUT_INVALID") > MAX_TOTAL_CONTENT_BYTES:
            raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Memory context has reached its aggregate content limit.")
        sequence = int(state["sequence"]) + 1
        now = utc_now()
        record_data = _create_record(
            memory_id=uuid7(),
            context_key=context_key,
            revision=1,
            sequence=sequence,
            content=content,
            source_type=source_type,
            source_ref=source_ref,
            source_hash=source_hash,
            previous_record_hash=None,
            vector=vector_blob,
            created_at=now,
            updated_at=now,
        )
        profile_id = embedding_profile(rt, con)
        _persist_record(con, record_data, profile_id, profile["fingerprint"])
        _sync_fts(con, context_key, record_data["memory_id"], content)
        con.execute(
            "UPDATE memory_context_state SET sequence=? WHERE context_key=?",
            (sequence, context_key),
        )
        con.commit()
        return {"ok": True, "writer_id": writer_id, "memory": _public_record(record_data)}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _load_current(con: Any, context_key: str, memory_id: str, dimensions: int) -> tuple[Any, dict[str, Any]]:
    row = con.execute(
        "SELECT * FROM memory_records WHERE memory_id=? AND context_key=?",
        (memory_id, context_key),
    ).fetchone()
    if not row:
        raise ContextIndexError("MEMORY_NOT_FOUND", "Memory record was not found.")
    return row, _record_from_row(row, dimensions)


def update(rt: Runtime, memory_id: str, expected_revision: int, payload: Any) -> dict[str, Any]:
    context_key = memory_context(rt)["context_key"]
    if type(expected_revision) is not int or expected_revision < 1:
        raise ContextIndexError("MEMORY_REVISION_INVALID", "Expected revision must be a positive integer.")
    _assert_can_mutate(rt)
    content, source_type, source_ref, source_hash = validate_write_input(payload)
    profile = _profile(rt)
    vector_blob = pack_vector(_embed(rt, content, "document"))
    con = connect(rt)
    try:
        con.execute("BEGIN IMMEDIATE")
        writer_id = _require_writer(con, rt)
        state = _ensure_writer_context(con, context_key, profile)
        _, old = _load_current(con, context_key, memory_id, profile["dimensions"])
        if old["is_deleted"]:
            raise ContextIndexError("MEMORY_DELETED", "Tombstoned memory cannot be updated.")
        if expected_revision != old["revision"]:
            raise ContextIndexError("MEMORY_STALE_REVISION", "Expected memory revision does not match current revision.")
        total_content = con.execute(
            "SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0) AS total FROM memory_records WHERE context_key=? AND is_deleted=0",
            (context_key,),
        ).fetchone()["total"]
        if int(total_content) - _utf8_size(str(old["content"]), "MEMORY_RECORD_INVALID") + _utf8_size(content, "MEMORY_INPUT_INVALID") > MAX_TOTAL_CONTENT_BYTES:
            raise ContextIndexError("MEMORY_LIMIT_EXCEEDED", "Memory context has reached its aggregate content limit.")
        sequence = int(state["sequence"]) + 1
        record_data = _create_record(
            memory_id=memory_id,
            context_key=context_key,
            revision=old["revision"] + 1,
            sequence=sequence,
            content=content,
            source_type=source_type,
            source_ref=source_ref,
            source_hash=source_hash,
            record_hash_lineage=_next_record_hash_lineage(old),
            previous_record_hash=old["record_hash"],
            vector=vector_blob,
            created_at=old["created_at"],
            updated_at=utc_now(),
        )
        _persist_record(con, record_data, embedding_profile(rt, con), profile["fingerprint"])
        _sync_fts(con, context_key, memory_id, content)
        con.execute("UPDATE memory_context_state SET sequence=? WHERE context_key=?", (sequence, context_key))
        con.commit()
        return {"ok": True, "writer_id": writer_id, "memory": _public_record(record_data)}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def delete(rt: Runtime, memory_id: str, expected_revision: int) -> dict[str, Any]:
    context_key = memory_context(rt)["context_key"]
    if type(expected_revision) is not int or expected_revision < 1:
        raise ContextIndexError("MEMORY_REVISION_INVALID", "Expected revision must be a positive integer.")
    _assert_can_mutate(rt)
    profile = _profile(rt)
    con = connect(rt)
    try:
        con.execute("BEGIN IMMEDIATE")
        writer_id = _require_writer(con, rt)
        state = _ensure_writer_context(con, context_key, profile)
        _, old = _load_current(con, context_key, memory_id, profile["dimensions"])
        if expected_revision != old["revision"]:
            raise ContextIndexError("MEMORY_STALE_REVISION", "Expected memory revision does not match current revision.")
        if old["is_deleted"]:
            raise ContextIndexError("MEMORY_DELETED", "Memory is already tombstoned.")
        sequence = int(state["sequence"]) + 1
        record_data = _create_record(
            memory_id=memory_id,
            context_key=context_key,
            revision=old["revision"] + 1,
            sequence=sequence,
            content=None,
            source_type=old["source_type"],
            source_ref=old["source_ref"],
            source_hash=old["source_hash"],
            record_hash_lineage=_next_record_hash_lineage(old),
            previous_record_hash=old["record_hash"],
            vector=None,
            created_at=old["created_at"],
            updated_at=utc_now(),
        )
        _persist_record(con, record_data, embedding_profile(rt, con), profile["fingerprint"])
        _sync_fts(con, context_key, memory_id, None)
        con.execute("UPDATE memory_context_state SET sequence=? WHERE context_key=?", (sequence, context_key))
        con.commit()
        return {"ok": True, "writer_id": writer_id, "memory": _public_record(record_data)}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get(rt: Runtime, memory_id: str) -> dict[str, Any]:
    context_key = memory_context(rt)["context_key"]
    con = connect(rt)
    try:
        _, record_data = _load_current(con, context_key, memory_id, _profile(rt)["dimensions"])
        return {"ok": True, "memory": _public_record(record_data)}
    finally:
        con.close()


def search(rt: Runtime, query: str, top_k: int, mode: str = "hybrid") -> dict[str, Any]:
    context_key = memory_context(rt)["context_key"]
    if not isinstance(query, str) or not query.strip() or "\x00" in query:
        raise ContextIndexError("MEMORY_QUERY_INVALID", "Search query must be non-empty text.")
    if _utf8_size(query, "MEMORY_QUERY_INVALID") > MAX_QUERY_BYTES:
        raise ContextIndexError("MEMORY_QUERY_TOO_LARGE", "Search query exceeds its size limit.")
    if type(top_k) is not int or top_k < 1 or top_k > MAX_TOP_K:
        raise ContextIndexError("MEMORY_TOP_K_INVALID", f"top-k must be between 1 and {MAX_TOP_K}.")
    if mode not in {"vector", "lexical", "hybrid"}:
        raise ContextIndexError("MEMORY_SEARCH_MODE_INVALID", "Search mode must be vector, lexical, or hybrid.")
    profile = _profile(rt)
    query_vector = _embed(rt, query, "query") if mode != "lexical" else None
    con = connect(rt)
    try:
        scores: dict[str, dict[str, Any]] = {}
        if mode in {"vector", "hybrid"}:
            rows = con.execute(
                "SELECT * FROM memory_records WHERE context_key=? AND is_deleted=0",
                (context_key,),
            ).fetchall()
            for row in rows:
                if str(row["embedding_fingerprint"]) != profile["fingerprint"]:
                    raise ContextIndexError("MEMORY_PROFILE_CONFLICT", "Stored memory uses a different embedding profile.")
                record_data = _record_from_row(row, profile["dimensions"])
                vector_score = max(0.0, cosine(query_vector or [], unpack_vector(bytes(row["vector_blob"]))))
                scores[record_data["memory_id"]] = {
                    "record": record_data,
                    "vector": vector_score,
                    "lexical": 0.0,
                }
        lexical_rows = []
        if mode != "vector":
            lexical_query = safe_fts_query(query)
            lexical_rows = con.execute(
                """SELECT memory_id, bm25(memory_fts) AS rank_score
                   FROM memory_fts
                   WHERE memory_fts MATCH ? AND context_key=?
                   LIMIT ?""",
                (lexical_query, context_key, MAX_RECORDS),
            ).fetchall()
        for row in lexical_rows:
            memory_id = str(row["memory_id"])
            item = scores.get(memory_id)
            if item is None:
                memory_row = con.execute(
                    "SELECT * FROM memory_records WHERE memory_id=? AND context_key=? AND is_deleted=0",
                    (memory_id, context_key),
                ).fetchone()
                if not memory_row:
                    continue
                record_data = _record_from_row(memory_row, profile["dimensions"])
                item = {"record": record_data, "vector": 0.0, "lexical": 0.0}
                scores[memory_id] = item
            item["lexical"] = 1.0 / (1.0 + abs(float(row["rank_score"])))
        retrieval = rt.config["context_index"].get("retrieval", {}).get("hybrid", {})
        lexical_weight = float(retrieval.get("lexical_weight", 0.35))
        vector_weight = float(retrieval.get("vector_weight", 0.65))
        ranked = []
        for item in scores.values():
            if mode == "vector":
                score = item["vector"]
            elif mode == "lexical":
                score = item["lexical"]
            else:
                score = item["vector"] * vector_weight + item["lexical"] * lexical_weight
            ranked.append((score, item["record"]))
        ranked.sort(key=lambda item: (-item[0], item[1]["memory_id"]))
        return {
            "ok": True,
            "query": query,
            "mode": mode,
            "top_k": top_k,
            "context_key": context_key,
            "results": [
                {"score": score, "memory": _public_record(record_data)}
                for score, record_data in ranked[:top_k]
            ],
        }
    finally:
        con.close()


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
