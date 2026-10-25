---
status: ACTIVE
version: 4.84
owner_skill: ls-create-agent-skills
---

# Skill interoperability (LocalSetup)

**Purpose:** Define the boundary between Agent Skills format compatibility and behavioral portability. Independently owned local skills can be selected without importing them into framework source. Acquisition into the shipped framework still uses the import workflow. Exporting a framework skill requires target-host adaptation and verification when behavior depends on local paths, tools, sibling skills, runtime dependencies, coordination protocols, or deployment semantics.

## Independently owned local skills

Keep custom skills under a project's `.agents/skills/<name>/` or the selected
user home's `.agents/skills/<name>/`. Each directory contains a `SKILL.md` with
matching `name` and a useful `description`. Scripts, references, and assets stay
inside that directory. Discovery reads bounded regular files; it does not run
scripts, import modules, install dependencies, or grant tool authority. Managed
adapter links are not rediscovered as independent source.
Source directories must be readable, writable, and traversable by their owner
so their managed copies can be replaced and removed safely. Symlink resources
and special files require a separate packaging decision and are rejected.

Use `localsetup skill search` to inspect available sources. Select an origin
explicitly with `repo:<name>`, `personal:<name>`, or `framework:<name>` through the
existing `--skills`, `--repo-skills`, or `--global-skills` selectors. Unqualified
names work only when unambiguous. A physical adapter cannot expose different
origins under the same name; conflicts stop before changes.

From the installed target repository, `localsetup path package repo:<name>`
resolves its recorded immutable package. `personal:<name>` resolves an
unambiguous recorded personal selection. Append a relative resource path to
locate its scripts or references. Resolution verifies the snapshot; it does
not execute the resource. Framework package paths retain their existing behavior.

LocalSetup records the original source identity, content and permissions digest,
integration contract, and materializing framework version. Copies live in
separate content-addressed locations within the existing package library. Two
projects may therefore use different skills with the same visible name. Updating
one source does not overwrite another project's installed snapshot.

If the original already occupies the selected client's native skill directory,
it remains there as independently owned content. LocalSetup records that
selection without placing a managed-package marker inside the original. Detach
and rollback remove only recorded managed projections, never the original.
Portable projections have content baselines; edits must be preserved and resolved
before replacement or removal.

### Compatibility and current provenance

Ordinary Agent Skills need no extra metadata to be discovered and selected.
Their status is `format-only`: static shape checks do not prove that arbitrary
instructions or scripts work with a particular host. A skill that intentionally
uses the LocalSetup integration contract can declare:

```yaml
metadata:
  localsetup-contract: "1"
```

This uses string-valued metadata as defined by the
[Agent Skills specification](https://agentskills.io/specification). An unsupported
declared contract requires alignment before installation. A prepared plan is
bound to the inspected source; changed bytes or permissions require rebuilding
the plan. Recorded updates re-inspect the selected origin and preserve the
previous snapshot until the replacement passes validation.

Framework evolution can invalidate an applicable integration contract. That
requires updating the skill's actual integration and its declared contract,
then rebuilding the plan; changing a version label alone does not prove behavior.
LocalSetup does not rewrite independently owned source or download a supposedly
trusted latest revision automatically. Current provenance records what was
checked and installed; it does not confer trust or control external clients that
load skill files outside these managed operations.

## Interoperability principle

- **Format compatibility:** Agent Skills frontmatter and directory shape let a compliant host parse a skill. The `ls-*` prefix is a LocalSetup convention, not a format requirement.
- **Behavioral portability:** Parsing does not prove that repository links, sibling handoffs, scripts, dependencies, platform tools, environment providers, coordination protocols, or deployment behavior work in another host. Verify and adapt those boundaries.
- **External imports:** Never copy an external candidate directly into `ls/skills/`. `ls-import-external-skills` owns acquisition, collision decisions, safety screening, and the gated path through vetting, normalization, sandbox testing, canonical copy, and registration.
- **Workflow packages:** `SKILL.md` follows the Agent Skills format. `workflow.yaml` is LocalSetup-specific orchestration metadata and requires explicit target-host support or adaptation.

## Using an external skill in this framework (import)

1. Load `ls-import-external-skills` with the external URL or local directory and the intended LocalSetup purpose.
2. For pasted content or a single-document URL, preserve the exact bytes in a temporary path and pass the path-based `skill_importer_scan` required by [SKILL_IMPORTING.md](SKILL_IMPORTING.md#adding-a-skill-from-paste-or-url).
3. Keep the importer as operational owner. Require passing `ls-skill-security-vetting`, `ls-normalize-skill-format`, and `ls-skill-sandbox-tester` evidence before canonical copy or registration. If any result is missing, rejected, unresolved, or untested, stop.
4. Confirm success only after the importer reports all gate evidence, canonical copy, and registration. Deployment is a separate action.

## Using a framework skill elsewhere (export)

1. Start from `ls/skills/<name>/` or a managed emitted package. Emitted packages may materialize public framework references beneath `references/localsetup/` and record transformations in `.localsetup-reference-bundle.json`.
2. Run the export audit owned by `ls-create-agent-skills`: inspect `SKILL.md`, references, scripts, and assets for local or absolute paths, sibling-skill handoffs, commands, adapter assumptions, dependencies, executables, tools, environment or secret providers, coordination protocols, and deployment behavior.
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
| **External -> Framework** | Route through `ls-import-external-skills`; require vetting, normalization, sandbox testing, canonical copy, and registration evidence. |
| **Framework -> External** | Audit and adapt host-specific behavior, then run the real target-host smoke scenario. |
| **Workflow package -> External** | Treat `SKILL.md` as format-compatible and adapt LocalSetup-specific metadata and orchestration before testing. |

Agent Skills compliance establishes format compatibility only. Behavioral portability is a separately verified property of a specific target host and scenario.
