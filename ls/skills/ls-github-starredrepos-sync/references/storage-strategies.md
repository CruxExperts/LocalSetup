# Storage Strategies

| Mode | Use | Committed content |
|---|---|---|
| `metadata` | First dry runs, current helper apply mode, and lightweight docs | Manifest, snapshots, docs |
| `submodule` | Roadmap only until guarded submodule creation is implemented | Not selectable by current helper |
| `checkout-cache` | Roadmap/local-only concept | Not selectable by current helper |
| `bare-mirror-cache` | Roadmap/local-only concept | Not selectable by current helper |
| `vendor` | Roadmap only; would require explicit license/privacy/size review | Not selectable by current helper |

`metadata` is the only supported runtime value for `STARREDREPOS_STORAGE_MODE` and for `manifest.json` today. The helper rejects every other value so archive manifests cannot imply a storage behavior that was not actually performed. Do not implement or enable `vendor` without confirming the user understands license, privacy, repository size, generated files, and secret-leak risk.
