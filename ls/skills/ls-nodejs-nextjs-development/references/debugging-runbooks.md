# Debugging Runbooks

## Install Fails

1. Identify package manager and lockfile.
2. Check Node version against `engines`, CI, and deployment target.
3. Reproduce with the frozen install command when possible.
4. Read the first package-resolution or peer-dependency error, not only the final
   npm lifecycle failure.
5. Avoid deleting lockfiles unless the task is to re-resolve dependencies.

## Build Fails

1. Run the repo build script.
2. Capture Node, package manager, Next.js, React, and TypeScript versions.
3. Separate TypeScript, ESLint, bundling, route analysis, and runtime errors.
4. For server/client boundary errors, inspect imports from the failing module
   upward.
5. For deployment-only failures, compare env vars, runtime, image, and output
   mode with local settings.

## Runtime Fails

1. Identify runtime: Node, serverless, Edge, static export, or Docker.
2. Check environment variables and secret availability.
3. Reproduce the failing route or action with logs that exclude secrets.
4. Validate request input, cookies, headers, auth state, and cache state.
5. Confirm whether middleware/proxy logic rewrites or redirects the request.

## Hydration Or Client Errors

1. Check browser console and server logs.
2. Look for nondeterministic render values, browser-only APIs, timezone/date
   formatting, feature flags, and user-specific data.
3. Confirm package versions for React and React DOM match.
4. Minimize the component tree around the mismatch before changing architecture.
