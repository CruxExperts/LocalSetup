---
status: ACTIVE
version: 4.85
owner_skill: ls-localsetup-framework-changes
---

# Workflow package standard

This document defines the package contract for workflow packages under `ls/workflows/ls-wf-*`.

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
| What is the source root? | `ls/skills/ls-*` | `ls/workflows/ls-wf-*` |
| What does it represent? | A reusable capability or operating procedure. | A named orchestration flow with known phases and gates. |
| Does it include `SKILL.md`? | Yes. | Yes. |
| Does it include `workflow.yaml`? | No. | Yes. |
| Where does it install? | Managed package library. | Managed package library. |
| How is it selected by packs? | `packs` entries in `ls/config/pack.yaml`. | `workflow_packs` entries in `ls/config/pack.yaml`. |

Do not move ordinary capability skills just because their prose uses the word "workflow". Move or create a workflow package only when the package is a named reusable orchestration surface, a registry workflow, or a quick-ref pipeline.

## Founding design principle

LocalSetup workflows should reduce friction and ambiguity. Extend the existing owner before adding another workflow, tool, document, check, gate, or process step. Add a new surface only when a concrete user or safety need cannot be met by improving the current one, and keep it scoped to that gap. Keep paths direct, explain decisions and ownership where ambiguity is likely, and validate the changed behavior with the smallest useful checks. Do not require a role, test, approval, record, or phase merely to satisfy a fixed process; retain it when it provides evidence or control that the task actually needs. For long-running interactive work, expose concise, visually separated progress at milestones and periodically while work continues, with the current item, useful remaining count, and next action; skip command-by-command narration.

Friction-free, robust operation is a framework-wide LocalSetup policy tenet. Improve the existing owner before creating another path. Automate routine, reversible work within existing authority; ask only for a materially different decision or a genuine authorization boundary. Reuse unchanged approvals and applicable validation evidence. Do not add duplicate checks, reviewers, ledgers, polling loops, or mandatory phases. Every additional gate must protect a named safety, correctness, or acceptance need. Friction-free never means bypassing security, signatures, privacy, or the user's control.

## Required files

Each package must include:

- `SKILL.md`
- `workflow.yaml`

The package directory name, the `SKILL.md` frontmatter `name`, and the `workflow.yaml` workflow ID must align:

```text
ls/workflows/ls-wf-sudo-tmux-session/
  SKILL.md          # name: ls-wf-sudo-tmux-session
  workflow.yaml     # workflow_id: sudo-tmux-session
```

## SKILL.md requirements

- Must use valid Agent Skills frontmatter.
- `name` must exactly match the package directory name.
- `description` must name the natural requests, symptoms, commands, file types,
  or error conditions that trigger the workflow, then explain its behavior and
  scope limits. Related phrases and exclusions belong here rather than in a
  keyword-stuffed name.
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

- Canonical package directory format: `ls-wf-<trigger-keywords>`.
- Select the three to five most useful words from circumstances in which the
  agent should load the package. Prefer concrete signals such as sudo, merge
  conflict, failing tests, or dependency advisory to abstract outcome labels.
  Omit redundant `workflow`, `skill`, and `pipeline` words unless the word is
  itself relevant to the task. Keep names accurate to the actual capability.
- `workflow_id` in `workflow.yaml` should match the workflow identifier used in docs.
- Preserve published package names and changed IDs in manifest aliases during
  renames. Existing legacy `ls-workflow-*` packages remain readable; aliases and
  the recorded-update owner migrate existing selections without creating a
  duplicate implementation or moving custom adapter content.
- The same activation-driven naming and description policy applies to regular
  skills through the [skill authoring owner](../skills/ls-create-agent-skills/SKILL.md).

## Naming ownership and provenance

The package directory, `SKILL.md` frontmatter, workflow manifest, and
`ls/config/pack.yaml` own names, discovery context, selection, and aliases.
Skill rename mappings belong in the authored `alias_policy` of that pack
configuration. Update callers and owning docs in the same change. Regenerate
catalogs, alias maps, and the existing artifact provenance registry through the
owning documentation tools; never hand-edit generated provenance or use it as
the authority for a new naming decision. The source commit and input hashes
bind the generated discovery context to the reviewed policy and package bytes.

## Validation and generation

Run these commands after editing workflow packages:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
```

Validation covers both skills and workflow packages. Generation refreshes the workflow registry, quick reference, generated workflow catalog, pack map, facts, migration maps, platform adapter docs, and implementation file map.
