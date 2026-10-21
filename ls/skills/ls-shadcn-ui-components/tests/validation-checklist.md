# Validation Checklist

Run this entire checklist from the LocalSetup repository root.

- `uv run --locked python ls/skills/ls-shadcn-ui-components/scripts/verify_shadcn_sources.py --help`
- `uv run --locked python ls/skills/ls-shadcn-ui-components/scripts/verify_shadcn_sources.py`
- `uv run --locked python ls/skills/ls-shadcn-ui-components/scripts/verify_shadcn_sources.py --json`
- `uv run --locked python ls/skills/ls-shadcn-ui-components/scripts/verify_shadcn_sources.py --refresh --json`
  when current upstream source facts matter.
- `uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog`
- `uv run --locked python ls/tools/skill_validation_scan.py ls/skills --scan-root . --no-fetch`
- `agentskills validate ls/skills/ls-shadcn-ui-components` when available.
- `uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .`
- `uv run --locked python ls/tools/localsetup.py --source-root . generate-docs`
- `git diff --check`

For project-specific shadcn/ui work, also run the target project's typecheck,
lint, tests, and build scripts when available.
