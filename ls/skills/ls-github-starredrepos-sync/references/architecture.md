# Architecture

The skill has four layers:

1. **Context verification:** confirm `gh` authentication, viewer identity, available API versions, and rate-limit state.
2. **Inventory:** list starred repositories through REST, preserving `starred_at` when available.
3. **Archive planning:** compare inventory to the `starredrepos` repository contract and produce a manifest plus snapshot diff.
4. **Optional enrichment:** scout repositories with deterministic static metadata first, then optional command/model scouting when explicitly enabled.

Scripts are Node ESM and use only built-in modules. They shell out through `spawnFile`-style calls with `shell: false`, and they pass JSON between stages.

## Data Flow

```text
gh auth -> verify-github-auth -> list-starred-repos -> sync-starredrepos
                                          |                    |
                                          v                    v
                                  repo metadata          manifest + diff
                                          |                    |
                                          v                    v
                                  scout reports        generated docs
```

## Boundary

The archive repository is currently for metadata, docs, and manifests. Submodule pointers and vendored copies are roadmap storage strategies only; this helper rejects non-metadata storage modes until guarded implementations and tests exist.
