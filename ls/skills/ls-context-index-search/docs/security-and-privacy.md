# Security And Privacy

The file index is sensitive derived state. Explicit central memory and its exported snapshots are more durable: they contain source-grounded text and declared provenance that cannot be reconstructed automatically after deletion.

## Default Excludes

The default inventory excludes common noise and secret-bearing paths:

- `.git/`, `.venv/`, `venv/`, `node_modules/`, cache/build/dist/coverage folders, `__pycache__/`, `.pytest_cache/`
- `.localsetup/context-index/`, `ls/.cache/`, `ls/logs/`, local maintenance/state folders
- `*.log`, `.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.kdbx`
- path names containing `secret`, `credential`, or `token`

Maintainers can inspect exclusions with:

```bash
localsetup context-index inventory --scope repo --show-excludes
```

## Secret Policy

- Do not resolve secret aliases during indexing.
- Do not index vault files, private keys, token stores, `.env` files, or credential dumps.
- It is acceptable to index intentional alias names such as `secret_ref: cloudflare_api_token` when they are already present in documentation.
- Logs must not contain raw secrets.

Memory is opt-in and requires a repository-local stable UUID created by `config init --scope repo`; copy that verified identity only to a replica of the same logical repository. Recording requires a caller-supplied source type, reference, and SHA-256 claim; the CLI does not fetch or authenticate that source. Verify it before recording, and treat imported memory as untrusted content, never as operational instructions. Supply memory text and search queries through bounded standard input, not process arguments; never record resolved secrets. Keep the central SQLite database in an owner-only, symlink-safe private directory, its sidecars and exported snapshots owner-only. A snapshot hash and bounded revision lineage detect corruption or divergent descendants, not a malicious sender: only import data from the expected writer after an authorized authenticated private transfer.

## Network Policy

File search and ingest are local by default. Memory defaults to deterministic `local_hash` plus FTS, with limited semantic recall and no model download. For memory, an explicitly configured OpenAI-compatible embedding service must be loopback-only and is called without environment proxies or redirects; remote endpoints fail closed. It receives an Authorization header only if `embeddings.api_key_env` explicitly selects a populated environment variable; ambient `OPENAI_API_KEY` is not sent. File-index embeddings have their own existing configuration and must not be enabled for private repos unless the operator approves that separate data boundary.

## Reset And Rebuild

Reset and rebuild delete derived file-index rows for the selected context and re-ingest from files. They do not delete authoritative memory tables. Back up the central database and its snapshots independently; `prune` for the file index does not garbage-collect memory tombstones, which protect replicas against stale resurrection.

## Privacy Boundary

Repo file scope stays in the repo DB by default. Framework scope uses the global DB; explicit memory uses a separate context in that configured global DB. Importing a snapshot creates a read-only replica, not another writer. Replication commands do not move data over a network or authorize a target server.
