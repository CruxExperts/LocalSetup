# Registry Patterns

Registry blocks can install full page sections, dashboards, login/signup flows,
chart examples, sidebars, or private components.

## Workflow

1. Search or identify item.
2. `view` the item.
3. Preview install with `add --view` or `add --dry-run`.
4. Confirm target aliases, dependencies, and file overwrites.
5. Apply and inspect diff.

## Pattern Types

- Dashboard blocks: usually combine Sidebar, Cards, Chart, Table, and Empty.
- Auth blocks: login/signup forms with validation and layout assumptions.
- Chart blocks: chart dependencies and data-shape assumptions.
- Sidebar blocks: app-shell structure and provider/state assumptions.
- Private registry items: headers/params should use environment variables.
