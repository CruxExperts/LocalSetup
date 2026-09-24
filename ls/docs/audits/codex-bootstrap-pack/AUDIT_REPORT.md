---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-audit
date: 2026-05-10
---

# Codex Bootstrap-Pack Audit Report

## Objective

Audit the prior global Codex agent-team bootstrap, verify current Codex CLI behavior from evidence, create a reusable Codex-first bootstrap-pack structure in Localsetup, and produce a deterministic remediation plan without destructive or approval-gated changes.

## Scope

Repo root: `<repo-root>`

Artifact root: `ls/docs/audits/codex-bootstrap-pack/`

Bootstrap pack root: `ls/docs/bootstrap-packs/codex-agent-team/`

Global Codex home: `<codex-home>` because `CODEX_HOME` was unset.

## Mission Answers

1. Prior global Codex bootstrap implementation: substantially implemented. The expected global files exist and parse: `config.toml`, `AGENTS.md`, `AGENT_TEAM_RUNBOOK.md`, and five role TOMLs under `agents/`. Prompt-load evidence showed global instructions loading outside this repo.
2. Discrepancies and quality issues: medium hardening findings remain. Global instructions lack an explicit no-subagent/user-constraint override, global instruction/config surfaces are group-writable, `[agents].max_threads = 6` lacks a normal concurrency guideline, and runbook wording drifts from actual `xhigh` reasoning config.
3. Current Codex CLI coherence: coherent with local `codex-cli 0.130.0` evidence. `codex debug prompt-input` is the strongest local prompt-load check found. Custom-agent TOML works locally, but a complete public custom-agent schema was not found and global/repo `AGENTS.md` merge behavior is still somewhat ambiguous.
4. Reusable bootstrap-pack structure: now present. The `bootstrap` pack is registered in `ls/config/pack.yaml`, and the Codex-first docs/metadata live under `ls/docs/bootstrap-packs/`.
5. Codex-first with future room: yes. Metadata sets `primary_platform: codex` and lists future platforms without claiming they are implemented.
6. Deterministic remediation plan: present in `REMEDIATION_PLAN.md` and `REMEDIATION_TASKS.yaml`.
7. Safe legacy replacement workflow: present. Legacy replacement is dry-run-first, hash-based, and approval-gated for external folders and global config.
8. Documentation organization: updated through existing indexes. `ls/docs/README.md`, `ls/docs/AGENTIC_DESIGN_INDEX.md`, `ls/docs/_generated/skill-packs.md`, and `ls/docs/_generated/implementation-file-map.md` now include the bootstrap-pack surfaces.

## Subagent Reports

| Report | Purpose |
|---|---|
| [codex-bootstrap-auditor.md](agent-reports/codex-bootstrap-auditor.md) | Strict read-only audit of global Codex bootstrap files. |
| [repo-bootstrap-pack-explorer.md](agent-reports/repo-bootstrap-pack-explorer.md) | Repo pack/index/workflow/template structure mapping. |
| [codex-cli-researcher.md](agent-reports/codex-cli-researcher.md) | Current/version-bound Codex CLI behavior research. |
| [legacy-inventory.md](agent-reports/legacy-inventory.md) | Safe inventory of legacy skill/prompt/config locations. |
| [final-reviewer.md](agent-reports/final-reviewer.md) | Final read-only acceptance review. |

## Bootstrap-Pack Changes Made

- Added `bootstrap` to `ls/config/pack.yaml`.
- Added `ls/docs/bootstrap-packs/INDEX.md`.
- Added `ls/docs/bootstrap-packs/codex-agent-team/README.md`.
- Added `ls/docs/bootstrap-packs/codex-agent-team/AUDIT_PROMPT.md`.
- Added `ls/docs/bootstrap-packs/codex-agent-team/metadata.yaml`.
- Added a concise pointer to `ls/templates/codex/AGENTS.md`.
- Updated public docs indexes and regenerated generated pack/file maps.

## Approval Boundaries Observed

No global config files, home-directory files, sibling repos, external runtime mirrors, auth files, secret stores, permissions, symlinks, or legacy trees were modified.

## Key Conclusion

The prior global bootstrap is usable but not fully hardened. The repo now has a reusable Codex-first bootstrap-pack structure and durable audit/remediation artifacts. The remaining changes are intentionally staged as remediation tasks because they touch global config, external folders, or higher-risk runtime behavior.
