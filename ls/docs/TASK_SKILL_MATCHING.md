---
status: ACTIVE
version: 4.44
owner_skill: ls-task-skill-matcher
---

# Task-to-skill matching (LocalSetup)

**Purpose:** Define how agents map user tasks to installed skills with minimal interruption, plus complementary recommendations from the public skill index.

## Scope

- Applies to normal task execution when user does not name a specific skill.
- Complements (does not replace) [SKILLS_AND_RULES.md](SKILLS_AND_RULES.md).
- Detailed execution procedure lives in skill `ls-task-skill-matcher`.

## Core behavior

1. **Mode detection**
   - Treat as **batch** when user request includes multiple distinct subtasks, or says "batch", "multiple steps", or "run the whole thing".
   - Otherwise treat as **single task**.

2. **Named-skill override**
   - If user names a specific skill, load that skill directly.
   - Do not run task-skill-matcher in that case.

3. **When to invoke matcher**
   - Invoke `ls-task-skill-matcher` when skill choice is uncertain, or when user asks "what skill should I use?" / "pick the best".

4. **Single-task flow**
   - If one clear installed match exists, ask once: "Use this skill?"
   - If `ls-skill-discovery` is available to the current client, delegate complementary public suggestions and include up to 3 returned recommendations in the same response (one-line reason each). Otherwise report public discovery unavailable with no recommendations and continue installed-skill selection.

5. **Batch flow**
   - Prompt once at start with options: auto-pick for full run, parcel prompts, or parcel auto-pick.
   - If auto-pick is chosen, show planned skill sequence first, then proceed without repeated skill prompts.
   - If parcel phases are unclear, propose one parcel (whole task).

6. **No installed fit**
   - Say no installed skill fits.
   - Offer up to 3 returned complementary public skills to import when discovery is available; otherwise report public discovery unavailable with no recommendations.
   - Optionally suggest creating a new skill via `ls-skill-creator`.

## Public index handling

- `ls-skill-discovery` owns [PUBLIC_SKILL_INDEX.yaml](PUBLIC_SKILL_INDEX.yaml) availability, freshness policy, prompts, and maintenance. Delegate the public discovery flow only when that skill is available to the current client; do not define another policy here. If unavailable, return no public recommendations without reading the index as a fallback or automatically installing a package.
- Present the installed-skill match and "Use this skill?" first. Preserve discovery's returned last-refresh status (date and age when available), availability and freshness disclosures, and any pending user question.
- If discovery returns no recommendations, report that result and continue the installed-skill flow. Complementary suggestions use only the returned discovery results.

## Platform paths

Use the current platform context loader/index per [PLATFORM_REGISTRY.md](PLATFORM_REGISTRY.md):

- Cursor: `.cursor/rules/ls-context-index.md` (or skills section in `.cursor/rules/ls-context.mdc`)
- Claude Code: `.claude/CLAUDE.md`
- Codex: `AGENTS.md`
- OpenClaw: context path per platform registry

## References

- [SKILLS_AND_RULES.md](SKILLS_AND_RULES.md)
- [PLATFORM_REGISTRY.md](PLATFORM_REGISTRY.md)
- Skill: `ls-task-skill-matcher`
