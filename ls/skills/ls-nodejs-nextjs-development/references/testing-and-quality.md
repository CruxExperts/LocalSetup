# Testing And Quality

## Prefer Existing Gates

Run the repo's scripts first:

```bash
npm run lint
npm run typecheck
npm test
npm run build
```

Adapt the command prefix for pnpm, Yarn, Bun, or the repo's task runner.

## What To Run

- Dependency or lockfile change: frozen install plus relevant tests and build.
- Server route, API, or auth change: unit/integration tests plus request-level
  checks.
- UI/component change: component tests or Playwright where available, plus build.
- Next.js config or runtime change: `next build` through the repo script and any
  deployment smoke command.
- TypeScript config change: typecheck and lint.

## Quality Checks

- Keep React and React DOM versions aligned.
- Check server/client boundaries.
- Check route handlers and server actions for validation and auth.
- Avoid broad snapshots unless the project already relies on them.
- Record skipped checks with a concrete reason.
