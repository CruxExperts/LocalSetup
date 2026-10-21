# Next.js Latest Stable

Verified at `2026-09-03T06:17:06.012Z` from npm registry metadata for `next`.

## Current Package Facts

- Stable npm `latest`: `16.3.4`.
- Canary tag: `16.4.0-canary.15`.
- Beta tag: `16.0.0-beta.0`.
- RC tag: `15.0.0-rc.1`.
- Engine for `next@16.3.4`: Node `>=20.9.0`.
- React peers for `next@16.3.4`:
  `^18.2.0 || 19.0.0-rc-de68d2f4-20241204 || ^19.0.0`.
- Published at `2026-08-31T20:00:51.381Z`; age at verification was
  `209774631` ms, so the release was not inside the 48-hour quarantine window.
- Registry-reported integrity, signature values and keys, tarball, and SLSA
  provenance metadata are preserved in `../data/verified-versions.json`.

## Before Acting On These Facts

1. Inspect `package.json`, lockfile, CI, deployment target, and Node runtime
   pins.
2. Confirm whether the project uses the App Router, Pages Router, or both.
3. Check whether the app uses Node runtime, Edge Runtime, static export, or
   standalone output.
4. Read official upgrade notes for the project's current major version.
5. Treat canary, beta, RC, and experimental flags as opt-in only.

## Common Next.js Surfaces

- `next.config.*`: output mode, images, redirects, rewrites, headers,
  experimental flags, compiler options, bundle analyzer, and transpiled
  packages.
- `middleware.*` or `proxy.*`: request matching, auth, rewrites, redirects,
  cookies, and runtime restrictions.
- `app/`: Server Components, Client Components, server actions, route handlers,
  layouts, metadata, caching, and runtime declarations.
- `pages/`: API routes, data fetching functions, custom app/document, and
  migration boundaries.
- Deployment config: Dockerfile, Vercel config, CI, environment variables,
  standalone output, and cache directories.

## Stable Defaults

- Keep React and React DOM aligned.
- Prefer the project package manager and lockfile.
- Use the repo's `next build` path through `npm run build`, `pnpm build`, or the
  existing script.
- Avoid changing `experimental` settings unless the task is explicitly about
  that feature and the project accepts the risk.
