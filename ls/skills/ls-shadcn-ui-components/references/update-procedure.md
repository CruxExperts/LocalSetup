# Update Procedure

shadcn/ui code is copied into the project. Updating means merging upstream
generated changes into local owned files.

## Safe Component Update

1. Capture current project state: `git status --short`.
2. Run `info --json` and confirm aliases, base, icon library, TSX, and RSC. Fall
   back to text `info` if the pinned/local CLI predates structured output.
3. Preview:

```bash
<runner> shadcn@latest add <component> --dry-run
<runner> shadcn@latest add <component> --view
```

4. Inspect targeted diffs with `--diff [path]` when live help supports it.
5. Apply only when the expected files and dependencies are understood.
6. Manually preserve local customizations.
7. Run project validation: typecheck, lint, test, and build when available.

## Do Not

- Blindly reinstall all components.
- Replace customized local components without diff review.
- Mix Radix and Base migrations in the same change unless the task requires it.
- Update registry config and component code in one noisy diff when separate
  commits would be clearer.
