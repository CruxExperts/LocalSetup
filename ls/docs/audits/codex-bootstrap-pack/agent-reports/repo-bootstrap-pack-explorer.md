---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-audit
date: 2026-05-10
---

# Repo Bootstrap-Pack Explorer Report

## Summary

No existing bootstrap-pack or agent-team pack structure was found. The current Localsetup pack model can support one through metadata and generated indexes: pack membership lives in `ls/config/pack.yaml`, selection flows through existing CLI/install code, and `ls/docs/_generated/skill-packs.md` is generated from the catalogs.

## Existing Patterns

- `ls/config/pack.yaml` is the pack source of truth.
- `load_pack_config()`, pack selection, install planning, and generated docs already consume arbitrary pack names.
- Workflow packages stay separate from capability skills under `ls/workflows/`.
- `ls/templates/codex/AGENTS.md` is the Codex platform context surface and should receive only a short pointer, not a duplicate catalog.
- `ls/config/platforms.yaml` maps platform adapter paths and should not carry pack membership.

## Findings

| ID | Severity | Evidence | Recommendation |
|---|---|---|---|
| RBP-001 | medium | `pack.yaml` had six packs and no bootstrap pack. | Add an additive `bootstrap` pack in `pack.yaml`. |
| RBP-002 | low | Generated pack map comes from source catalogs. | Regenerate docs after pack changes instead of hand-editing generated files. |
| RBP-003 | low | Codex template is a context surface, not the pack catalog. | Add a short pointer only. |
| RBP-004 | low | Workflow docs define workflow packages, not pack taxonomy. | Avoid adding bootstrap-pack taxonomy to workflow-standard docs. |

## Recommended Low-Risk Changes

- Add `bootstrap` to `ls/config/pack.yaml`.
- Add a bootstrap-pack docs index and pack metadata under `ls/docs/bootstrap-packs/`.
- Regenerate generated docs with existing tooling.
- Validate the catalog and diff.

## Deferred Changes

- Runtime deployment automation for global Codex config.
- External-folder replacement of legacy skill trees.
- Changes to `platforms.yaml` unless adapter behavior changes.
