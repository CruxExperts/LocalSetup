---
status: ACTIVE
version: 5.6
owner_skill: ls-docs-organization
---

# Workflow packages

LocalSetup separates reusable agent capability from reusable agent orchestration.

- A **skill** is a portable capability package under `ls/skills/ls-*`.
- A **workflow package** is a named orchestration package under `ls/workflows/ls-workflow-*`.
- Both install into the managed package library because both include a valid Agent Skills `SKILL.md`.
- Workflow packages also include `workflow.yaml`, which is LocalSetup-specific metadata for aliases, required skills, gates, phases, validation, outputs, and migration notes.

The practical rule is simple: use a skill when the agent needs a capability, and use a workflow package when the agent needs a repeatable task flow with known gates and evidence.

## Source layout

| Source root | Package pattern | Required files | Purpose |
|---|---|---|---|
| `ls/skills/` | `ls-*` | `SKILL.md` | Portable capability packages. |
| `ls/workflows/` | `ls-workflow-*` | `SKILL.md`, `workflow.yaml` | Executable workflow packages with LocalSetup orchestration metadata. |

The `SKILL.md` file keeps the workflow executable by agent hosts that understand Agent Skills. The `workflow.yaml` file lets LocalSetup validate and generate framework-specific registry data.

## Install behavior

The installer copies selected skills and selected workflow packages into:

```text
~/.local/share/localsetup/packages
```

When selected with `--tools` or `--platforms`, platform adapters such as `.agents/skills`, `.kilo/skills`, and `.cursor/skills` attach to that managed library by symlink or portable copy. A workflow package therefore appears beside normal skills at runtime, but its source and metadata stay separate in `ls/workflows/`.

Workflow pack selection also pulls in required capability skills. For example, a pre-publish workflow can require publishing, versioning, and audit skills without duplicating those instructions inside the workflow package.

Installed workflow packages are materialized outputs, not the canonical source tree. Runtime-facing Markdown references to public framework docs are rewritten into `references/localsetup/docs/...`, and copied public docs are bundled under that package-local reference root. Each emitted package includes `references/localsetup/.localsetup-reference-bundle.json` with the copied references, rewrites, source-only metadata, exclusions, and validation status.

`workflow.yaml.required_docs` stays source-repo validation metadata. LocalSetup validates those paths against the source checkout and records them in the transform manifest as source-only metadata, but does not rewrite the YAML values for runtime hosts. Private, blocked, or traversal-style `required_docs` entries are rejected before an emitted package is written.

## Generated docs

These files are generated from workflow manifests:

- [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md)
- [WORKFLOW_QUICK_REF.md](WORKFLOW_QUICK_REF.md)
- [_generated/workflow-catalog.json](_generated/workflow-catalog.json)

Do not hand-maintain workflow rows in those files. Update the relevant `ls/workflows/<package>/workflow.yaml` and regenerate docs instead:

```bash
uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
```

## Validation

Use the catalog validator after editing skills or workflow packages:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
```

The validator checks that workflow package names match `ls-workflow-<workflow_id>`, `SKILL.md` names match directory names, aliases do not collide, dependencies exist, required docs and tools resolve inside the repo, and each package has a smoke row.

`localsetup verify` also validates emitted installed packages. It fails when a managed package is missing its transform manifest, has invalid emitted package metadata, leaks private maintenance paths, has symlink escapes, or leaves runtime-facing Markdown references pointed at source-checkout framework docs.

When the optional Agent Skills CLI is available, validate both source roots:

```bash
agentskills validate ls/skills/ls-context
agentskills validate ls/workflows/ls-workflow-spec-clarify-reverse
```

## Adding or changing a workflow package

1. Create or edit `ls/workflows/ls-workflow-<id>/`.
2. Keep `SKILL.md` concise and Agent Skills compliant.
3. Put LocalSetup orchestration metadata in `workflow.yaml`.
4. Reference existing docs and tools instead of pasting large procedures.
5. Add or update tests for new validator, installer, generation, or package behavior.
6. Regenerate docs and run validation.

For the formal contract, see [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md).
