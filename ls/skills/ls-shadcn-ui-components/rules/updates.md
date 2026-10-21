# Update Rules

- Preview before applying: `view`, `add --view`, `add --dry-run`, and targeted
  `--diff` when supported.
- Inspect local customizations before replacing files.
- Keep registry config changes separate from broad component updates when
  possible.
- Prefer one component or pattern at a time for risky updates.
- Run the repo's typecheck, lint, tests, and build after UI updates.
