# Next.js Development Best Practices

## Server And Client Boundaries

- Treat files as Server Components unless the project marks them with
  `"use client"`.
- Keep server-only modules, secrets, database clients, and privileged APIs out of
  Client Components.
- Use route handlers or server actions deliberately, with input validation and
  auth checks at the boundary.
- Confirm runtime declarations before using Node-only APIs in Edge Runtime code.

## Caching And Data

- Inspect existing cache policy before changing fetch options, route segment
  config, revalidation, or dynamic rendering.
- Be careful with personalized data and shared caches.
- For debugging stale data, identify whether the issue is fetch cache, route
  cache, CDN, browser cache, or deploy platform behavior.

## Configuration

- Review `next.config.*` before build or deploy changes.
- Avoid adding experimental flags as a workaround unless the project has chosen
  that risk.
- Check image remote patterns, rewrites, redirects, headers, and output mode when
  deployment behavior changes.

## Migration

- Move one major boundary at a time when the app is large or risky.
- Read official upgrade notes and codemod docs for the exact source and target
  versions.
- Keep React, React DOM, Next.js, TypeScript, ESLint config, and test tooling
  compatibility together.
- Validate both local build and deploy-target behavior when runtime mode changes.
