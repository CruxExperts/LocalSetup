---
status: ACTIVE
version: 5.6
owner_package: docs-align
localsetup_provenance:
  schema_version: 1
  source_provenance_hash: b7d090411ce53ede878c17718564e2afbb6db38302cd0f881e2e013fe62ae070
  emitter: docs-align
framework_version: 5.6.2
source_commit: 6a31edc4072e5f0a9608c4dd246912d0759248c3
artifact_sha256: 8eabd8aa9cbcdf7ffd000b63360417969e1cee0e16c8f9f08ac9342cfeb328d6
---
# Documentation Alignment Summary

This page is generated from repository inventory, source-truth manifests, asset metadata, and the docs-alignment audit.

| Signal | Value |
|---|---:|
| Version | `5.6.2` |
| Documentation files inventoried | 510 |
| Immutable upstream documents | 64 |
| Shipped skills | 105 |
| Workflow packages | 18 |
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
