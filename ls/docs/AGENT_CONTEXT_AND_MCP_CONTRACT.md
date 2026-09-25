---
status: ACTIVE
version: 4.44
owner_skill: ls-context-index
---

# Agent context and MCP contract

## Decision

The LocalSetup context index is a derived retrieval cache, not an authority. Repository files, framework files, manifests, and generated catalogs remain the evidence source. Agents must perform freshness preflight, use retrieved text only when fresh, and directly read any path reported stale or in `read_direct_paths`.

The deterministic CLI is the canonical execution surface. MCP is an optional, read-only adapter and does not own index lifecycle, task orchestration, queues, dashboard control, Herdr, Agent Q, A2A, or OmniRoute routing.

## Stable retrieval request

A public retrieval request accepts only:

```json
{
  "scopes": ["repo", "framework"],
  "query": "string",
  "mode": "lexical | vector | hybrid",
  "top_k": 1
}
```

`repo` means the selected repository and `framework` means LocalSetup-owned sources. Their opaque provenance identity is `tenant_slug/namespace_slug/corpus_slug/scope_slug`. Consumers cannot choose an arbitrary database path, filesystem root, personal-memory corpus, or removed `global` scope.

Fresh sources use bounded lexical retrieval by default. Vector or hybrid ranking is an explicit mode, not an authority claim. It can refine discovery but never removes the direct-source-read escalation. The implementation must make lexical mode independent of embedding calls before this default is claimed as delivered.

## Freshness and result envelope

Before search, preflight every selected scope. The externally meaningful statuses are `fresh`, `not_indexed`, `changed`, `needs_reembed`, `deleted`, and reserved `unknown`; a deterministic ordered reasons list explains a non-fresh result. Do not promise a timestamp-only freshness SLA.

A normalized search result has only this semantic shape:

```json
{
  "rank": 1,
  "score": 0.0,
  "scope": "repo",
  "context_key": "opaque identity",
  "path": "repo-relative/path.md",
  "line_start": 1,
  "line_end": 1,
  "heading_path": ["Heading"],
  "snippet": "bounded text",
  "chunk_id": "opaque id",
  "provenance": {
    "source_hash": "sha256",
    "chunk_hash": "sha256",
    "indexed_at": "timestamp",
    "source_mtime": "timestamp"
  },
  "freshness": {"stale": false, "status": "fresh", "reasons": []}
}
```

`lookup(chunk_id)` is the explicit full-content escalation and returns the same provenance and freshness fields plus `content`. Vector/lexical component scores are optional diagnostics. SQLite schema, vector blobs, embedding provider/model/endpoint, hybrid weights, worker state, logs, and storage locations are implementation details.

## Privacy boundary

Index inventory excludes secrets, credentials, keys, certificates, vaults, logs, caches, dependencies, builds, virtual environments, and runtime state before content enters storage. A response may return a documented alias name but never resolve or emit a secret value.

Public payloads contain relative paths only. They must not expose absolute paths or source URIs, embedding configuration or credentials, vector blobs, raw log records, or direct SQLite rows. Content-pattern redaction is not currently implemented; until it is, path exclusion is the enforceable control.

## MCP status and future surface

Current status: `context-index mcp config` emits a launch record, but `ls/tools/context_mcp_server.py` always reports `MCP_SERVER_OPTIONAL`. There is no working MCP server or callable MCP tool in this build. Consumers must use the CLI.

A future MCP server is a thin stdio adapter over the same public operations, never direct SQLite access. Its default read-only tools may be `preflight`, `search`, `lookup`, `stats`, and an ingest/worklist **plan**. It must not expose ingest, worker execution, rebuild/reset/prune apply, configuration initialization, task dispatch, or any control-plane capability.

## Implementation gates

Before enabling MCP or changing agent guidance, reconcile the published schema with runtime validation; normalize the flattened freshness fields in current search output; remove the hard-coded `status: "UNKNOWN"`; prevent embeddings in lexical mode; and add content redaction before storage/output if that guarantee is desired. Focused tests belong in `ls/tests/test_context_index.py` and must cover selector rejection, normalized envelopes, stale direct-read escalation, public-payload privacy, lexical no-embedding behavior, and MCP capability status.

See [FRAMEWORK_LIBRARY_ARCHITECTURE.md](FRAMEWORK_LIBRARY_ARCHITECTURE.md) for dependency and control-plane boundaries, and the `ls-context-index` architecture reference for current implementation details.
