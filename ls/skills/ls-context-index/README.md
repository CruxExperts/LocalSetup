# ls-context-index

`ls-context-index` provides two deliberately separate surfaces: a disposable SQLite retrieval cache over source files, and an opt-in authoritative memory store in the configured central/global SQLite database. File search never replaces direct source reads. Memory records carry declared source provenance and require explicit revisioned writes; they are not collected from a user's files automatically.

## What It Provides

- Vector-first local RAG using SQLite tables, SQLite FTS5, and deterministic local hash embeddings by default.
- Repo and framework context segregation through `tenant_slug`, `namespace_slug`, `corpus_slug`, `scope_slug`, and `context_key`.
- UUIDv7 IDs for relational rows so separately created SQLite databases can be merged later without ID collisions.
- Freshness, worklist, and agent-preflight JSON surfaces that tell agents which paths are stale, missing, deleted, or safe to search.
- Reset and rebuild controls for disposable index recovery.
- Explicit, revisioned memory records with local vector/FTS retrieval, tombstones, and private snapshots for read-only replicas.

## Default Storage

- Repo scope: `.localsetup/context-index/context-index.sqlite3`
- Framework scope: global DB, `~/.local/share/localsetup/context-index/context-index.sqlite3`

The repo DB is ignored by the default inventory rules and is not intended to be committed. The configured global DB can hold separate framework-index and memory contexts. Memory records and vectors are authoritative state in dedicated tables, not disposable index chunks; back up that DB separately.

## Core Commands

```bash
localsetup context-index doctor
localsetup context-index agent-preflight --scope repo
localsetup context-index freshness --scope repo
localsetup context-index worklist --scope repo
localsetup context-index stats --scope repo
localsetup context-index ingest --scope repo
localsetup context-index search "workflow registry" --scope repo --top-k 10
localsetup context-index lookup --chunk-id UUID
localsetup context-index vector-rebuild plan --scope repo
localsetup context-index vector-rebuild apply --scope repo --plan "<REVIEWED_PLAN_ID>"
localsetup context-index rebuild plan --scope repo
localsetup context-index rebuild apply --scope repo --plan "<REVIEWED_PLAN_ID>"
localsetup context-index prune plan --scope repo
localsetup context-index prune apply --scope repo --plan "<REVIEWED_PLAN_ID>"
localsetup context-index worker nudge --scope repo
localsetup context-index logs status --scope repo
localsetup context-index mcp config --scope repo
```

All agent-facing commands emit JSON. Agents should check `freshness`, `worklist`, or `agent-preflight` first, then use search only for paths that are not listed in `read_direct_paths`. For every plan/apply pair, review the plan's `ok`, `context_key`, `mode`, and proposed effect, then replace `<REVIEWED_PLAN_ID>` with its returned `plan_id`; never apply an unreviewed placeholder.

## Indexed Data

By default the repo scope indexes text-like files, docs, skills, workflows, generated context catalogs, code files, configuration files, and image metadata stubs. Obvious build/runtime/cache/noise paths are excluded, as are `.env`, vault, key, token, credential, log, and common secret-looking files.

Image files are represented as metadata-only text in this first implementation. The schema has `modality` and vector profile fields so richer CLIP or multimodal embeddings can be added without changing the agent command contract.

## Database Indexing

The SQLite schema is deliberately relational and future PostgreSQL-friendly. Common agent paths have native indexes:

- `sources(context_key, repo_relative_path)` for direct lookup.
- `sources(context_key, freshness_status, priority, repo_relative_path)` for freshness/worklists.
- `sources(tenant_slug, namespace_slug, corpus_slug, scope_slug, repo_relative_path)` for future merged/central databases.
- `chunks(context_key, repo_relative_path, line_start, line_end)` for provenance lookup.
- `vectors(context_key, embedding_profile_id)` and `vectors(embedding_profile_id, context_key, modality)` for vector scans by profile/scope.
- FTS5 virtual table `chunk_fts` for lexical fallback.

`stats` reports table counts, DB size, FTS availability, and index names. `prune` is conservative: it removes tombstoned/deleted source rows and orphan vector rows from the selected context, never source files.

## Central memory and replicas

`ls/core/context_index/memory_records.py` owns record validation and provenance;
`memory_snapshot.py` owns snapshot import, export, and replica checks;
`memory.py` retains write and search operations.

