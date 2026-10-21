# Node.js Runtime And Package Management

Verified runtime facts were checked at `2026-09-03T06:17:06.012Z` against
the Node release schedule and distribution index.

## Runtime Selection

- Prefer Active LTS for new production work when the framework and host support
  it. On the verification date, that line was Node `24.x` Krypton.
- Maintenance LTS is valid for existing systems, but should carry an upgrade
  plan. On the verification date, Node `22.x` Jod was Maintenance LTS.
- Do not choose EOL lines for new production work. Node `20.x` Iron ended on
  2026-04-30.
- Do not choose Current lines as production defaults unless a project explicitly
  requires them and the host supports them.

## Project Runtime Pins

Check these before changing Node:

- `package.json` `engines.node`
- `packageManager`
- `.nvmrc`
- `.node-version`
- `.tool-versions`
- `volta` config in `package.json`
- Docker image tags
- CI setup actions
- deployment platform runtime settings

## Package Manager Rules

- Prefer the manager already indicated by `packageManager` and lockfiles.
- Use Corepack when the project expects package-manager shims and the host
  supports them.
- Do not switch between npm, pnpm, Yarn, or Bun unless the migration is the task.
- Keep one lockfile family unless the repo intentionally supports several
  package managers.
- Use `npm ci`, `pnpm install --frozen-lockfile`, `yarn install --immutable`, or
  the repo's documented frozen install command in CI.

## Engines And Peers

- `engines` expresses compatibility constraints; it is not automatically the
  best runtime choice.
- Peer dependencies must be checked before changing React, Next.js, test
  runners, lint plugins, or build adapters.
- For Next.js apps, React and React DOM should normally be kept on matching
  stable versions.
