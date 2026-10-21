# Docs Routing Reference

This reference contains the schemas, examples, and validation scenarios for `ls-docs-create-move`. Keep the root `SKILL.md` compact and use this file for detailed implementation guidance.

## Input Payload

Required fields:

- `intent`: short natural-language description of what the doc is for.
- `title`: proposed human title.

Optional fields:

- `summary`: one to three sentence description of the expected content.
- `doc_type_hint`: coarse type such as `runbook`, `adr`, `spec`, `how_to`, `notes`, or `reference`.
- `tags`: list of component, domain, feature, or workflow tags.
- `allow_nonstandard_docs`: boolean override flag, default false.

Default-root example (`<root>` resolves to `docs`):

```yaml
intent: "Document how to rotate API keys for the payments service"
title: "Payments API key rotation runbook"
summary: "Step by step runbook for rotating API keys in production for the payments service."
doc_type_hint: "runbook"
tags:
  - "payments"
  - "api-keys"
  - "runbook"
allow_nonstandard_docs: false
```

## Output Payload

Return the recommendation and any warnings in a structured shape:

```yaml
category_label: "runbook"
category_slug: "incident-runbooks"
component_slug: "payments"
proposed_path: "docs/incident-runbooks/payments"
filename: "payments-api-key-rotation.md"
index_entry:
  id: "docs-incident-runbooks-payments-api-key-rotation"
  path: "docs/incident-runbooks/payments/payments-api-key-rotation.md"
  title: "Payments API key rotation runbook"
  category_label: "runbook"
  category_slug: "incident-runbooks"
  component_slug: "payments"
  status: "ACTIVE"
  last_updated: "2026-05-09"
  doc_type: "runbook"
  tags:
    - "payments"
    - "api-keys"
    - "runbook"
  nonstandard_location: false
warnings: []
```

When a selected path differs from the recommended placement, a permitted override should include a `nonstandard_location` warning. A denied override returns the warning and recommendation without creating a document or index entry.

## Category Manifest

Optional repo-local manifest:

- Path: `<root>/.docs-classifications.yaml`
- Purpose: record known documentation categories and their folder slugs.

Each entry should include:

```yaml
- label: "runbook"
  slug: "incident-runbooks"
  description: "Operational procedures and recovery playbooks."
```

Classification behavior:

- Load the manifest when present.
- Reuse a fitting label and slug before creating a new category.
- Append a new label and slug only when no existing category fits.
- Treat the manifest as repo-local configuration, not framework-owned state.

## Optional Config

Optional repo-local config:

- Path: `docs.config.yaml`
- Purpose: override defaults for a specific repository.

Supported fields:

```yaml
root: "docs"
categories:
  runbook:
    slug: "incident-runbooks"
    description: "Operational procedures and recovery playbooks."
aliases:
  playbook: "runbook"
required_front_matter_fields:
  - "status"
  - "last_updated"
  - "doc_type"
  - "tags"
exclude_globs:
  - "_generated/**"
```

`docs.config.yaml` remains at repository root. Load it first, resolve `root` as a normalized repo-relative `<root>` (default `docs`), strip trailing separators, and reject absolute paths or `..` escapes. Then derive `<root>/index.yaml`, `<root>/INDEX.md`, and `<root>/.docs-classifications.yaml`; apply config categories, aliases, required fields, and exclusions; load the category manifest for unresolved values; and use heuristics last.

Changing the root changes all three active metadata paths. Do not retain `docs/index.yaml` as a second active index or silently move or merge legacy state. Report split state for an explicit consolidation decision. Exclusions apply before proposing or accepting a path, and `allow_nonstandard_docs` does not bypass them.

## Slug Examples

- `Incident runbooks` becomes `incident-runbooks`.
- `API reference` becomes `api-reference`.
- `User settings panel` becomes `user-settings-panel`.

Use lowercase ASCII, replace separators with hyphens, and remove characters unsafe for common filesystems.

## Reuse Examples

Strong update candidates:

- Same `component_slug` and very similar title.
- Same `category_label` and overlapping tags where the topic is clearly the same.
- Existing `<root>/index.yaml` entry whose path and title match the requested work.

Weak candidates:

- Same broad category but different component.
- Same tag with an unrelated title.
- Similar filename but unrelated metadata.

When a strong candidate exists, recommend updating the existing path and include the proposed new path only as context.

## Document Metadata

Managed docs should include front matter or a compact metadata header with at least:

```yaml
status: "ACTIVE"
last_updated: "2026-05-09"
doc_type: "runbook"
tags:
  - "payments"
  - "api-keys"
```

Use lifecycle values from the repo's documentation lifecycle guidance when present. Refresh `last_updated` on significant changes.

## Human Index Shape

`<root>/INDEX.md` should be derived from `<root>/index.yaml`. Links inside the human index are relative to `<root>`. A typical category section is:

```markdown
## Runbooks

- Payments API key rotation runbook (`incident-runbooks/payments/payments-api-key-rotation.md`) - payments, api-keys
```

Choose one ordering rule within each category, usually alphabetical by title or descending by `last_updated`, and keep it consistent.

## Validation Scenarios

New repo using the default root:

- Start with no `docs/` directory.
- Create one architecture doc, one runbook, and one how-to.
- Verify docs folders, `<root>/index.yaml`, and `<root>/INDEX.md` are created and aligned.

Messy existing repo:

- Start with ad hoc docs and partial topic coverage.
- Add a doc request for an already-covered topic.
- Verify the skill recommends updating the existing doc when appropriate.

Move and rename:

- Move a managed doc from one folder to another.
- Verify the path changes, the stable `id` stays the same, and both indexes update.

Custom root:

- Set repo-root `docs.config.yaml` to `root: "project-docs/"`.
- Verify the normalized root is `project-docs`, recommended documents live below it, and the active metadata files are `project-docs/index.yaml`, `project-docs/INDEX.md`, and `project-docs/.docs-classifications.yaml`.
- Verify `docs/index.yaml` is not treated as a fallback active index; report existing split state instead of silently consolidating it.

Nonstandard placement denied:

- Request a path that differs from the recommendation with `allow_nonstandard_docs: false` or omit the flag.
- Verify the result returns requested and recommended paths, a warning, and a confirmation-needed state without creating, moving, or indexing a document.

Nonstandard placement approved:

- Intentionally write a doc outside the recommended location with `allow_nonstandard_docs: true`.
- Verify containment, exclusions, ownership, and generated/private boundaries still pass; the warning is present; and the single `<root>/index.yaml` entry records the actual repo-relative path with `nonstandard_location: true`.
