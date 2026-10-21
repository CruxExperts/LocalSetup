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
