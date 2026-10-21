# Generated Docs Policy

Generated docs are owned by tooling and should not be hand-edited.

## LocalSetup generated alignment files

- `ls/docs/_generated/docs-inventory.json`
- `ls/docs/_generated/docs-truth-map.json`
- `ls/docs/_generated/docs-audit-result.json`
- `ls/docs/_generated/docs-asset-manifest.json`
- `ls/docs/_generated/docs-alignment-summary.md`

## Update rules

- Use `generate_docs_artifacts.py` for the full generated-doc refresh.
- Use `docs_alignment.py apply --scope generated` for the alignment subset.
- Use managed blocks for volatile public facts.
- Keep `_generated/` and `local-context/` outside lifecycle version rewrites.
- Add new generated paths to version-sync candidates when they become release surfaces.