Before the first memory command, run `localsetup context-index config init --scope repo` in the writer repository. It persists a stable, repository-local `identity.memory_uuid`; two repositories with the same display name receive separate memory contexts even when sharing one central database. Preserve this identity when moving the same memory context to a replica: its repository config must contain the writer's verified UUID, not a newly generated one. Cloning that config deliberately selects the same identity, so verify the expected writer and repository before importing. Memory writes require an explicitly selected central/global database inside a private, owner-only directory. Supply text and a declared source (`type`, `ref`, `sha256`) as UTF-8 JSON on standard input, not in shell arguments. The source hash is a provenance claim supplied by the caller; record creation does not fetch or authenticate the source. Search takes the query on standard input and returns local vector/lexical results with source attribution.

`config init` may write an absolute `storage.global_database.path` for the writer host. On a replica, preserve only the verified `memory_uuid` identity and configure the global database path for that host; do not reuse the writer's absolute SQLite path or mount one live database on multiple servers. Keep the replica DB directory private (mode 0700) and the file owner-only (mode 0600).

Opening a pre-existing central database tightens only the framework's dedicated default database directory from mode 0755 to 0700 before SQLite access; a custom database path must already have a private mode-0700 parent and never changes an unrelated directory. The database file and WAL/SHM sidecars must be owner-only mode 0600. Group-writable or symlinked database ancestors fail closed rather than being changed. `config init` atomically writes an owner-only repository config and rejects symlinked config paths; review and repair an existing insecure config file instead of following its link.

```bash
printf '%s' '{"content":"Example source-grounded fact","source":{"type":"document","ref":"docs/example","sha256":"sha256:<64 lowercase hex digits>"}}' | localsetup context-index memory record --database global
printf '%s' 'source-grounded fact' | localsetup context-index memory search --database global --mode hybrid
localsetup context-index memory get UUID --database global
localsetup context-index memory update UUID --expected-revision 1 --database global < reviewed-update.json
localsetup context-index memory delete UUID --expected-revision 2 --database global
localsetup context-index memory export --database global --output /private/snapshot.json
localsetup context-index memory import --database global --input /private/snapshot.json
```

Only the central writer can record, update, or delete. An imported replica is read-only, accepts a newer consistent snapshot from the same writer, and rejects stale or conflicting state. Bounded record-hash lineage checks ancestry even when the replica skips several updates; it cannot prevent a malicious actor with write access to the writer database and transfer channel from fabricating a new history. Deletions remain as tombstones so an older snapshot cannot silently restore them. Export is a private mode-0600 file, not a transport: hashes detect changes but **do not authenticate the sender**. Move it to another server only through an operator-approved authenticated, private channel, then import it there. No server, synchronization service, or transport is started by these commands. The default local hash embedding needs no model download but has limited semantic recall; an explicitly configured loopback-compatible embedding endpoint can improve it.

## Configuration

Run `config init` to create a repository config and stable memory identity, then tune storage, includes/excludes, chunking, embedding provider names, model names, dimensions, retrieval weights, worker limits, and logging. Existing repo configs without an identity are updated in place; `config init` does not silently replace a valid UUID. The default provider is `local_hash`. Memory's optional loopback endpoint sends an Authorization header only if `embeddings.api_key_env` explicitly names a populated environment variable; an ambient `OPENAI_API_KEY` is not used as a fallback. The `openai_compatible`, `openai`, and `llama_cpp` names select the same HTTP adapter: configure the full embeddings endpoint, a compatible embedding model, and its returned dimension. OpenAI `/v1/embeddings` is supported. A llama.cpp `/v1/embeddings` server additionally needs an embedding-capable model with non-`none` pooling, normally served with `--embedding`. Other compatible services require provider-specific verification. See [schemas/config.schema.json](schemas/config.schema.json).

## Safety Contract

The file index is never canonical. If `freshness` marks a path as stale, changed, deleted, or not indexed, read that file directly. Reset/rebuild/prune operate on derived source rows, not durable memory. In contrast, explicit memory records are authoritative, must be backed up, and remain subject to their declared (not automatically verified) provenance and the caller's data-retention decisions.

## MCP Status

`mcp config` emits a deterministic optional MCP server configuration. MCP serving is intentionally not a hard dependency; `ls/tools/context_mcp_server.py` is present as the stable wrapper target and reports a structured optional-dependency error until an MCP SDK implementation is enabled.
