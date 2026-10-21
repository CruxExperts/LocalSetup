# Safe Component Update Example

Read first: `references/update-procedure.md`, `rules/updates.md`, and
`references/project-context.md`.

Capture clean status, run `info --json` (or text `info` for an older pinned
CLI), preview with `add --dry-run`, inspect `--view` and targeted
`--diff` output, then merge only the intended changes.

Failure modes: replacing customized local files, updating many components at
once, and skipping typecheck/lint/build after UI code changes.
