# Theming

Prefer semantic tokens and the project's generated theme contract.

## Rules

- Use tokens such as `bg-background`, `text-foreground`, `border-border`,
  `bg-primary`, `text-primary-foreground`, `bg-muted`, and `text-muted-foreground`.
- Do not hardcode parallel light and dark color overrides unless the project
  explicitly owns that pattern.
- Use OKLCH values where the current generated theme uses OKLCH.
- Keep CSS variables in the documented CSS entry file.
- Use component variants before custom class overrides.
- Use `cn()` for conditional classes and local composition.

## Dark Mode

Check whether dark mode is class-based, data-attribute-based, framework-managed,
or custom. Use existing provider/theme toggles rather than creating a second
theme system.

## Pointer Cursor And RTL

Pointer cursor and RTL are setup-level behaviors. Prefer CLI migration/setup
support over hand-editing many components.
