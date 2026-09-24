---
status: ACTIVE
version: 5.6
owner_skill: ls-docs-organization
---

# Features

This is the full public capability catalog for LocalSetup. The [root README](../../README.md) explains the pitch; this page lists what the framework actually provides.

## Generated Facts

<!-- facts-block:start -->
- Current version: `5.6.2`
- Supported platforms: `codex, claude-code, cursor, kilo, opencode, openclaw, github-copilot-cli, github-copilot-vscode, cline-cli, cline-vscode, amp-cli, goose-cli, pi-cli, hermes-agent, qwen-code-cli, kimi-cli, factory-droid, antigravity-app, gemini-cli, omp-cli`
- Shipped skills: `105`
- Workflow packages: `18`
- Source: `ls/docs/_generated/facts.json`
<!-- facts-block:end -->

## Engine And Install

The [4.4.0 release guide](releases/4.4.0.md) describes the current context and package modernization. Materialized package contents exclude Python bytecode caches while retaining source and data assets. Optional platform templates route to canonical catalogs instead of duplicating their inventory.

| Capability | What it gives you |
|---|---|
| Global framework source | The registered source checkout carries `ls/`; consuming repos keep `.localsetup/` state and selected adapters, not copied framework source. |
| Python-first LocalSetup installer | Bash bootstrap delegates planning, dependency handling, install, verify, and rollback to `ls/tools/localsetup.py`. |
| Explicit multi-platform adapters | One install can attach selected Cursor, Claude Code, Codex CLI, OpenClaw, Kilo, and OpenCode adapter paths to the same managed package library. |
| Managed home library | Skills and workflow packages install to `~/.local/share/localsetup/packages`; explicitly selected adapters point there by symlink or use portable copies. |
| Lock and rollback metadata | `.localsetup/lock.json` and managed-path reports make installs inspectable and reversible. |
| Repair handoff and runtime split | `doctor repair` separates managed lock state from local runtime state, preserves custom content, emits compact handoff prompts, and records repair queue metadata. |
| Client registry and platform projection | `ls/config/clients.yaml` is the canonical source for client capabilities and adapter mappings; generated `ls/config/platforms.yaml` is the compatibility/runtime projection consumed by existing platform tooling. |

## LSCli And Explicit Model Execution

