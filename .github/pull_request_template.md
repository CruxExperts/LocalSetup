## Summary

Describe what changed and why.

## Linked issue or context

Link the issue, discussion, or maintenance note this PR addresses.

## Compatibility impact

- Supported platforms affected:
- Install, adapter, skill, workflow, or package behavior changed: yes/no
- Generated docs or version surfaces changed: yes/no
- Public package boundary changed: yes/no

## Validation

List the commands you ran and their results.

```bash
uv run --locked python ls/tools/localsetup.py --source-root . version-plan
uv run --locked python ls/tools/localsetup.py --source-root . version-sync --check --target "$(cat VERSION)"
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/localsetup.py --source-root . docs-align check --ci
uv run --locked pytest ls/tests -q
git diff --check
```

## Release notes

- Conventional Commit type:
- `Release-Type:` trailer included for major/minor/none or breaking-marker commits: yes/no/not applicable
- Version-sync commit included when required: yes/no
- Screenshots, logs, or artifacts attached when useful: yes/no
