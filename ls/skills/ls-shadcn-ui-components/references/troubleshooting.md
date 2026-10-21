# Troubleshooting

Start with project state, then reproduce with the smallest shadcn CLI command.

## Common Failures

- Missing `components.json`: project not initialized or wrong working directory.
- Wrong imports: aliases or package imports differ from `@/components/ui`.
- Component not found: docs-only pattern, registry mismatch, or stale item name.
- CSS missing: wrong Tailwind major setup, CSS entry not imported, monorepo UI
  package styles not imported by the app.
- Icon import failure: configured `iconLibrary` differs from assumptions.
- Hydration or RSC error: interactive component crossed a server/client boundary.
- Overlay layering issue: custom z-index or nested portal container changed
  primitive behavior.
- Form a11y issue: label/error state not wired to control.

## Debug Order

1. `info --json`; fall back to text `info` if the pinned/local CLI predates it
2. `docs <component> --json` when available
3. `view <component or item>`
4. `add --dry-run <item>`
5. targeted build/lint/typecheck/test from the repo's scripts
6. inspect generated files and aliases
