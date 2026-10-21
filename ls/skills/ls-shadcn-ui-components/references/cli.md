# CLI Reference

Use the runner selected from `packageManager` and lockfiles:

- npm: `npx shadcn@latest ...`
- pnpm: `pnpm dlx shadcn@latest ...`
- Yarn: `yarn dlx shadcn@latest ...`
- Bun: `bunx shadcn@latest ...`

The current package is `shadcn`; do not use the older `shadcn-ui` package name.

Prefer live help for exact syntax:

```bash
<runner> shadcn@latest --help
<runner> shadcn@latest <command> --help
```

## Current Commands

- `init` / `create`: initialize a new or existing project. Current template
  choices include `next`, `start`, `vite`, `react-router`, `laravel`, and
  `astro`. Setup controls include `--monorepo`, `--base base|radix|aria`, `--rtl`,
  `--pointer`, `--css-variables`, and `--preset`.
- `add [components...]`: install registry components or blocks into the local
  project. Preview with `--dry-run`, inspect with `--view`, and inspect specific
  diffs with `--diff [path]` when supported by live help.
- `apply`: apply registry or preset changes. Use `--only theme|font` when the
  current help supports partial preset application.
- `docs <components...>`: print official documentation links or JSON metadata;
  use `--json` and `--base` when live help exposes them.
- `view <items...>`: inspect registry items before install.
- `search` / `list`: search official and configured registries.
- `migrate`: current migration helpers include `cn`, `icons`, `base-color`,
  `radix`, and `rtl`; confirm flags with `migrate --help`.
- `info`: inspect project, config, preset, components, and links. Current
  official docs support top-level `info --json`.
- `eject`: eject a selected registry item into local project ownership; preview
  its current behavior with live help before use.
- `build`: build registry output from registry config.
- `registry add`: add registry configuration.
- `mcp init --client claude|cursor|vscode|codex|opencode`: initialize MCP
  configuration for supported clients. For Codex, official docs currently
  require adding the TOML configuration manually because the CLI cannot update
  `~/.codex/config.toml` automatically.
- `preset decode|resolve|info|url|open`: inspect and apply named, code, or URL
  presets safely.

## Command Discipline

- Run commands from the relevant package root. In monorepos, pass `--cwd`
  explicitly when live help supports it.
- Use `docs`, `view`, `add --view`, and `add --dry-run` before installing
  unfamiliar registry items.
- Avoid deprecated `diff` for new workflows unless the project or live help
  specifically requires it.
- Never document or use a flag that is not present in current official docs or
  live `--help`.
