---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-audit
date: 2026-05-10
---

# Codex Bootstrap Auditor Report

## Summary

Audit scope was read-only. `CODEX_HOME` was unset, so the active global Codex home resolved to `<codex-home>`. Expected bootstrap files were present, TOML files parsed where applicable, and `codex debug prompt-input` from outside the repo showed the global `AGENTS.md` instructions loading.

No critical or high-severity correctness failures were found. Medium and low hardening issues remain around over-delegation exceptions, permissive global file modes, and minor config/runbook wording drift.

## Evidence Inspected

| Evidence | Result |
|---|---|
| `CODEX_HOME` | Unset; active home `<codex-home>` |
| `<codex-home>/config.toml` | Exists; TOML parse OK |
| `<codex-home>/AGENTS.md` | Exists; loaded by `codex debug prompt-input` outside the repo |
| `<codex-home>/AGENT_TEAM_RUNBOOK.md` | Exists |
| `<codex-home>/agents/*.toml` | Five role files exist and parse |
| `<codex-home>/config.toml.bak-20260510-020636` | Exists; supports preservation check |
| `<codex-home>/models_cache.json` | Contains `gpt-5.5`, `gpt-5.4-mini`, and `gpt-5.3-codex` |
| `<codex-home>/runs` | Absent |

## Findings

| ID | Severity | Path | Evidence | Recommendation |
|---|---|---|---|---|
| CBA-001 | medium | `<codex-home>/AGENTS.md` | Final protocol requires researcher/reviewer for non-trivial work but lacks an explicit exception for user instructions such as no-subagent constraints. | Add an override clause to global instructions. Requires approval because it changes global config. |
| CBA-002 | medium | `<codex-home>`, `<codex-home>/agents/*.toml` | Directory modes were reported as `775`; instruction/TOML files as `664`. These are behavior-control surfaces. | Consider user-only modes if no shared-group workflow depends on current modes. Requires approval. |
| CBA-003 | medium | `<codex-home>/config.toml` | `[agents].max_threads = 6` plus automatic team-mode triggers can encourage broad delegation. | Add a normal 1-2 concurrent-agent guideline or lower the limit after approval. |
| CBA-004 | low | `<codex-home>/AGENT_TEAM_RUNBOOK.md` | Runbook wording says high reasoning while config uses `xhigh`. | Align wording with actual config after approval. |
| CBA-005 | info | `<codex-home>/runs` | No global run ledger exists. | No action unless global bootstrap tasks should write global ledgers. |

## Safety Review

Existing safety and reasoning settings were preserved: `sandbox_mode = "danger-full-access"`, granular approval policy, and `xhigh` reasoning remained present. Role TOMLs were reasonably bounded: read-only roles use read-only sandboxing, while worker/tester use workspace-write.

## Approval-Required Items

- Modify global `<codex-home>/AGENTS.md`.
- Modify global `<codex-home>/AGENT_TEAM_RUNBOOK.md`.
- Lower `<codex-home>/config.toml` `[agents].max_threads`.
- Change permissions under `<codex-home>`.

## Missing Validation

The auditor did not mutate files, inspect auth files, or verify a full custom-agent spawn path. TOML syntax and prompt loading were checked.
