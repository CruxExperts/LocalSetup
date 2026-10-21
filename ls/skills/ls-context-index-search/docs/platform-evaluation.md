# Platform Evaluation

Decision recorded: 2026-05-12. Upstream capability review refreshed: 2026-09-04.

## Decision

LocalSetup selects SQLite as its default context-index backend, with FTS5 for lexical search and ordinary relational tables for vector payloads, freshness metadata, and source provenance. This is a project architecture decision for LocalSetup's portability and no-service requirements, not a universal database ranking. Keep the schema generic enough to migrate to PostgreSQL later.

This follows the user's portability requirement: each enabled repo can have its own copyable SQLite DB, while framework context uses one global SQLite DB. Context columns (`tenant_slug`, `namespace_slug`, `corpus_slug`, `scope_slug`, `context_key`) let one database contain many repos or let several repo DBs be merged into a central database later.

## Candidate Summary

This table separates current upstream capabilities from LocalSetup-specific integration judgments. Dependency size, operational footprint, and relative performance were not benchmarked with pinned versions; re-evaluate them before adding an adapter.

| Candidate | Current upstream capability | LocalSetup consideration |
|---|---|---|
| SQLite FTS5 + relational vector blobs | SQLite provides FTS5 and WAL; LocalSetup implements vector storage in ordinary tables. | Selected default for the existing local, copyable, no-service design. |
| PostgreSQL + pgvector/FTS | PostgreSQL provides full-text search; pgvector adds exact and approximate nearest-neighbor search with SQL filtering. | Retained as a possible future central-store target, not an implemented backend. |
| LanceDB | The open-source library runs embedded and supports vector, full-text, and hybrid search. | Would add a new dependency and storage adapter; no adapter is currently approved. |
| Qdrant | Qdrant supports payload filtering and server/Docker deployment; its official MCP project also documents local mode. | Would require a separate adapter and LocalSetup-owned provenance, freshness, and privacy controls. |
| Chroma | Python supports in-memory and persistent clients plus metadata filtering; other SDK local-persistence paths may require a server. | Does not replace LocalSetup's current relational freshness/worklist control plane. |
| Meilisearch | A self-hosted or cloud service supporting keyword, semantic, and hybrid search. | Service operation is outside the current repo-local default. |
| Tantivy | A Rust full-text-search library using BM25. | Lexical retrieval alone would still need LocalSetup metadata and freshness storage. |
| BM25S | A Python/NumPy BM25 library. | Lexical retrieval alone would still need LocalSetup metadata and freshness storage. |
| Milvus Lite | An embedded local-file mode exposed through `pymilvus`, with vector, filtering, and hybrid features subject to documented limits. | No adapter is implemented; evaluate its limits and dependency surface for a concrete use case. |
| Weaviate | Supports server/cloud deployment and hybrid vector+BM25 search; embedded mode is experimental and can download a binary. | No adapter is implemented; embedded binary acquisition needs a separate supply-chain decision. |
| FAISS | A C++ library with Python wrappers for dense-vector similarity search and clustering. | Metadata, freshness, and control-plane behavior would remain a LocalSetup integration responsibility. |
| LangChain | A modular retrieval and RAG framework. | Architecture reference only; not a context-index backend dependency. |
| LangGraph | An orchestration runtime with persistence primitives. | Architecture reference only; not an index backend. |

## Why SQLite

SQLite is already compatible with LocalSetup's local-first and Python-first stance. It gives deterministic file-level state, WAL support, ordinary indexes, `sqlite_master`-visible schema validation, FTS5 lexical search, and low-friction test execution without network or Docker. The implementation stores vectors as packed float blobs and scans by `context_key` + embedding profile; this is acceptable for repo-scale indexes and keeps the schema easy to migrate to PostgreSQL. Embeddings default to deterministic local hash vectors for offline use, with an explicit OpenAI-compatible HTTP adapter for hosted APIs or local llama.cpp-style servers.

## Native Index Strategy

The schema includes explicit indexes for common agent and worker paths:

- Source lookup: `idx_sources_context_path`
- Freshness/worklist: `idx_sources_context_freshness`, `idx_sources_context_priority_status`
- Merge/consolidation: `idx_sources_scope_lookup`, `idx_sources_context_fingerprint`
- Provenance lookup: `idx_chunks_source_line`, `idx_chunks_context_line_lookup`
- Vector search profile filtering: `idx_vectors_context_profile`, `idx_vectors_profile_modality`
- Worker visibility: `idx_worker_runs_context_status`
- Lexical fallback: `chunk_fts` FTS5 virtual table

These indexes are deliberately SQL-native so agents and tests can validate them with ordinary SQLite introspection.

## LangChain And LangGraph Notes

LangChain documents modular retrieval/RAG patterns, while LangGraph documents orchestration and persistence. LocalSetup treats them as architecture references rather than runtime or storage dependencies. Its command surface remains deterministic, local-first, and aligned with framework files through its own CLI and schema.

## Sources Checked

- LangChain RAG docs: https://docs.langchain.com/oss/python/langchain/rag
- LangGraph repository: https://github.com/langchain-ai/langgraph
- LangGraph product/docs overview: https://www.langchain.com/langgraph
- SQLite FTS5: https://www.sqlite.org/fts5.html
- UUIDv7 / RFC 9562: https://www.rfc-editor.org/rfc/rfc9562
- PostgreSQL UUID docs: https://www.postgresql.org/docs/current/datatype-uuid.html
- PostgreSQL full-text search: https://www.postgresql.org/docs/current/textsearch.html
- pgvector: https://github.com/pgvector/pgvector
- LanceDB quickstart and hybrid search: https://docs.lancedb.com/quickstart and https://docs.lancedb.com/search/hybrid-search
- Qdrant filtering, deployment, and MCP: https://qdrant.tech/documentation/search/filtering/ , https://qdrant.tech/documentation/quick-start/ , and https://github.com/qdrant/mcp-server-qdrant
- Chroma clients and metadata filtering: https://docs.trychroma.com/docs/run-chroma/clients and https://docs.trychroma.com/docs/querying-collections/metadata-filtering
- Meilisearch self-hosting and hybrid search: https://www.meilisearch.com/docs/resources/self_hosting/getting_started/install_locally and https://www.meilisearch.com/docs/capabilities/hybrid_search/advanced/semantic_vs_hybrid
- Tantivy and BM25S: https://github.com/quickwit-oss/tantivy and https://github.com/xhluca/bm25s
- Milvus Lite: https://milvus.io/docs/milvus_lite.md
- Weaviate deployment, embedded mode, and hybrid search: https://docs.weaviate.io/deploy/installation-guides , https://docs.weaviate.io/deploy/installation-guides/embedded , and https://docs.weaviate.io/weaviate/concepts/search/hybrid-search
- FAISS: https://github.com/facebookresearch/faiss
