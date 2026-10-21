# Source Ledger

Volatile facts were last verified on 2026-09-04 UTC from the official shadcn/ui
site, changelog, npm registry metadata, and published schemas. The verifier
checks package structure, source reachability, and npm latest metadata; it does
not parse every current documentation claim below. From the LocalSetup
repository root, re-run
`python3 ls/skills/ls-shadcn-ui-components/scripts/verify_shadcn_sources.py --refresh --json`
and manually review the cited official sources before release work or before
relying on syntax-sensitive current facts.

## Primary Sources

- Documentation: <https://ui.shadcn.com/docs>
- Components: <https://ui.shadcn.com/docs/components>
- LLM index: <https://ui.shadcn.com/llms.txt>
- Installation: <https://ui.shadcn.com/docs/installation>
- CLI: <https://ui.shadcn.com/docs/cli>
- components.json: <https://ui.shadcn.com/docs/components-json>
- Package imports: <https://ui.shadcn.com/docs/package-imports>
- Theming: <https://ui.shadcn.com/docs/theming>
- Forms: <https://ui.shadcn.com/docs/forms>
- Dark mode: <https://ui.shadcn.com/docs/dark-mode>
- RTL: <https://ui.shadcn.com/docs/rtl>
- Monorepo: <https://ui.shadcn.com/docs/monorepo>
- Skills: <https://ui.shadcn.com/docs/skills>
- MCP: <https://ui.shadcn.com/docs/mcp>
- Registry: <https://ui.shadcn.com/docs/registry>
- Registry schema: <https://ui.shadcn.com/schema/registry.json>
- Registry item schema: <https://ui.shadcn.com/schema/registry-item.json>
- GitHub repository: <https://github.com/shadcn-ui/ui>
- Verified package snapshot: <https://raw.githubusercontent.com/shadcn-ui/ui/7c9eaba1c0a6404c990c144a654792e3313c650d/packages/shadcn/package.json>
- Official upstream skill: <https://github.com/shadcn-ui/ui/tree/main/skills/shadcn>
- GitHub releases: <https://github.com/shadcn-ui/ui/releases>
- Changelog: <https://ui.shadcn.com/docs/changelog>
- npm package metadata: <https://registry.npmjs.org/shadcn>

## Verified Snapshot

- Latest npm package observed: `shadcn@4.21.0`, published
  `2026-09-04T05:34:07.022Z`. Official docs use
  `shadcn@latest`; the current package is `shadcn`, not the older `shadcn-ui`
  package name. Because this release was published on the observation date,
  recheck npm metadata immediately before relying on it as the current version.
- Official docs/changelog around this version mention package imports, registry
  target alias placeholders, previous-version error suggestions, presets,
  pointer cursor setup, partial preset apply, RTL, Radix migration, and Sera.
- Official docs/changelog list commands including `init`/`create`, `apply`,
  `add`, deprecated `diff`, `docs`, `view`, `search`/`list`, `migrate`,
  `info`, `eject`, `build`, `mcp init`, `preset decode|resolve|info|url|open`,
  and `registry add`.
- Official docs confirm top-level `shadcn info --json`. JSON is also documented
  for commands including `docs`, `preset resolve`, and `preset info`.
- `docs`, `view`, `add --dry-run`, `add --diff`, and `add --view` remain
  current.
- Template targets observed for `init -t`: Next.js, Vite, TanStack Start,
  React Router, Laravel, and Astro. Base choices are `base`, `radix`, and `aria`.

## Known Conflicts

- Docs pages and registry items are not one-to-one. Some docs pages are
  patterns or examples rather than simple `registry:ui` installs.
- The current `components.json` schema does not expose a top-level `base` field.
  Base is selected through CLI setup flags, style values such as `radix-nova`
  or `base-nova`, and `info` output.
- The current `components.json` schema supports `iconLibrary`, `rtl`, and
  `registries`.
- Base UI is the default and recommended choice for new projects as of July
  2026. Radix remains supported with `-b radix` or `--base radix`, and React
  Aria is supported with `--base aria`.
- Rendered documentation and current source disagree on the exact preset token
  implied by `--defaults`; verify that syntax with current help rather than
  encoding either value here.
- Live CLI help should win over stale rendered docs when syntax differs.

## Official Component Pages Checked

Accordion, Alert, Alert Dialog, Aspect Ratio, Avatar, Badge, Breadcrumb, Button,
Button Group, Calendar, Card, Carousel, Chart, Checkbox, Collapsible, Combobox,
Command, Context Menu, Data Table, Date Picker, Dialog, Direction, Drawer,
Dropdown Menu, Empty, Field, Hover Card, Input, Input Group, Input OTP, Item,
Kbd, Label, Menubar, Native Select, Navigation Menu, Pagination, Popover,
Progress, Radio Group, Resizable, Scroll Area, Select, Separator, Sheet,
Sidebar, Skeleton, Slider, Sonner, Spinner, Switch, Table, Tabs, Textarea,
Toast, Toggle, Toggle Group, Tooltip, Typography.
