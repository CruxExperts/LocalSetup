---
status: ACTIVE
version: 5.7
owner_skill: ls-documentation-alignment
---

# Document lifecycle (LocalSetup)

**Purpose:** Framework docs in `ls/docs/` must have a defined status. Check status before assuming a feature is implemented.

## Status values

| Status    | Meaning |
|-----------|---------|
| ACTIVE    | In effect; use as current guidance. |
| PROPOSAL  | Under consideration; not yet implemented. Do not assume behavior is in place. |
| DRAFT     | Work in progress; may change. |
| DEPRECATED| No longer recommended; see replacement if noted. |
| ARCHIVED  | Retained for reference only. |

## Practice

- Every framework doc under `ls/docs/` (and in source `ls/docs/`) should include YAML front matter with `status:` and, where applicable, `version:`.
- Before referencing a doc for core rules or behavior, check its status. If PROPOSAL, confirm with the user before relying on described behavior.
- See [AGENTIC_DESIGN_INDEX.md](AGENTIC_DESIGN_INDEX.md) for the index of framework docs.
