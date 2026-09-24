---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-compliance
---

# OpenCode Plan-Mode Prompt: Agent-Team Bootstrap Audit

Use this prompt when the current repository is LocalSetup and the goal is to audit, adapt, or prepare an OpenCode-native agent-team bootstrap pack.

## Mission

Act as the primary OpenCode controller, planner, auditor, and verifier. Inspect whether OpenCode global and project config can support a generic subagent-first controller model for non-trivial work with bounded fanout. Create or update repo-local bootstrap-pack artifacts needed to reuse, version, audit, and later adapt this workflow.

Answer these questions:

1. Are the OpenCode config, agent files, permissions, model slots, and project instructions coherent with current OpenCode behavior?
2. Do the instructions use OpenCode-native surfaces rather than Codex-specific ones?
3. Do the instructions favor subagent-first routing for non-trivial work, small verifiable task units, low primary-agent context growth, direct mode only for trivial or constrained cases, and bounded delegation?
4. Are `permission.task` rules ordered correctly so wildcard deny comes before explicit allows?
5. Is `default_agent` a primary agent?
6. Are read-only subagents actually denied edit/write-like permissions?
7. Are model names generic portable slots rather than hard-bound private aliases?
8. Are volatile OpenCode facts backed by current local CLI output, official docs, schema, or source-backed evidence?
9. Are repo-local artifacts organized, indexed, generated, and validated without mutating home-directory OpenCode files?

## Operating Constraints

- Use OpenCode concepts only: `agent`, `mode`, `model`, `permission`, and `permission.task`.
- Use `permission`, not deprecated `tools`, in new examples.
- Prefer the documented `agents/` directory spelling; mention singular `agent/` only as runtime-compatible historical/source behavior when verified.
- Do not modify `~/.config/opencode`, credentials, provider keys, or global permissions without explicit user approval.
- Do not invent a separate daemon, scheduler, database, or agent framework.
- Do not add specialist roles when `explore`, `researcher`, `worker`, `tester`, and `reviewer` cover the task through scoped prompts.
- Do not claim completion without evidence.
- Do not rely on model memory for current OpenCode behavior.

## Recommended Subagent Scopes

Use narrow OpenCode subagents when they materially improve throughput, preserve context, reduce risk, or strengthen verification:

- `explore`: map repo pack, workflow, docs, template, index, and validation surfaces.
- `researcher`: verify current or version-matched OpenCode CLI/schema/docs/source facts.
- `worker`: update one bounded repo-local documentation/template scope.
- `tester`: run validation commands and summarize failures.
- `reviewer`: final read-only review of artifacts, permission examples, model slots, and validation evidence.

Subagents must not spawn subagents unless the controller explicitly authorizes nested delegation for that assignment.

## Required Artifact Checks

Inspect or create these repo-local artifacts only:

- `ls/docs/bootstrap-packs/INDEX.md`
- `ls/docs/bootstrap-packs/opencode-agent-team/README.md`
- `ls/docs/bootstrap-packs/opencode-agent-team/AUDIT_PROMPT.md`
- `ls/docs/bootstrap-packs/opencode-agent-team/MODEL_MAP.md`
- `ls/docs/bootstrap-packs/opencode-agent-team/metadata.yaml`
- `ls/templates/opencode/AGENTS.md`

## Fact Verification

Before writing current OpenCode claims, verify:

```bash
opencode --version
opencode agent --help
opencode agent list
curl -fsSL https://opencode.ai/config.json
```

For source-level behavior, first check the local starred-repos reference for `anomalyco/opencode`; if unavailable or stale, use remote GitHub only as a fallback and record the commit or URL.

## Acceptance

Before final response, verify every explicit requirement against real files, command output, parsed YAML/JSON, inspected diffs, generated artifacts, and reviewer evidence. Document anything not confirmed.
