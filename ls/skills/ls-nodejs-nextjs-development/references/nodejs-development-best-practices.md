# Node.js Development Best Practices

## Project Hygiene

- Start from existing scripts. Prefer `npm run <script>` or the package-manager
  equivalent over ad hoc commands.
- Keep dependency, formatting, and semantic changes in separate commits when the
  diff would otherwise be noisy.
- Avoid global installs in repo workflows. Use project scripts, `npx`, `npm exec`,
  `pnpm exec`, or documented toolchain wrappers.
- Use environment validation for required variables at process startup.

## Runtime Boundaries

- Validate external input: CLI args, env vars, HTTP payloads, webhook bodies,
  cookies, headers, queue messages, and JSON files.
- Preserve error context with actionable messages and `cause` where useful.
- Add timeouts and cancellation for network calls and long-running IO.
- Keep secrets out of logs, error payloads, generated files, and client bundles.

## Module And Build Behavior

- Match the repo's module mode: CommonJS, ESM, or TypeScript `nodenext`/`bundler`.
- Do not change `type`, exports, transpilation, or path aliases without checking
  tests and runtime loading.
- Keep server-only modules out of browser bundles.
- Keep generated artifacts out of source edits unless the repo requires checked
  in generated output.

## Dependency Changes

- Read changelogs for major upgrades.
- Check package engines, peers, and deployment runtime support.
- Update lockfiles with the project's package manager.
- Run install, test, lint/typecheck, and build gates that match the changed
  surface.
