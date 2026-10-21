# Security And Deployment

## Security Review

- Validate inputs at HTTP, server action, CLI, env, and file boundaries.
- Keep secrets server-side and out of client bundles, logs, error messages, and
  generated artifacts.
- Check auth and authorization in route handlers, API routes, middleware/proxy,
  and server actions.
- Review redirects, rewrites, URL construction, image remote patterns, CORS, and
  headers.
- Check dependency advisories and deployment platform security notices for
  affected Next.js versions.

## Deployment Review

- Identify deployment target: Vercel, self-hosted Node, Docker, serverless, Edge,
  or static export.
- Confirm Node runtime support on the host before upgrading.
- For Docker, prefer multi-stage builds, frozen installs, non-root runtime users,
  minimal runtime images, and explicit health checks where appropriate.
- For Next.js standalone output, confirm file tracing, static assets, public
  files, and environment variables are included correctly.
- For Edge Runtime, avoid Node-only APIs and packages.

## Production Baselines

- Prefer Active LTS Node for new production baselines when compatible.
- Do not choose EOL Node for new production work.
- Do not promote Current Node, canary Next.js, beta Next.js, RC React, or
  experimental React as stable defaults.
