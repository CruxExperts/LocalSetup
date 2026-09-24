from __future__ import annotations

import json
import math
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from .common import ContextIndexError, Runtime, utc_now, uuid7
from .embeddings import cosine, embedding_profile, embedding_vector, pack_vector, safe_fts_query, unpack_vector
from .memory_records import (
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    MAX_BUNDLE_BYTES,
    MAX_CONTENT_BYTES,
    MAX_EMBEDDING_PREFIX_BYTES,
    MAX_EMBEDDING_RESPONSE_BYTES,
    MAX_INPUT_BYTES,
    MAX_QUERY_BYTES,
    MAX_RECORDS,
    MAX_RECORD_LINEAGE_ENTRIES,
    MAX_SOURCE_REF_BYTES,
    MAX_TOP_K,
    MAX_TOTAL_CONTENT_BYTES,
    MAX_TOTAL_RECORD_LINEAGE_ENTRIES,
    SNAPSHOT_TRUST_WARNING,
    _create_record,
    _database_role,
    _db_record_values,
    _next_record_hash_lineage,
    _profile,
    _persist_record,
    _public_record,
    _record_from_row,
    _snapshot_hash,
    _snapshot_records,
    _sync_fts,
    _utf8_size,
    _validate_source,
    memory_context,
    strict_json_object,
    validate_write_input,
)
from .memory_snapshot import export_snapshot, import_snapshot
from .storage import connect


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
