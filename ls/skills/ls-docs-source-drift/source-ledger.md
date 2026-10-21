# Source Ledger

This ledger defines the default LocalSetup truth-map adapter. Other repos can copy the pattern and replace the source rows with their own manifests.

| Claim | Source files | Notes |
|---|---|---|
| Framework version | `VERSION`, `pyproject.toml`, `ls/docs/_generated/facts.json` | `VERSION` is authoritative; generated and package surfaces must match. |
| Supported platforms | `ls/config/platforms.yaml` | Human docs summarize this manifest. |
| Shipped skills | `ls/skills/ls-*/SKILL.md`, `ls/config/pack.yaml` | Count skill manifests and validate pack assignment. |
| Workflow packages | `ls/workflows/ls-wf-*/workflow.yaml`, `ls/config/pack.yaml` | Count workflow manifests and validate workflow pack assignment. |
| Generated docs | `ls/tools/generate_docs_artifacts.py`, `ls/core/docs.py` | Generated files are refreshed by tooling, not hand-edited. |
| Assets | `assets/*`, Markdown image references | Asset metadata includes dimensions, references, provenance, and license notes. |
| CI gates | `.github/workflows/*.yml` | Docs alignment runs after generation and before diff checks. |
