# LocalSetup - Context and skills index

| Asset | Description | When applied |
|-------|-------------|--------------|
| ls-context.mdc | Master rule: overview, invariants, skills index, docs index | Always |
| ls/docs/PYTHON_ARCHITECTURE_STANDARD.md | Python architecture: new and substantially refactored Python tooling follows ls/docs/PYTHON_ARCHITECTURE_STANDARD.md; keep entrypoints thin, package responsibilities explicit, and existing debt baseline-managed. | Python framework tooling changes |

## Capability and workflow discovery

Load `ls-localsetup-project-context` for framework layout and resolver guidance. Use the current
client's available-skill descriptions to select installed capabilities; a catalog
entry alone does not mean that package is installed or available to this client.
For the full framework catalog, resolve `localsetup path doc SKILLS.md` and
`localsetup path doc WORKFLOW_REGISTRY.md`, then read only the entries relevant
to the task. Package frontmatter and generated catalogs own descriptions, tags,
and pack membership; do not duplicate their lists in platform context.

Load an explicitly named available skill directly. Use `ls-task-skill-matcher`
when selection is unclear, using its single-task or batch selection procedure.

OmniRoute has one ambiguous-task/preflight router, `ls-omniroute-task-triage`: route
classified read-only discovery to ls-omniroute-model-discovery, mutation to ls-omniroute-admin-automation, and source/coverage maintenance to ls-omniroute-update. These owners remain distinct after catalog consolidation.

Framework documentation: resolve `localsetup path doc AGENTIC_DESIGN_INDEX.md`.
