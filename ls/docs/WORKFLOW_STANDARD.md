---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-compliance
---

# Workflow package standard

This document defines the package contract for workflow packages under `ls/workflows/ls-workflow-*`.

For the user-facing model, see [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md). This file is the maintainer contract used by validation, packaging, generated docs, and install behavior.

## Standards basis

- Agent Skills specification: a reusable package is a directory with `SKILL.md`; the `name` field matches the directory.
- Claude Code skills: reusable commands and prompt-like procedures are skills and can be invoked directly.
- Claude Code subagents: subagents are isolated task assistants, not package manifests.
- MCP prompts: prompts standardize reusable prompt templates, not a full workflow object.

LocalSetup therefore keeps capability skills under `ls/skills/` and defines workflow packages under `ls/workflows/`. A workflow package remains executable as an Agent Skills package through `SKILL.md`, while `workflow.yaml` adds LocalSetup-specific orchestration metadata.

## Skill vs workflow boundary

| Question | Use a skill | Use a workflow package |
|---|---|---|
| What is the source root? | `ls/skills/ls-*` | `ls/workflows/ls-workflow-*` |
| What does it represent? | A reusable capability or operating procedure. | A named orchestration flow with known phases and gates. |
| Does it include `SKILL.md`? | Yes. | Yes. |
| Does it include `workflow.yaml`? | No. | Yes. |
| Where does it install? | Managed package library. | Managed package library. |
| How is it selected by packs? | `packs` entries in `ls/config/pack.yaml`. | `workflow_packs` entries in `ls/config/pack.yaml`. |

Do not move ordinary capability skills just because their prose uses the word "workflow". Move or create a workflow package only when the package is a named reusable orchestration surface, a registry workflow, or a quick-ref pipeline.

## Required files

Each package must include:

- `SKILL.md`
- `workflow.yaml`

The package directory name, the `SKILL.md` frontmatter `name`, and the `workflow.yaml` workflow ID must align:

```text
ls/workflows/ls-workflow-example-flow/
  SKILL.md          # name: ls-workflow-example-flow
  workflow.yaml     # workflow_id: example-flow
```

## SKILL.md requirements

- Must use valid Agent Skills frontmatter.
- `name` must exactly match the package directory name.
- `description` should be concise and task-oriented.
- Body should summarize workflow purpose and point to canonical docs/tools.
- Avoid pasting large procedural docs into workflow packages.

## workflow.yaml requirements

`workflow.yaml` must include all keys below:

- `workflow_id`
- `display_name`
- `aliases`
- `invocation`
- `required_skills`
- `required_tools`
- `required_docs`
- `gates`
- `phases`
- `validation`
- `outputs`
- `smoke`
- `migration`

Path values in `required_docs` and `required_tools` must be repo-relative, must not use `..`, `~`, absolute paths, or Windows absolute paths, and must resolve to existing files when they are path-like. Aliases must not collide with skill names, workflow package names, workflow IDs, or other workflow aliases.

## Content guidance

- Keep content concise and ASCII.
- Use existing docs in `ls/docs/` as source references.
- Use existing local tools by path instead of re-documenting internals.
- Include at least one smoke row per workflow package.
- Keep `SKILL.md` executable and brief. Put catalog metadata, dependency lists, gates, and validation expectations in `workflow.yaml`.
- Prefer required skills over copied instructions. Workflow packages should compose capability skills rather than duplicate them.
- Use generated registry docs as output, not as source. Edit `workflow.yaml`, then regenerate docs.

## Naming

- Package directory format: `ls-workflow-<workflow-id>`.
- Pipeline workflow packages: `ls-workflow-pipeline-...`.
- `workflow_id` in `workflow.yaml` should match the workflow identifier used in docs.

## Validation and generation

Run these commands after editing workflow packages:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
```

Validation covers both skills and workflow packages. Generation refreshes the workflow registry, quick reference, generated workflow catalog, pack map, facts, migration maps, platform adapter docs, and implementation file map.
