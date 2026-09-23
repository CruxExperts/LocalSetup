import os
import sqlite3
import stat
from pathlib import Path

from .common import GLOBAL_DB_REL, SCHEMA_VERSION, ContextIndexError, Runtime, utc_now, uuid7


def _is_central_database(rt: Runtime) -> bool:
    storage = rt.config["context_index"].get("storage", {})
    if str(storage.get("mode") or "") in {"global", "central_sqlite"}:
        return True
    configured = storage.get("global_database", {}).get("path") or rt.home / GLOBAL_DB_REL
    return Path(str(configured)).expanduser().absolute() == rt.db_path.expanduser().absolute()


def _open_directory_chain(path: Path, *, upgrade_legacy_leaf: bool = False) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(os.path.sep, flags)
    try:
        for name in path.parts[1:]:
            if name in {"", "."}:
                continue
            if name == "..":
                raise OSError
            try:
                child = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(name, 0o700, dir_fd=fd)
                child = os.open(name, flags, dir_fd=fd)
            info = os.fstat(child)
            mode = stat.S_IMODE(info.st_mode)
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or (mode & 0o022 and not sticky_root)
            ):
                os.close(child)
                raise OSError
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or mode & 0o022
            or (mode & 0o700) != 0o700
        ):
            raise OSError
        if mode != 0o700:
            if not upgrade_legacy_leaf:
                raise OSError
            os.fchmod(fd, 0o700)
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError
        return fd
    except Exception:
        os.close(fd)
        raise


