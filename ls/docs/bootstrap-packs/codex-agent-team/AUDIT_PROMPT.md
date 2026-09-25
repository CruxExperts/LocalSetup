---
status: ACTIVE
version: 5.7
owner_skill: ls-framework-compliance
---

# Codex Plan-Mode Prompt: Bootstrap-Pack Audit

Use this prompt when the current repository is LocalSetup and the goal is to audit or refresh the generic global Codex agent-team bootstrap.

## Mission

Act as the main Codex controller, planner, auditor, and verifier. Inspect whether the global Codex bootstrap is implemented correctly, completely, and deterministically as a generic subagent-first controller model for non-trivial work with bounded fanout. Create or update the repo-local bootstrap-pack artifacts needed to reuse, version, audit, and later adapt this workflow for other agent frameworks.

Answer these questions:

1. Was the prior global Codex bootstrap implemented as requested?
2. Are there discrepancies, conflicts, omissions, unsafe assumptions, or quality issues?
3. Are the Codex global instructions, custom agents, runbook, and config coherent with current Codex CLI behavior?
4. Do the instructions favor subagent-first routing for non-trivial work, small verifiable task units, low main-context growth, direct mode only for trivial or constrained cases, and bounded delegation rather than specialist proliferation?
5. Is there a reusable bootstrap-pack structure in LocalSetup?
6. Does that structure target OpenAI Codex CLI first while leaving room for other frameworks later?
7. Is there a deterministic remediation plan for anything missing?
8. Is there a safe workflow for identifying and replacing old or legacy LocalSetup skills, prompts, and configs without destructive changes?
9. Are documentation artifacts organized, indexed, and updated rather than duplicated?

## Operating Constraints

- Use native Codex concepts only.
- Do not invent a separate daemon, scheduler, database, or agent framework.
- Do not add specialist role files when the existing generic `explorer`, `researcher`, `worker`, `tester`, and `reviewer` roles can handle the task through scoped prompts.
- Do not make destructive changes.
- Do not delete legacy skills or configs.
- Do not overwrite framework docs without inspecting them first.
- Do not modify global config, external folders, or network state without user approval.
- In Plan Mode or other no-write contexts, plan ledger entries and subtasks but do not create ledgers or edit state until writes are allowed.
- Do not claim completion without evidence.
- Do not rely on model memory for current CLI behavior.

## Recommended Subagent Scopes

Use narrow native Codex subagents whenever they can materially improve throughput, preserve context, reduce risk, or strengthen verification:

- `reviewer`: audit `$CODEX_HOME` or `~/.codex` bootstrap files for risk, conflicts, scope, and evidence.
- `explorer`: map repo pack, workflow, docs, template, index, and validation surfaces.
- `researcher`: verify current or version-matched Codex CLI behavior from local help, official docs, or source-backed evidence.
- `explorer`: inventory legacy LocalSetup skills, prompts, and configs without changing them.
- `reviewer`: final read-only review of artifacts and validation evidence.

Subagents must not spawn subagents.

## Required Artifacts

Write artifacts under `ls/docs/audits/codex-bootstrap-pack/`:

- `AUDIT_REPORT.md`
- `FINDINGS.yaml`
- `REMEDIATION_PLAN.md`
- `REMEDIATION_TASKS.yaml`
- `VALIDATION.md`
- `agent-reports/*.md`

Also ensure bootstrap-pack index and metadata are present:

- `ls/docs/bootstrap-packs/INDEX.md`
- `ls/docs/bootstrap-packs/codex-agent-team/metadata.yaml`

## Acceptance

Before final response, build a prompt-to-artifact checklist and verify every explicit requirement against real files, command output, parsed YAML/TOML/JSON, inspected diffs, and reviewer evidence.
