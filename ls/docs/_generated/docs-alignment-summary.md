---
status: ACTIVE
version: 4.66
owner_package: docs-align
localsetup_provenance:
  schema_version: 1
  source_provenance_hash: ca3f2438331173db53d0e10c0bf037a143aacc3481c7b17b0db79b08770340e9
  emitter: docs-align
framework_version: 4.66.0
source_commit: 99af232b43f6da4b06576bf44b889281332c57b5
artifact_sha256: 444856399a2873d93321db024f4a45f0402799ea3397adc3daace36a3bf43258
---
# Documentation Alignment Summary

This page is generated from repository inventory, source-truth manifests, asset metadata, and the docs-alignment audit.

| Signal | Value |
|---|---:|
| Version | `4.66.0` |
| Documentation files inventoried | 560 |
| Immutable upstream documents | 64 |
| Shipped skills | 116 |
| Workflow packages | 21 |
| Supported platforms | 20 |
| Audit findings | 1 |
| Critical findings | 0 |
| Major findings | 1 |

## Generated Artifacts

- `docs-inventory.json`: scanned docs, skills, workflows, assets, CI workflows, and CLI commands.
- `docs-truth-map.json`: claims and their backing source files.
- `docs-audit-result.json`: JSON-first findings for drift and Markdown/doc hygiene.
- `docs-asset-manifest.json`: asset metadata and references.

## Findings

- `major` `stale_count` ls/docs/FEATURES.md:54: hard-coded shipped skill/workflow count is stale
