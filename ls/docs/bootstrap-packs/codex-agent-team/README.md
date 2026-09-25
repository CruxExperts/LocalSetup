---
status: ACTIVE
version: 4.44
owner_skill: ls-framework-compliance
---

# Codex Agent Team Bootstrap Pack

This pack captures the generic Codex controller/subagent workflow used to audit and maintain a global Codex agent-team bootstrap. The OpenCode-native sibling pack lives at [../opencode-agent-team/README.md](../opencode-agent-team/README.md).

It is intentionally native to Codex:

- `$CODEX_HOME/config.toml` or `~/.codex/config.toml`
- `$CODEX_HOME/AGENTS.md` or `~/.codex/AGENTS.md`
- `$CODEX_HOME/agents/*.toml` or `~/.codex/agents/*.toml`
- repo `AGENTS.md`
- native subagents
- plan mode and goal mode
- markdown runbooks and small YAML metadata

The default is subagent-first and throughput-oriented, not specialist-heavy: the controller keeps task units small, looks for useful delegation before doing substantial work directly, and uses direct mode only for trivial one-step tasks, simple questions, cases with no useful independent subtask, or active tool/mode constraints. Normal fanout is one or two agents for non-trivial work; three is reserved for clearly independent discovery, research, or validation. Existing native roles stay generic:

- `explorer`: read-only mapping of relevant files, systems, docs, workflows, data, dependencies, tests, and risks
- `researcher`: current or source-backed fact verification
- `worker`: one bounded execution task with exact write scope
- `tester`: validation, benchmarks, measurements, and failure summaries
- `reviewer`: final risk, correctness, regression, scope, and evidence review

The shipped `guardian_subagent` is a Codex approvals reviewer role for permission and rules checks. Do not use it as a normal task-delegation role.

## LocalSetup Pack

Install or select the pack as `bootstrap`. The source membership lives in [`ls/config/pack.yaml`](../../../config/pack.yaml), and the generated index lives in [`_generated/skill-packs.md`](../../_generated/skill-packs.md).

The pack composes existing LocalSetup skills and workflow packages instead of copying their instructions:

- controller context and communication
- framework compliance and docs routing
- safety and guarded operations
- test and review support
- audit and git-repair workflows

## Prompt

Use [AUDIT_PROMPT.md](AUDIT_PROMPT.md) when asking Codex to audit a prior global bootstrap or refresh the durable generic controller defaults.

## Platform Adaptation

Use the OpenCode sibling pack when adapting this workflow to OpenCode. It uses OpenCode-native `agent`, `mode`, `permission`, `permission.task`, and provider model-slot examples rather than Codex TOML or Codex subagent files.

## Approval Boundary

This pack may inspect global Codex state, but changes to home-directory files, sibling repos, global permissions, or external runtime mirrors require explicit user approval.
