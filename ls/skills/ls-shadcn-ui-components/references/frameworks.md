# Frameworks

Use official installation pages and live CLI help before choosing a setup path.

## Supported Setup Families

- Next.js: check App Router vs Pages Router, RSC, Tailwind version, and CSS entry.
- Vite: check SPA structure, CSS entry, TS/JS mode, and path alias support.
- TanStack Start: current template name is `start`; verify file conventions.
- React Router: confirm framework mode and route/module boundaries.
- Astro: confirm React integration and island/client directives.
- Laravel: inspect Vite/Laravel integration, resources path, and build scripts.
- Manual React: verify bundler, Tailwind setup, alias support, and CSS import.
- Monorepo: locate app package, shared UI package, and package import/export
  contracts.

## Setup Controls

Current setup controls include `--monorepo`, `--base base|radix|aria`, `--rtl`,
`--pointer`, `--css-variables`, and `--preset`. Verify exact spelling with
`init --help` because this surface changes.

## Tailwind generations

- Tailwind v4 projects rely more heavily on CSS-first configuration.
- Older Tailwind projects may still have `tailwind.config.*` and content paths.
- Do not move token definitions between config and CSS without matching the
  project's Tailwind major version and current shadcn/ui docs.
