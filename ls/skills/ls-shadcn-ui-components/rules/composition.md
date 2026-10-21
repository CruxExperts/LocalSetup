# Composition Rules

- Compose from installed local components, not raw upstream snippets.
- Keep Dialog, Sheet, Drawer, Popover, Menu, Select, and Command children inside
  their documented primitive structure.
- Tabs triggers belong inside `TabsList`.
- Menu, Select, and Command items belong inside group/list components.
- Button loading state composes `Spinner` plus `disabled`.
- Icon-only controls need accessible names.
- Keep client-only interactions behind a client boundary in RSC projects.
