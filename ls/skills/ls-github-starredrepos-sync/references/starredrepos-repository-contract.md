# Starredrepos Repository Contract

Default repository name: `starredrepos`.
Default local worktree when `STARREDREPOS_WORKTREE` is unset: `~/starredrepos`.

Recommended layout:

```text
README.md
manifest.json
snapshots/
  latest.json
  diffs/
docs/
  repos/
scouts/
  reports/
modules/
```

## Rules

- `manifest.json` follows `data/schema/manifest.schema.json`.
- `snapshots/latest.json` is the latest complete inventory.
- `snapshots/diffs/` stores diff reports between runs.
- `docs/repos/` stores generated per-repository notes.
- `modules/` is reserved for a future guarded submodule mode. The current helper does not create or select submodules.
- Local checkout caches and bare mirrors are never committed.
