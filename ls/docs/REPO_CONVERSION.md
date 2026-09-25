---
status: ACTIVE
version: 4.44
owner_package: ls-workflow-pipeline-repo-convert
---

# Repo Conversion

Use repo conversion when a project may contain old LocalSetup framework files, old adapter paths, stale lock metadata, or managed global skills from earlier releases.

The global command uses the registered LocalSetup source checkout and treats the nearest Git worktree root from the invocation directory as the target. Outside Git, the exact current directory is the target. Override that behavior with `--target-directory`.

## Dry Report

```bash
localsetup convert --tools codex --packs core
```

Without `--yes`, conversion only reports the source root, target root, backup path, artifacts, and blockers. Treat blockers as stop signs. Unmanaged adapter directories, unmanaged legacy global skills, and other ambiguous project-owned content need human review before apply. Adapter-shaped paths such as current `.agents/skills`, `.cursor/skills`, and historical `.codex/skills` are shared agent surfaces; custom content in them is repo-owned by default. The Codex transition removes only entries proven LocalSetup-managed by managed-root targeting, adapter markers, or recorded lock ownership. Unproven symlinks require review and are never adopted as a fallback alias.

## Apply

```bash
localsetup convert --tools codex --packs core --yes
```

Apply mode creates a timestamped backup under `.localsetup/backups/conversion-*`, writes `conversion-report.json`, archives known LocalSetup lock/framework artifacts, syncs the current `ls` source when the target is a separate repo, installs the selected packs and adapters, and verifies the result.

Platform selection stays explicit. CWD and Git-root detection choose where selected adapters go; they do not choose which adapters are installed.

## Source And Target

- Source checkout: the LocalSetup framework registered in `~/.local/bin/localsetup`.
- Target repo: nearest Git worktree root from the command CWD, or CWD outside Git.
- Override: `--target-directory /path/to/project`.

Direct source-checkout commands such as `uv run --locked python ls/tools/localsetup.py install --apply` keep their existing source-local default unless `--target-directory` is provided.

## Verification

After conversion, run:

```bash
localsetup verify --tools codex
localsetup doctor --tools codex
```

Use the conversion report and `.localsetup/lock.json` as the evidence trail for follow-up rollback or audit work. Conversion backs up and removes stale target `ls/` folders; it does not copy framework source into the target.