| Capability | What it gives you |
|---|---|
| Protected coding CLI | [LSCli](LSCLI.md) exposes `lscli` and `localsetup agent` with explicit profiles, grants, headless/interactive input, context selection and brokered tools. Coding requires actual sandbox/resource preflight. |
| Runtime and command ownership | Offline artifact setup, create-only profiles, receipt-backed registration, refresh and explicit interrupted-registration recovery; no ambient provider discovery. |
| Durable session workflows | Explicit continuation/recovery, native or portable branches and tool-free compaction preserve prior evidence and require current authority; uncertain operations are not replayed. |
| Tool-free structured completion | [`localsetup llm complete`](LSCLI.md#tool-free-completion-command) uses a protected worker, explicit profile/request, one attempt and local output-schema validation; native schema enforcement requires declared capability. |

[SDK provenance](SDK_FORK.md), [runtime contracts](LSCLI_RUNTIME.md) and
[historical candidate checks](LSCLI_QUALIFICATION.md) define the qualified scope.
Implemented interfaces do not establish compatibility with every provider or host,
or acceptance of an unexamined published artifact.

## Skills And Interoperability

| Capability | What it gives you |
|---|---|
| Agent Skills compliance | Shipped skills use spec-compatible `SKILL.md` packages with `name`, `description`, and `metadata.version`. |
| 105 shipped skills plus 16 workflow packages | Practical capabilities and orchestration flows for debugging, tests, PR review, git recovery, service triage, patching, docs, MCP building, context indexing, TypeScript code quality, OmniRoute integration, opt-in heartbeat harnessing, repo finalization, and more. |
| Skill import | Import skills from a URL or local path with discovery, validation, heuristic security screening, and summaries. |
| Skill vetting | Treat third-party skills as untrusted before they can influence agent behavior. |
| Skill normalization | Clean imported or in-tree skills for spec compliance, platform-neutral wording, and framework tooling standards. |
| Skill discovery | Maintain a public registry/index and recommend similar skills when creating or importing. |

## Workflow Control

| Capability | What it gives you |
|---|---|
| Workflow registry | Named workflows, aliases, and impact expectations for repeatable agent behavior. |
| First-class workflow packages | Workflow sources live under `ls/workflows/ls-workflow-*`, include executable `SKILL.md` files, and carry LocalSetup `workflow.yaml` metadata for dependencies, gates, phases, validation, and generated catalogs. |
| Opt-in harness automation | The `harness` pack installs heartbeat capability only; explicit `localsetup harness codex-heartbeat ...` commands control target activation. The [typed LSCli profile](../skills/ls-codex-heartbeat/references/config.md#typed-lscli-profile) remains disabled/unselected by default, and generated cron commands retain `--no-agent`. |
| Reserved task accounting | [Controller commands and reserved actions](../skills/ls-codex-heartbeat/references/config.md#controller-accounting-commands) reserve full allocations, retain uncertain charges, bind result review and stop repeated no-progress attempts. Ordinary unreserved profiles and legacy queue reports do not acquire this policy implicitly. |
| Decision tree workflow | A reverse-prompt planning loop that asks one focused question at a time. |
| PRD batch workflow | Queue-driven spec execution with status updates and outcome records. |
| Agent Q transport | Bidirectional PRD/spec exchange over file_drop or mail with sealed payloads, registry checks, and ledgering. |
| Composite pipelines | Higher-level flows such as PR feedback, git repair, server triage, and repo polish built from existing skills. |
| Git traceability | Specs, outcomes, and decisions can reference commits so work is auditable later. |

## Safety And Operations

| Capability | What it gives you |
|---|---|
| Human-in-the-loop tmux ops | Privileged or risky operations stay visible in tmux with sudo readiness checks and resumable output. |
| Transaction-safe heartbeat runs | Codex heartbeat writes staged artifacts, validates hashes, promotes atomically, and recovers interrupted staged runs before fresh work starts. |
| Safety and backup guidance | Skills route destructive ops through conservative backup, temp-file, firewall, and approval practices. |
| Input hardening | Framework docs and tooling policy require hostile-input treatment for CLI args, files, network payloads, and imported content. |
| Security-aware skill import | Prompt-injection and suspicious-pattern heuristics run before imported skills become part of the library. |
| Markdown/reference validation | Public docs and skill references can be checked for broken local links and anchors. |

## Release And Maintenance

| Capability | What it gives you |
|---|---|
| Patch-default release versioning | Routine commit batches advance one patch by default; `Release-Type:` trailers explicitly request major, minor, patch, or no bump. |
| Generated facts sync | README and docs facts blocks stay aligned with `VERSION`, platform count, skill count, and workflow package count. |
| Automated documentation alignment | `ls/tools/docs_alignment.py` inventories repo docs, maps source-truth claims, validates assets and links, refreshes generated artifacts, and supports CI checks. |
| Skill metadata versions | Skill versions are tracked separately from framework release versions. |
| Framework audit | Doc, link, skill matrix, version, facts, and smoke checks before release. |
| Public package boundary | Packaging and scan commands keep generated/runtime artifacts out of source releases. |

## High-Value Shipped Skills

| Skill | Use it for |
|---|---|
| `ls-agentlens` | Codebase navigation and module discovery. |
| `ls-debug-pro` | Systematic debugging across languages and failure types. |
| `ls-test-runner` | Writing and running test suites across common frameworks. |
| `ls-typescript-code-quality` | TypeScript, TSX, tsconfig, typed linting, Node TypeScript scripts, and framework-heavy TypeScript changes. |
| `ls-pr-reviewer` | Risk-focused PR review and missing-test detection. |
| `ls-mcp-builder` | Building MCP servers for agent/tool interoperability. |
| `ls-skill-importer` | Bringing in skills from external sources safely. |
| `ls-skill-vetter` | Reviewing third-party skills before install. |
| `ls-workflow-ops-tmux-session` | Visible human-gated operations on local or remote machines. |
| `ls-workflow-tmux-terminal-mode` | Tmux-default terminal mode setup and read-only health checks. |
| `ls-linux-service-triage` | Diagnosing Linux service, reverse proxy, process, and DNS failures. |
| `ls-automatic-versioning` | Keeping framework version, generated docs, and release behavior aligned. |

Full catalogs: [SKILLS.md](SKILLS.md), [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md), and [WORKFLOW_QUICK_REF.md](WORKFLOW_QUICK_REF.md).

## Next Steps

- [Quickstart](QUICKSTART.md)
- [Platform registry](PLATFORM_REGISTRY.md)
- [Workflow packages](WORKFLOW_PACKAGES.md)
- [Workflow registry](WORKFLOW_REGISTRY.md)
- [Skill importing](SKILL_IMPORTING.md)
- [Versioning](VERSIONING.md)
