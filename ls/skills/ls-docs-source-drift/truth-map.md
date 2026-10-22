# Truth Map Guide

The truth map is generated at `ls/docs/_generated/docs-truth-map.json`.

Each claim has:

- `value`: the computed current value.
- `sources`: the files or globs that back the value.
- optional generated mirrors, such as the value currently present in `facts.json`.

Use `explain` to inspect one claim:

```bash
uv run --locked python ls/tools/docs_alignment.py --repo-root . explain --claim-id workflow_count
```

When adapting this workflow to another repo, keep the same shape but replace the LocalSetup source rows with that repo's package manifest, API schema, CLI registry, generated docs, or CI source.

## Accuracy, freshness, and downstream use

Before recommending that guidance be updated or retired, report these separately:

- **Accuracy:** `confirmed-current`, `confirmed-inaccurate`, or `unresolved`.
  Cite the reviewed source revision or primary evidence and the exact claim it
  supports or contradicts. Lack of a recent check means unresolved, not inaccurate.
- **Review status:** `within-policy`, `review-due`, or `no-review-policy`.
  Give the last verified date and applicable review interval. Age alone does not
  prove an error or justify deletion.
- **Lifecycle:** active guidance or preserved historical evidence. Preserve
  published records and explain their historical scope instead of rewriting them
  as current instructions.
- **Downstream use:** list confirmed inbound references, dependent workflows,
  generators, package bundles, aliases, and operational procedures. Follow
  transitive references where relevant to the proposed change, especially recovery
  instructions. Distinguish textual references from confirmed runtime use.

Search the selected repository's tracked source, documentation, workflow manifests,
package metadata, and generated reference inventories. Search the reference's
path, title, stable identifier, aliases, and relative-link forms. Record the search
revision, roots, patterns, exclusions, access limitations, and indirect or external
uses that could not be checked. An empty search means "no uses found within this
scope", never "unused everywhere". Do not broaden access to private repositories
or logs just to complete the search.

Recommend one disposition with reasons: retain, refresh the contradicted claims,
preserve as history, retire after migrating known dependents, or investigate an
unresolved question. Explain affected consumers and unresolved risks before
changing content. If retirement is selected, update dependent links and discovery
entries through their owning generators in the same slice, preserve meaningful
historical evidence, and check the remaining references. A retirement review is a
recommendation; it does not independently authorize destructive deletion.
