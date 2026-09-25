---
status: ACTIVE
version: 4.44
owner_skill: ls-docs-organization
---

# Agentic design index (LocalSetup)

**Purpose:** Index of agentic-design documentation. Paths are relative to ls/docs/ (repo-local). Audience: humans and AI agents.

Released under the MIT License. See the repository root [LICENSE](../../LICENSE).

## Core docs

| Doc | Description |
|-----|-------------|
| [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md) | Named workflows; when to use; impact review |
| [bootstrap-packs/INDEX.md](bootstrap-packs/INDEX.md) | Reusable bootstrap-pack prompts, metadata, audit artifacts, and Codex-first adaptation path |
| [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md) | User and maintainer guide for first-class workflow packages |
| [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md) | First-class workflow package standard and manifest rules |
| [PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md](PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md) | PRD/spec format, outcome template, external confirmation; how PRDs interact with bidirectional Agent Q |
| [DECISION_TREE_WORKFLOW.md](DECISION_TREE_WORKFLOW.md) | Decision tree: one Q per turn, 4 options A-D, preferred + rationale |
| [AGENTIC_UMBRELLA_WORKFLOWS.md](AGENTIC_UMBRELLA_WORKFLOWS.md) | Umbrella workflows: single kickoff, PHC gates, single final webhook |
| [AGENTIC_AGENT_Q_PATTERN.md](AGENTIC_AGENT_Q_PATTERN.md) | Agent Q (queue) pattern: locate, implement, status, outcome; structured inbox/in/out/pending |
| [AGENTIC_AGENT_TO_AGENT_PROTOCOL.md](AGENTIC_AGENT_TO_AGENT_PROTOCOL.md) | Agent-to-agent PRD handoff: OpenPGP outer blob, registry, file_drop ingest (ACTIVE) |
| [OPENPGP_RUNTIME.md](OPENPGP_RUNTIME.md) | Current shared certificate, envelope, authority, recovery and consumer contract |
| [AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md](AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md) | Historical Agent Q build order and design decisions; current behavior is in the shared runtime, protocol and client guides |
| [TRUSTED_WORK_QUEUE.md](TRUSTED_WORK_QUEUE.md) | Immutable review queue: full repository snapshots, opaque PRDs, and directional shared-folder deposits/claims |
| [AGENTIC_AGENT_Q_SCENARIOS.md](AGENTIC_AGENT_Q_SCENARIOS.md) | file_drop/mail scenarios: same machine different repos, local/remote, sync, agent decision guide |
| [DOCUMENT_LIFECYCLE_MANAGEMENT.md](DOCUMENT_LIFECYCLE_MANAGEMENT.md) | Doc status (ACTIVE/PROPOSAL/DRAFT); check before assuming implemented |
| [OUTPUT_AND_DOC_GENERATION.md](OUTPUT_AND_DOC_GENERATION.md) | Platform default: rich output (code blocks, lists, typography, links, glyphs, humanized prose) for all generated content |
| [REPO_AND_DATA_SEPARATION.md](REPO_AND_DATA_SEPARATION.md) | Engine at ls/; local context vs framework; propose via PRD |
| [PYTHON_ARCHITECTURE_STANDARD.md](PYTHON_ARCHITECTURE_STANDARD.md) | Python architecture: new and substantially refactored Python tooling follows ls/docs/PYTHON_ARCHITECTURE_STANDARD.md; keep entrypoints thin, package responsibilities explicit, and existing debt baseline-managed. |
| [FRAMEWORK_LIBRARY_ARCHITECTURE.md](FRAMEWORK_LIBRARY_ARCHITECTURE.md) | Library, wrapper, CLI, target-node, and dashboard boundaries; queue-promotion and harness-extension gates |
| [AGENT_CONTEXT_AND_MCP_CONTRACT.md](AGENT_CONTEXT_AND_MCP_CONTRACT.md) | Freshness-first retrieval, normalized provenance, privacy, and optional read-only MCP contract |
| [GLOBAL_HANDOFF_LEDGER.md](GLOBAL_HANDOFF_LEDGER.md) | Private controller evidence ledger: accepted checkpoints, bindings, approvals, and restart-safe resume |
| [NODE_DASHBOARD_CONTROL_BOUNDARY.md](NODE_DASHBOARD_CONTROL_BOUNDARY.md) | Node dashboard trust boundary: bounded telemetry and capability requests through a node-local target helper |
| [ENVMAN_INTEGRATION_CONTRACT.md](ENVMAN_INTEGRATION_CONTRACT.md) | Opt-in, read-only external EnvMan discovery and inherited-environment boundary |
| [GIT_TRACEABILITY.md](GIT_TRACEABILITY.md) | Attach git hash when referencing PRDs, specs, outcomes |
| [SKILLS_AND_RULES.md](SKILLS_AND_RULES.md) | How master rule and skills interact; when to load which skill |
| [FRONTEND_WEB_APP_SKILL_ROUTING.md](FRONTEND_WEB_APP_SKILL_ROUTING.md) | Canonical LocalSetup routing for frontend web-app skills that overlap with the cached Build Web Apps plugin |
| [MULTI_PLATFORM_INSTALL.md](MULTI_PLATFORM_INSTALL.md) | Install for supported platforms |
| [PLATFORM_REGISTRY.md](PLATFORM_REGISTRY.md) | **Canonical list of supported AI agent platforms**; context and skills paths; skill registration file list |
| [ADAPTER_OWNERSHIP.md](ADAPTER_OWNERSHIP.md) | Shared adapter-directory ownership boundary; LocalSetup owns managed entries, not whole adapter paths |
| [AGENT_SKILLS_COMPLIANCE.md](AGENT_SKILLS_COMPLIANCE.md) | Agent Skills spec compliance; skill document versioning (metadata.version); auto-bump on commit |
| [SKILL_INTEROPERABILITY.md](SKILL_INTEROPERABILITY.md) | Import external skills (e.g. Anthropic); export our skills; full interchange with spec-compliant hosts |
| [SKILL_IMPORTING.md](SKILL_IMPORTING.md) | Import from URL or path; discover, validate, security-screen, summarize; user selects which skills to import; normalization (Phase 1 + Phase 2) mandatory |
| [SKILL_NORMALIZATION.md](SKILL_NORMALIZATION.md) | Phase 1: document normalization (platform choice when platform-specific). Phase 2: tooling rewrite to framework standard. Spec compliance and approval flow. |
| [SKILL_DISCOVERY.md](SKILL_DISCOVERY.md) | Public skill registries ([PUBLIC_SKILL_REGISTRY.urls](PUBLIC_SKILL_REGISTRY.urls), [PUBLIC_SKILL_INDEX.yaml](PUBLIC_SKILL_INDEX.yaml)); recommend similar when creating/importing |
| [TASK_SKILL_MATCHING.md](TASK_SKILL_MATCHING.md) | Task-to-installed-skill matching flow: single vs batch, auto-pick/parcel, complementary public-skill suggestions |
| [ops/tmux-ops-managed.md](ops/tmux-ops-managed.md) | Managed tmux ops: state layout, human flow, agent flow, command reference, JSON examples, timeout/cancel semantics |
| [ops/tmux-ops-remote.md](ops/tmux-ops-remote.md) | Tmux ops when tmux runs on another host: REMOTE_TMUX_HOST, REMOTE_TMUX_CWD; use `tmux_ops run` as usual |
| [TMUX_TERMINAL_MODE.md](TMUX_TERMINAL_MODE.md) | Tmux-default terminal mode: enable/disable/status, ide vs shell mode, flags, manual rollback, layer reference |

