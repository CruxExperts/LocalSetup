---
name: ls-wf-repository-localsetup-conversion
description: Use when converting an existing repo to the current LocalSetup framework with backup, blocker, install, and verification gates.
metadata:
  version: "1.0"
---

Use this workflow when onboarding a repository that may already contain old LocalSetup files, adapter paths, lockfiles, or framework source.

Every conversion starts with a report-only run. Use one shell session so the report and apply steps share the exact target and backup paths. Do not add `--yes` to this first command:

```bash
target_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd -P)"
backup_dir="$target_root/.localsetup/backups/conversion-$(date -u +%Y%m%dT%H%M%SZ)"
localsetup convert --target-directory "$target_root" --backup-dir "$backup_dir" --tools codex --packs core
```

Record the report output and stop unless it provides all of this evidence:

- blockers are empty; unmanaged or ambiguous content is a blocker
- the reported LocalSetup source checkout and target repository root are the intended roots
- the report names the same timestamped `backup_dir` and its `conversion-report.json` path

Only after that evidence is explicit may you run the applying command with the unchanged variables:

```bash
localsetup convert --target-directory "$target_root" --backup-dir "$backup_dir" --tools codex --packs core --yes
```

After apply, confirm the recorded backup and report exist, then run the verification and doctor commands in `ls/docs/REPO_CONVERSION.md`. Follow that guide for source-vs-target behavior, blocker handling, and rollback evidence.
