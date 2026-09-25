---
status: ACTIVE
version: 4.44
owner_skill: ls-skill-creator
---

# Agent Skills compliance (LocalSetup)

**Purpose:** Confirm framework skills conform to the [Agent Skills](https://agentskills.io/specification) specification and document versioning and validation.

## Specification reference

- **Specification:** [agentskills.io/specification](https://agentskills.io/specification)
- **Repo:** [github.com/agentskills/agentskills](https://github.com/agentskills/agentskills)
- **Validation (optional):** Install [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref) and use its current CLI entrypoint to validate: `agentskills validate path/to/skill`

## Compliance summary

| Requirement | Framework behavior |
|-------------|--------------------|
| **Directory structure** | Each skill is a directory with `SKILL.md`; optional `scripts/`, `references/`, `assets/` per spec. |
| **name** (required) | Present in every skill; lowercase, hyphens, 1-64 chars; matches parent directory (e.g. `ls-context`). |
| **description** (required) | Present; what the skill does and when to use it; under 1024 chars. |
| **metadata.version** (optional) | Used for skill document versioning; bumped automatically when the skill file is updated (see below). |
| **allowed-tools** (optional, experimental) | Permitted when a skill needs to declare pre-approved tool hints; support varies by host, so local checks treat it as reviewable rather than invalid. |
| **Body** | Markdown instructions after frontmatter; progressive disclosure; keep under ~500 lines per spec. |
| **File references** | Relative paths from skill root; one level deep where possible. |

## Workflow packages and Agent Skills

Workflow packages under `ls/workflows/ls-workflow-*` also include valid Agent Skills `SKILL.md` files. That keeps them executable by agent hosts through the same managed library as skills.

The extra `workflow.yaml` file is LocalSetup metadata, not part of the Agent Skills specification. It records workflow ID, aliases, required skills, docs, tools, gates, phases, validation, outputs, smoke rows, and migration notes. See [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md) and [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md).

## Skill document versioning

- Each framework skill includes **metadata.version** (e.g. `"1.0"`) in SKILL.md frontmatter per the spec's optional `metadata` field.
- Skill version bumps are tracked separately from the framework release version. Patch is incremented (e.g. 1.0 -> 1.1) for non-breaking updates.

## Validation

- Optionally run `agentskills validate ./ls/skills/ls-<name>` (after installing [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref)) to check frontmatter and naming.
- Optionally run `agentskills validate ./ls/workflows/ls-workflow-<name>` for workflow packages. The Agent Skills validator checks `SKILL.md`; LocalSetup `validate-catalog` checks `workflow.yaml`.
- Framework skill names use the `ls-*` prefix and match the directory name; descriptions include trigger terms for discovery.
- Framework workflow package names use the `ls-workflow-*` prefix and match the `SKILL.md` frontmatter `name`.

## Interoperability

- **Framework skills are valid Agent Skills.** They use only spec-defined fields and layout; they can be copied into any Agent Skills-compatible host (e.g. [Anthropic's skills](https://github.com/anthropics/skills), Claude Code) and used as-is.
- **Framework workflow packages are executable Agent Skills packages.** Their `SKILL.md` files are portable; their `workflow.yaml` files are LocalSetup-specific and may be ignored by hosts that do not understand LocalSetup workflow metadata.
- **External spec-compliant skills can be imported into this framework only through `ls-skill-importer`.** Complete its full vetting, staged normalization, and frozen-byte sandbox-validation gates before canonical copy or registration; deployment remains a separate explicitly authorized action. Format compliance alone does not prove behavioral portability or permit bypassing those gates.
- Full import/export steps: [SKILL_INTEROPERABILITY.md](SKILL_INTEROPERABILITY.md).

## Reference

- [SKILL_INTEROPERABILITY.md](SKILL_INTEROPERABILITY.md)  - Import external skills; use our skills in other hosts; spec alignment for interchange.
- [SKILLS_AND_RULES.md](SKILLS_AND_RULES.md)  - How skills are loaded and platform paths.
- [PLATFORM_REGISTRY.md](PLATFORM_REGISTRY.md)  - Supported platforms and registration file list.
- Repo VERSION and conventional commits: [VERSIONING.md](VERSIONING.md). Skill versioning is per-skill (metadata.version), not the repo VERSION.
