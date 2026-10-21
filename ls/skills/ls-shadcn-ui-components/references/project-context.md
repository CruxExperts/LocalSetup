# Project Context

Start every task by building a local project picture.

## Discovery

- Locate `package.json`, lockfiles, workspace files, framework config, Tailwind
  config or CSS entry, and the nearest `components.json`.
- Read `packageManager`; use lockfiles only when `packageManager` is missing.
- Run `<runner> shadcn@latest info --json` from the project root for structured
  project context; fall back to text output if the pinned/local CLI predates it.
- For monorepos, find all `components.json` files and map each file to the
  package where generated UI code lives.

## Info Fields To Use

When structured JSON output is available, inspect:

- `project`: framework, RSC, TypeScript, Tailwind version, and detected paths.
- `config`: aliases, style, icon library, TSX mode, CSS variables, registries,
  RTL, and resolved paths. If `null`, the project is not initialized.
- `preset`: selected preset details when present.
- `components`: installed components and their resolved files when available.
- `links`: official URLs and docs surfaced by the CLI.

## Import Rules

- Use `config.aliases.ui`, `config.aliases.components`, `config.aliases.lib`,
  `config.aliases.hooks`, and `config.aliases.utils`.
- If package imports are configured, honor `package.json#imports` such as
  `#components/*` instead of forcing TypeScript `paths` aliases.
- Do not assume `@/components/ui`, `@/lib/utils`, or `lucide-react`.
- Preserve local barrel exports or package exports only when the project already
  uses them.

## RSC And Client Boundaries

In Next.js App Router projects, interactive components generally belong behind
a client boundary. Keep server data loading in server components and pass plain
serializable data into client UI components.
