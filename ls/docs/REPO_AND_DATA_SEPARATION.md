---
status: ACTIVE
version: 4.44
owner_skill: ls-framework-compliance
---

# Repo and data separation (LocalSetup)

**Purpose:** LocalSetup framework source lives in the registered source checkout, normally `~/.local/share/localsetup/source` or a contributor checkout. Consuming repos keep target-owned state under `.localsetup/` and selected adapter paths only.

## Separation

- **Source checkout**  - Contents of `ls/` in the LocalSetup source tree: framework code, docs, skills, workflow packages, templates, and tests.
- **Target state**  - `.localsetup/lock.json`, `.localsetup/install-journal/`, `.localsetup/backups/`, `.localsetup/context-index/`, and `.localsetup/state/` in consuming repos.
- **Adapter surfaces**  - Agent adapter-shaped directories such as `.agents/skills`, `.claude/skills`, `.cursor/skills`, `.kilo/skills`, and `.opencode/skills`. Historical `.codex/skills` content remains a shared surface and is eligible only for the proof-gated Codex managed-entry transition. These are shared surfaces, not LocalSetup-exclusive directories.
- **Local context**  - Repo-root files such as `.cursor/rules/local-*.mdc` or platform-specific overrides. Safe to edit for project-specific rules.
- **Mutable state**  - Project/user state such as reminders, backlog, temporary notes, harness state, and runtime logs belongs in approved repo-level or platform-owned paths outside `ls/`.
- **Proposals**  - For framework behavior changes, use the Agent Q / PRD flow; see [PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md](PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md).

## Source and runtime boundaries

- `ls/skills/` and `ls/workflows/` are framework source in the LocalSetup source checkout.
- A consuming repo should not contain `ls/` by default. Treat a target `ls/` as stale legacy framework source unless the target is the LocalSetup source checkout itself.
- Never store reminders, backlog, temporary notes, or other mutable project/user state under `ls/`. If that happens accidentally, move it to an approved mutable-state path and revert the framework-source change.
- `~/.local/share/localsetup/packages` is managed runtime output and can be recreated by install.
- Explicitly selected platform adapter paths such as `.agents/skills`, `.kilo/skills`, and `.cursor/skills` are shared adapter surfaces where LocalSetup writes managed package entries. They are attachments to the managed runtime library, not new source roots and not exclusive LocalSetup-owned directories. A global-only install creates no repo adapter paths.
- LocalSetup owns only adapter entries it creates and records, such as `.localsetup-adapter.json`, selected package symlinks, or portable managed package copies. Custom skills, files, symlinks, and non-LocalSetup entries in adapter directories remain repo-owned and must be preserved in place by install, repair, detach, rollback, and cleanup workflows.
- Harness activation files such as `HEARTBEAT.md`, `config/codex_heartbeat.yaml`, `cron/manifest.yaml`, and `.localsetup/state/codex-heartbeat/` are target-repo state created only by explicit harness commands, not by normal install.
- Generated workflow docs come from `ls/workflows/*/workflow.yaml`; do not treat generated registry rows as source edits.

## Reference

- [GIT_TRACEABILITY.md](GIT_TRACEABILITY.md)  - Attach git hash when referencing PRDs, specs, outcomes.
- [AGENTIC_DESIGN_INDEX.md](AGENTIC_DESIGN_INDEX.md)  - Index of framework docs.
- [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md)  - Source/runtime model for first-class workflow packages.
- [ADAPTER_OWNERSHIP.md](ADAPTER_OWNERSHIP.md)  - Shared adapter-directory ownership boundary.
- [HARNESS_AUTOMATION.md](HARNESS_AUTOMATION.md)  - Opt-in harness activation and runtime artifact boundaries.
