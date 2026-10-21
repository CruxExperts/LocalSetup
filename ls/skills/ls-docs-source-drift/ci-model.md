# CI Model

Docs alignment should run as a read-only gate in pull requests, pushes to `main`, merge queues, and manual workflow dispatch.

## Recommended order

1. Install dependencies.
2. Regenerate docs artifacts.
3. Run version-sync checks.
4. Run `docs_alignment.py check --ci`.
5. Write a concise GitHub job summary.
6. Run `git diff --exit-code`.

CI must not auto-commit. A future write workflow must be `workflow_dispatch` only, dry-run by default, branch-explicit, and permission-scoped.