## Skills and workflow index (in repo)

- **Per platform:** See [PLATFORM_REGISTRY.md](PLATFORM_REGISTRY.md) for context loader and skills paths. Cursor: `.cursor/rules/ls-context-index.md` lists master rule plus key skills and workflow packages.

## Quick reference

- **Run decision tree:** Load workflow package `ls-workflow-spec-clarify-reverse`; see [DECISION_TREE_WORKFLOW.md](DECISION_TREE_WORKFLOW.md).
- **Process queue / PRDs:** Load `ls-workflow-queue-batch-implement`; see [PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md](PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md), [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md).
- **Agent Q ship/ingest (file_drop or mail):** Load `ls-agentq-transport` for the handoff flow and transport capability; see [scenarios](AGENTIC_AGENT_Q_SCENARIOS.md), [current client commands](../tools/agentq_transport_client/docs/USER_GUIDE.md), and the [shared OpenPGP contract](OPENPGP_RUNTIME.md). Both carriers use the same mandatory verified binary envelope.
- **Umbrella workflow:** Load `ls-workflow-umbrella-run`; see [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md).
- **Create a new skill:** Load `ls-skill-creator`; see [SKILL_INTEROPERABILITY.md](SKILL_INTEROPERABILITY.md).
- **Create or update a workflow package:** Follow [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md) and [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md); edit `ls/workflows/<package>/workflow.yaml`, then regenerate docs.
- **Import skills from URL or path:** Load `ls-skill-importer`; run `ls/tools/skill_importer_scan <path>`; see [SKILL_IMPORTING.md](SKILL_IMPORTING.md).
- **Discover similar public skills:** Load `ls-skill-discovery` when creating or importing; uses [PUBLIC_SKILL_REGISTRY.urls](PUBLIC_SKILL_REGISTRY.urls) and [PUBLIC_SKILL_INDEX.yaml](PUBLIC_SKILL_INDEX.yaml); see [SKILL_DISCOVERY.md](SKILL_DISCOVERY.md).
- **Audit and scrub the public skill index:** Run `uv run --locked python ls/tools/skill_index_scrub.py` to check for dead URLs, stub/placeholder descriptions, and schema gaps. Add `--fix` to fetch real descriptions from upstream and write them back. Add `--report FILE` for a GFM report.
- **Tmux shared session and sudo:** Load workflow package `ls-workflow-ops-tmux-session`; use `ls/tools/tmux_ops` (`pick`, `probe`, `run`, `status`, `cancel`). The workflow package defines the minimal agent script. [ops/tmux-ops-managed.md](ops/tmux-ops-managed.md) explains the implementation, state files, JSON contracts, timeout semantics, and human/operator view. For remote/VMs: see [ops/tmux-ops-remote.md](ops/tmux-ops-remote.md) (`REMOTE_TMUX_HOST`). See [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md).
- **Tmux-default terminal mode:** Run `ls/tools/tmux_terminal_mode enable [--mode ide|shell]` to wire up automatic tmux session launch (IDE terminal profile or shell RC auto-attach) and inject the mandatory agent ops rule. `disable` restores originals from backup. `status` reports all layers. See [TMUX_TERMINAL_MODE.md](TMUX_TERMINAL_MODE.md).
- **Run framework audit:** Load `ls-framework-audit`; run from repo root: `python ls/skills/ls-framework-audit/scripts/run_framework_audit.py --output /path/to/report.md` (or set `LOCALSETUP_AUDIT_OUTPUT`). No `--deep` in the current script; if docs elsewhere mention Deep Analysis, treat as backlog until the audit skill ships it. See [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md).
- **Run markdown reference audit:** Load `ls-markdown-reference-validator`; run `python ls/skills/ls-markdown-reference-validator/scripts/markdown_reference_audit.py --force --reason manual` (uses the repo-only YAML sidecar profile and writes ignored local state). Use the explicit host-aware profile only when local adapter or host instruction surfaces are required.
- **Route docs creation and updates:** Load `ls-docs-organization`; see `ls/skills/ls-docs-organization/SKILL.md`. Use it to classify docs, choose folder slugs, and keep indexes aligned.
