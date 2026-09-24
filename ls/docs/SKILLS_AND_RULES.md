---
status: ACTIVE
version: 5.6
owner_skill: ls-task-skill-matcher
---

# Skills And Rules (LocalSetup)

**Purpose:** How the master rule (or platform context loader) and skills interact; when to load which skill; cross-platform paths.

## Model

- **Context is client-owned:** LocalSetup ships optional platform context templates. The native installer installs selected packages and explicitly selected adapters; it does not adopt those templates or replace the user's context files. Check the current client registry and active workspace before assuming a template is loaded.
- **Capability skills and workflow packages:** Capability skills live in `ls/skills/`. Workflow packages live in `ls/workflows/` and also contain `SKILL.md`, so installs both package types into the managed package library.
- **When to load a skill or workflow:** Load an available package when the task matches its description (e.g. user says "decision tree" -> ls-workflow-spec-clarify-reverse). Use current client discovery for installed availability, `ls-context` for framework orientation, and generated catalogs for the complete source inventory. Read relevant entries on demand; catalog membership does not prove installation.

## Skills vs workflow packages

Use [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md) as the canonical definition of:

- capability skill vs workflow package semantics
- source metadata (`SKILL.md` and `workflow.yaml`)
- managed-library install shape and adapter behavior

## Task-to-skill matching flow

- **Mode detection:** Treat as **batch** when user request includes multiple distinct subtasks, or says "batch", "multiple steps", or "run the whole thing". Otherwise treat as **single task**.
- **Named skill override:** If user names a specific skill, load that skill directly. Do not run task-skill-matcher.
- **When to invoke matcher:** When uncertain which skill fits, or when user asks "what skill should I use?" / "pick the best", load `ls-task-skill-matcher`.
- **Single task behavior:** If one clear installed match exists, ask once "Use this skill?" before loading. If `ls-skill-discovery` is available to the current client, delegate complementary public suggestions to it; it owns [PUBLIC_SKILL_INDEX.yaml](PUBLIC_SKILL_INDEX.yaml) policy. Include up to 3 returned recommendations in the same response (one-line reason each), preserving discovery's index-status disclosures and any pending user question. Otherwise report public discovery unavailable with no recommendations and continue installed-skill selection; do not read the index as a fallback or automatically install a package.
- **Batch behavior:** Prompt once at start with options: auto-pick for whole job, parcel-by-parcel prompts, or parcel auto-pick. If auto-pick is chosen, state the planned skill sequence first, then proceed without repeated skill prompts.
- **No installed fit:** Say that no installed skill fits, offer up to 3 returned complementary public skills to import when discovery is available (otherwise report public discovery unavailable with no recommendations), and optionally suggest creating a skill via `ls-skill-creator`.
- **Reference:** Full procedure and output format live in skill `ls-task-skill-matcher` and [TASK_SKILL_MATCHING.md](TASK_SKILL_MATCHING.md).

## Platform paths

**Canonical inventory:** [PLATFORM_REGISTRY.md](PLATFORM_REGISTRY.md) links the complete generated installer inventory and owning client metadata. The examples below are not the complete selector list. Fresh adapter creation requires selection; updates of a recorded target retain its validated ownership even when selectors are omitted. A fresh global-only install does not create repo adapter paths. Context paths identify template or user-owned guidance and do not describe installer output. Verify actual loading through the current client's registry entry and configuration.

| Platform | Context loader | Skills |
|----------|----------------|--------|
| Cursor | .cursor/rules/ls-context.mdc | .agents/skills -> managed library |
| Claude Code | .claude/CLAUDE.md | .claude/skills -> managed library |
| Codex | AGENTS.md (repo root) | .agents/skills -> managed library |
| OpenClaw | platform template | .agents/skills -> managed library |
| OpenCode | AGENTS.md (repo root) | .agents/skills -> managed library |
| Kilo CLI | AGENTS.md | .agents/skills -> managed library |

## Format

- Skills follow the [Agent Skills](https://agentskills.io/specification) specification: SKILL.md with required `name` and `description` frontmatter; optional `metadata.version` for skill document versioning; body = instructions. This shared format does not guarantee identical loading, tools, permissions, or behavior across hosts.
- Workflow packages also include a spec-compatible `SKILL.md`; their LocalSetup-only `workflow.yaml` is documented in [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md).
- **Skill document versioning:** Each skill includes `metadata.version` (e.g. `"1.0"`). Skill versions are tracked separately from the framework release version; see [AGENT_SKILLS_COMPLIANCE.md](AGENT_SKILLS_COMPLIANCE.md).
- When adding a platform or registering a new skill, use [PLATFORM_REGISTRY.md](PLATFORM_REGISTRY.md) as the source of truth.
- **Interoperability:** Format compatibility is only the starting point. External skills (for example, [Anthropic's](https://github.com/anthropics/skills)) require vetting, normalization, and sandbox testing before canonical placement and registration. Exported skills require host-specific adaptation and a real target-host smoke scenario before claiming behavioral compatibility. Follow [SKILL_INTEROPERABILITY.md](SKILL_INTEROPERABILITY.md) for the import, export, and qualification procedures.

## Maintainer references

- [Workflow packages](WORKFLOW_PACKAGES.md)
- [Workflow package standard](WORKFLOW_STANDARD.md)
- [Workflow registry](WORKFLOW_REGISTRY.md)
- [Workflow quick reference](WORKFLOW_QUICK_REF.md)
