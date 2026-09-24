---
status: ACTIVE
version: 5.6
owner_skill: ls-skill-creator
---

# Skill interoperability (LocalSetup)

**Purpose:** Define the boundary between Agent Skills format compatibility and behavioral portability. LocalSetup can import external skills only through its gated import workflow; exporting a framework skill requires target-host adaptation and verification when behavior depends on local paths, tools, sibling skills, runtime dependencies, coordination protocols, or deployment semantics.

## Interoperability principle

- **Format compatibility:** Agent Skills frontmatter and directory shape let a compliant host parse a skill. The `ls-*` prefix is a LocalSetup convention, not a format requirement.
- **Behavioral portability:** Parsing does not prove that repository links, sibling handoffs, scripts, dependencies, platform tools, environment providers, coordination protocols, or deployment behavior work in another host. Verify and adapt those boundaries.
- **External imports:** Never copy an external candidate directly into `ls/skills/`. `ls-skill-importer` owns acquisition, collision decisions, safety screening, and the gated path through vetting, normalization, sandbox testing, canonical copy, and registration.
- **Workflow packages:** `SKILL.md` follows the Agent Skills format. `workflow.yaml` is LocalSetup-specific orchestration metadata and requires explicit target-host support or adaptation.

## Using an external skill in this framework (import)

1. Load `ls-skill-importer` with the external URL or local directory and the intended LocalSetup purpose.
2. For pasted content or a single-document URL, preserve the exact bytes in a temporary path and pass the path-based `skill_importer_scan` required by [SKILL_IMPORTING.md](SKILL_IMPORTING.md#adding-a-skill-from-paste-or-url).
3. Keep the importer as operational owner. Require passing `ls-skill-vetter`, `ls-skill-normalizer`, and `ls-skill-sandbox-tester` evidence before canonical copy or registration. If any result is missing, rejected, unresolved, or untested, stop.
4. Confirm success only after the importer reports all gate evidence, canonical copy, and registration. Deployment is a separate action.

## Using a framework skill elsewhere (export)

1. Start from `ls/skills/<name>/` or a managed emitted package. Emitted packages may materialize public framework references beneath `references/localsetup/` and record transformations in `.localsetup-reference-bundle.json`.
2. Run the export audit owned by `ls-skill-creator`: inspect `SKILL.md`, references, scripts, and assets for local or absolute paths, sibling-skill handoffs, commands, adapter assumptions, dependencies, executables, tools, environment or secret providers, coordination protocols, and deployment behavior.
3. Adapt every unsupported boundary to the target host. If the host expects another name, rename both the directory and `name` field.
4. Execute the skill's real smoke scenario in the target host. Claim behavioral portability only for the verified host and scenario.

## Using a workflow package elsewhere

Workflow packages need the same export audit. Copying their Agent Skills-shaped content does not transfer LocalSetup orchestration semantics.

- Preserve the directory and `name` field match.
- Treat `workflow.yaml` as LocalSetup-specific metadata unless the target host explicitly supports its contract.
- Adapt required documents, sibling workflows, tools, and coordination behavior before use.
- Run the workflow's real target-host smoke scenario before claiming support.

## Specification and design references

- **Format:** [Agent Skills specification](https://agentskills.io/specification) and [agentskills/agentskills](https://github.com/agentskills/agentskills).
- **Design and authoring:** [Anthropic's skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) for concise instructions, progressive disclosure, and scripts/references/assets structure.
- **Validation:** [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref) for frontmatter and naming checks. Format validation does not replace import safety gates or target-host behavior testing.

## Summary

| Direction | Required action |
|-----------|-----------------|
| **External -> Framework** | Route through `ls-skill-importer`; require vetting, normalization, sandbox testing, canonical copy, and registration evidence. |
| **Framework -> External** | Audit and adapt host-specific behavior, then run the real target-host smoke scenario. |
| **Workflow package -> External** | Treat `SKILL.md` as format-compatible and adapt LocalSetup-specific metadata and orchestration before testing. |

Agent Skills compliance establishes format compatibility only. Behavioral portability is a separately verified property of a specific target host and scenario.
