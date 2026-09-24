---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-audit
date: 2026-05-10
---

# Legacy Inventory Report

## Summary

The current repo holds canonical Localsetup source surfaces under `ls/skills`, `ls/templates`, and `ls/config`. Repo-local hidden dirs for Cursor, Kilo, and OpenCode were shallow placeholders. The strongest legacy duplicate trees were `<legacy-localsetup-repo>/.agents/skills` and `~/.codex/skills`, both using older `localsetup-*` names with sampled hashes that differ from current repo source.

`~/.local/share/localsetup/packages` appears to be the current runtime mirror for sampled `ls-*` skills.

No auth files, token stores, KeePass databases, mail stores, or browser profiles were opened.

## Inventory

| Location | Status | Evidence |
|---|---|---|
| `ls/skills` | Canonical repo source | Current repo source has 50 `ls-*` skill dirs and 50 `SKILL.md` files; this includes additions made after the original 2026-05-10 inventory |
| `ls/templates` | Canonical prompt/template source | 14 template files reported |
| `ls/config` | Canonical config source | 9 config/schema/example files reported |
| `.codex` in current repo | Operational history | run artifacts only at shallow depth |
| `.cursor`, `.kilo`, `.opencode` in current repo | Empty placeholders | no files at shallow depth |
| `<legacy-localsetup-repo>/.agents/skills` | Legacy duplicate candidate | 50 skill dirs, older `localsetup-*` names, sample hashes differ |
| `~/.codex/skills` | Legacy duplicate candidate | 55 skill dirs, older `localsetup-*` names, sample hashes differ |
| `~/.local/share/localsetup/packages` | Current runtime mirror candidate | 61 `ls-*` dirs; sampled hashes matched current repo source |
| `~/.agents` | Adjacent non-Localsetup skills | no Localsetup skill tree surfaced |
| `~/.cursor`, `~/.config/kilo`, `~/.config/opencode` | Adjacent app state | no Localsetup-specific files surfaced in this pass |

## Safe Replacement Workflow

1. Build a machine-readable canonical manifest from the current repo only.
2. Add an alias map from old `localsetup-*` names to current `ls-*` names.
3. Compare candidate targets by hash and path family in a dry-run report.
4. Replace only one approved target tree at a time, with backup, temp write, hash verification, and atomic swap.
5. Re-scan after replacement and record before/after manifests.

## Approval Boundaries

Any write, delete, rename, symlink, permission, or package-install action outside the current repo requires explicit user approval. That includes `<legacy-localsetup-repo>`, `~/.codex`, `~/.agents`, `~/.cursor`, `~/.config/kilo`, `~/.config/opencode`, and `~/.local/share/localsetup/packages`.

## Recommended Remediation Tasks

- Add a deterministic canonical skill index with path and hash entries.
- Add a normalized alias map from `localsetup-*` to `ls-*`.
- Add a drift-report script or generated artifact comparing repo source with runtime mirrors.
- Migrate `~/.codex/skills` and `<legacy-localsetup-repo>/.agents/skills` only after approval.
