---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-compliance
---

# Bootstrap Packs

Bootstrap packs are small, versioned bundles of LocalSetup skills, workflow packages, prompts, and metadata for standing up or auditing agent operating modes.

The initial bootstrap pack targeted OpenAI Codex CLI first. OpenCode now has a sibling pack that adapts the same controller/subagent workflow to OpenCode-native config, permissions, and model-slot surfaces.

## Pack Index

| ID | Primary platform | LocalSetup pack | Status | Metadata | Prompt |
|---|---|---|---|---|---|
| `codex-agent-team` | `codex` | `bootstrap` | audit-ready | [metadata.yaml](codex-agent-team/metadata.yaml) | [AUDIT_PROMPT.md](codex-agent-team/AUDIT_PROMPT.md) |
| `opencode-agent-team` | `opencode` | `bootstrap` | audit-ready | [metadata.yaml](opencode-agent-team/metadata.yaml) | [AUDIT_PROMPT.md](opencode-agent-team/AUDIT_PROMPT.md) |

## Source Of Truth

- Skill/workflow pack membership: [`ls/config/pack.yaml`](../../config/pack.yaml)
- Generated skill/workflow pack map: [`_generated/skill-packs.md`](../_generated/skill-packs.md)
- Bootstrap document bundle index: this file
- Codex platform context: [`ls/templates/codex/AGENTS.md`](../../templates/codex/AGENTS.md)
- OpenCode platform context: [`ls/templates/opencode/AGENTS.md`](../../templates/opencode/AGENTS.md)

## Safety Rule

Bootstrap packs may describe global config, external runtime mirrors, and legacy replacement paths, but repo-local audit passes must not modify those locations without explicit user approval.
