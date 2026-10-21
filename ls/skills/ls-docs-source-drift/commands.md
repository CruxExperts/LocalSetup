# Documentation Alignment Command Reference

All commands are run from the repository root.

| Command | Purpose |
|---|---|
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . inventory` | Print the scanned docs, assets, CLI, CI, skills, workflows, and platforms. |
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . audit` | Print JSON findings without writing files. |
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . plan` | Group findings into generated, public-doc, lifecycle, asset, CI, and manual-review buckets. |
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . apply --scope generated` | Refresh generated alignment artifacts. |
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . apply --scope public` | Apply supported public-doc wording/count fixes. |
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . apply --scope assets` | Refresh asset manifest and `assets/README.md`. |
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . apply --scope all --dry-run` | Show write targets without mutating files. |
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . check --ci` | Read-only CI gate; exits non-zero for critical or major findings. |
| `uv run --locked python ls/tools/docs_alignment.py --repo-root . explain --claim-id skill_count` | Show backing source files for a claim. |
| `uv run --locked python ls/tools/localsetup.py --source-root . docs-align check --ci` | Run through the LocalSetup CLI wrapper. |