def _validate_sidecar_at(parent_fd: int, name: str) -> None:
    flags = (
        os.O_RDWR
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError
    finally:
        os.close(fd)


def _prepare_private_central_database(db_path: Path, *, default_path: Path) -> Path:
    parent_fd = -1
    db_fd = -1
    try:
        path = Path(os.path.abspath(os.fspath(db_path.expanduser())))
        if path.name in {"", ".", ".."}:
            raise OSError
        # Only the framework's dedicated default directory is eligible for legacy privacy migration.
        legacy_leaf = path == Path(os.path.abspath(os.fspath(default_path.expanduser())))
        parent_fd = _open_directory_chain(path.parent, upgrade_legacy_leaf=legacy_leaf)
        for suffix in ("-wal", "-shm"):
            try:
                _validate_sidecar_at(parent_fd, path.name + suffix)
            except FileNotFoundError:
                pass
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            db_fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
            os.fchmod(db_fd, 0o600)
        except FileExistsError:
            db_fd = os.open(
                path.name,
                os.O_RDWR
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        info = os.fstat(db_fd)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or (mode & 0o077)
            or (mode & 0o600) != 0o600
        ):
            raise OSError
        os.close(db_fd)
        db_fd = -1
        return path
    except (OSError, ValueError):
        raise ContextIndexError(
            "DATABASE_PATH_UNSAFE",
            "Central SQLite database must be an owner-only regular file inside a private, non-symlink directory.",
        ) from None
    finally:
        if db_fd >= 0:
            os.close(db_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _validate_central_sidecars(db_path: Path) -> None:
    parent_fd = -1
    try:
        parent_fd = _open_directory_chain(db_path.parent)
        for suffix in ("-wal", "-shm"):
            try:
                _validate_sidecar_at(parent_fd, db_path.name + suffix)
            except FileNotFoundError:
                pass
    except OSError:
        raise ContextIndexError(
            "DATABASE_PATH_UNSAFE",
            "Central SQLite sidecars must be regular files owned by the current user inside the private database directory.",
        ) from None
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def connect(rt: Runtime) -> sqlite3.Connection:
    central = _is_central_database(rt)
    if central:
        db_path = _prepare_private_central_database(rt.db_path, default_path=rt.home / GLOBAL_DB_REL)
    else:
        rt.db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path = rt.db_path
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        sqlite_cfg = rt.config["context_index"].get("storage", {}).get("sqlite", {})
        con.execute(f"PRAGMA busy_timeout={int(sqlite_cfg.get('busy_timeout_ms', 5000))}")
        con.execute(f"PRAGMA journal_mode={str(sqlite_cfg.get('journal_mode', 'WAL'))}")
        con.execute(f"PRAGMA synchronous={str(sqlite_cfg.get('synchronous', 'NORMAL'))}")
        if central:
            _validate_central_sidecars(db_path)
        else:
            _restrict_database_permissions(db_path)
        migrate(con)
        if central:
            _validate_central_sidecars(db_path)
        else:
            _restrict_database_permissions(db_path)
        return con
    except Exception:
        con.close()
        raise


def _restrict_database_permissions(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = str(db_path) + suffix
        if os.path.exists(candidate):
            os.chmod(candidate, 0o600)


def migrate(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        DROP INDEX IF EXISTS idx_usage_chunk;
        DROP INDEX IF EXISTS idx_usage_context_used;
        DROP TABLE IF EXISTS usage_events;

        CREATE TABLE IF NOT EXISTS database_metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contexts (
          scope_id TEXT PRIMARY KEY,
          tenant_slug TEXT NOT NULL,
          namespace_slug TEXT NOT NULL,
          corpus_slug TEXT NOT NULL,
          scope_slug TEXT NOT NULL,
          context_key TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
          source_id TEXT PRIMARY KEY,
          scope_id TEXT NOT NULL,
          context_key TEXT NOT NULL,
          tenant_slug TEXT NOT NULL,
          namespace_slug TEXT NOT NULL,
          corpus_slug TEXT NOT NULL,
          scope_slug TEXT NOT NULL,
          source_uri TEXT NOT NULL,
          repo_relative_path TEXT NOT NULL,
          source_type TEXT NOT NULL,
          priority TEXT NOT NULL,
          modality TEXT NOT NULL,
          source_exists INTEGER NOT NULL,
          indexed_file_size INTEGER NOT NULL,
          indexed_mtime_ns INTEGER NOT NULL,
          indexed_content_hash TEXT,
          indexed_extractor_hash TEXT NOT NULL,
          indexed_chunker_hash TEXT NOT NULL,
          indexed_embedding_config_hash TEXT NOT NULL,
          indexed_redaction_config_hash TEXT NOT NULL,
          indexed_at TEXT NOT NULL,
          last_checked_at TEXT NOT NULL,
          freshness_status TEXT NOT NULL,
          staleness_reason TEXT,
          source_fingerprint TEXT NOT NULL,
          UNIQUE(context_key, repo_relative_path)
        );
        CREATE TABLE IF NOT EXISTS chunks (
          chunk_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          context_key TEXT NOT NULL,
          repo_relative_path TEXT NOT NULL,
          chunk_index INTEGER NOT NULL,
          line_start INTEGER NOT NULL,
          line_end INTEGER NOT NULL,
          heading_path TEXT NOT NULL,
          content TEXT NOT NULL,
          chunk_hash TEXT NOT NULL,
          chunk_fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(source_id, chunk_index)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
          content,
          chunk_id UNINDEXED,
          source_id UNINDEXED,
          context_key UNINDEXED,
          repo_relative_path UNINDEXED
        );
        CREATE TABLE IF NOT EXISTS embedding_profiles (
          embedding_profile_id TEXT PRIMARY KEY,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          dimensions INTEGER NOT NULL,
          metric TEXT NOT NULL,
          config_hash TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vectors (
          vector_id TEXT PRIMARY KEY,
          chunk_id TEXT NOT NULL,
          context_key TEXT NOT NULL,
          embedding_profile_id TEXT NOT NULL,
          modality TEXT NOT NULL,
          dimensions INTEGER NOT NULL,
          vector_blob BLOB NOT NULL,
          vector_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(chunk_id, embedding_profile_id)
        );
        CREATE TABLE IF NOT EXISTS ingest_runs (
          ingest_run_id TEXT PRIMARY KEY,
          context_key TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL,
          summary_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS freshness_snapshots (
          snapshot_id TEXT PRIMARY KEY,
          context_key TEXT NOT NULL,
          checked_at TEXT NOT NULL,
          mode TEXT NOT NULL,
          summary_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reset_plans (
          plan_id TEXT PRIMARY KEY,
          context_key TEXT NOT NULL,
          mode TEXT NOT NULL,
          created_at TEXT NOT NULL,
          applied_at TEXT,
          summary_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS worker_runs (
          worker_run_id TEXT PRIMARY KEY,
          context_key TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL,
          summary_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS worker_locks (
          context_key TEXT PRIMARY KEY,
          worker_run_id TEXT NOT NULL,
          acquired_at TEXT NOT NULL,
          heartbeat_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_records (
          memory_id TEXT PRIMARY KEY,
          context_key TEXT NOT NULL,
          revision INTEGER NOT NULL,
          sequence INTEGER NOT NULL,
          is_deleted INTEGER NOT NULL CHECK(is_deleted IN (0, 1)),
          content TEXT,
          content_hash TEXT NOT NULL,
          source_type TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          source_hash TEXT NOT NULL,
          previous_record_hash TEXT,
          record_hash TEXT NOT NULL,
          record_hash_version INTEGER NOT NULL DEFAULT 1,
          record_hash_lineage_json TEXT,
          embedding_profile_id TEXT NOT NULL,
          embedding_fingerprint TEXT NOT NULL,
          vector_blob BLOB,
          vector_hash TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          CHECK((is_deleted = 1 AND content IS NULL AND vector_blob IS NULL AND vector_hash IS NULL)
             OR (is_deleted = 0 AND content IS NOT NULL AND vector_blob IS NOT NULL AND vector_hash IS NOT NULL))
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
          content,
          memory_id UNINDEXED,
          context_key UNINDEXED
        );
        CREATE TABLE IF NOT EXISTS memory_database_state (
          singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
          role TEXT NOT NULL CHECK(role IN ('writer', 'replica')),
          writer_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_context_state (
          context_key TEXT PRIMARY KEY,
          sequence INTEGER NOT NULL,
          embedding_fingerprint TEXT NOT NULL,
          replica_snapshot_hash TEXT,
          source_writer_id TEXT
        );
        """
    )
    memory_columns = {row[1] for row in con.execute("PRAGMA table_info(memory_records)")}
    if "record_hash_version" not in memory_columns:
        con.execute("ALTER TABLE memory_records ADD COLUMN record_hash_version INTEGER NOT NULL DEFAULT 1")
    if "record_hash_lineage_json" not in memory_columns:
        con.execute("ALTER TABLE memory_records ADD COLUMN record_hash_lineage_json TEXT")
    con.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_sources_context_path ON sources(context_key, repo_relative_path);
        CREATE INDEX IF NOT EXISTS idx_sources_context_freshness ON sources(context_key, freshness_status, priority, repo_relative_path);
        CREATE INDEX IF NOT EXISTS idx_sources_context_priority_status ON sources(context_key, priority, freshness_status, repo_relative_path);
        CREATE INDEX IF NOT EXISTS idx_sources_context_fingerprint ON sources(context_key, source_fingerprint);
        CREATE INDEX IF NOT EXISTS idx_sources_context_mtime ON sources(context_key, indexed_mtime_ns);
        CREATE INDEX IF NOT EXISTS idx_sources_scope_lookup ON sources(tenant_slug, namespace_slug, corpus_slug, scope_slug, repo_relative_path);
        CREATE INDEX IF NOT EXISTS idx_chunks_source_line ON chunks(source_id, line_start, line_end);
        CREATE INDEX IF NOT EXISTS idx_chunks_context_path ON chunks(context_key, repo_relative_path);
        CREATE INDEX IF NOT EXISTS idx_chunks_context_line_lookup ON chunks(context_key, repo_relative_path, line_start, line_end);
        CREATE INDEX IF NOT EXISTS idx_chunks_fingerprint ON chunks(context_key, chunk_fingerprint);
        CREATE INDEX IF NOT EXISTS idx_vectors_chunk_profile ON vectors(chunk_id, embedding_profile_id);
        CREATE INDEX IF NOT EXISTS idx_vectors_context_profile ON vectors(context_key, embedding_profile_id);
        CREATE INDEX IF NOT EXISTS idx_vectors_profile_modality ON vectors(embedding_profile_id, context_key, modality);
        CREATE INDEX IF NOT EXISTS idx_ingest_runs_context_started ON ingest_runs(context_key, started_at);
        CREATE INDEX IF NOT EXISTS idx_freshness_context_checked ON freshness_snapshots(context_key, checked_at);
        CREATE INDEX IF NOT EXISTS idx_worker_runs_context_status ON worker_runs(context_key, status, started_at);
        CREATE INDEX IF NOT EXISTS idx_memory_context_sequence ON memory_records(context_key, sequence);
        CREATE INDEX IF NOT EXISTS idx_memory_context_live ON memory_records(context_key, is_deleted, updated_at);
        """
    )
    con.execute("INSERT OR REPLACE INTO database_metadata(key, value) VALUES (?, ?)", ("schema_version", SCHEMA_VERSION))
    con.commit()


def ensure_context(con: sqlite3.Connection, rt: Runtime) -> str:
    existing = con.execute("SELECT scope_id FROM contexts WHERE context_key = ?", (rt.context["context_key"],)).fetchone()
    now = utc_now()
    if existing:
        con.execute("UPDATE contexts SET updated_at = ? WHERE scope_id = ?", (now, existing["scope_id"]))
        return str(existing["scope_id"])
    scope_id = uuid7()
    con.execute(
        """
        INSERT INTO contexts(scope_id, tenant_slug, namespace_slug, corpus_slug, scope_slug, context_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scope_id,
            rt.context["tenant_slug"],
            rt.context["namespace_slug"],
            rt.context["corpus_slug"],
            rt.context["scope_slug"],
            rt.context["context_key"],
            now,
            now,
        ),
    )
    return scope_id
