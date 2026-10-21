# Styling Rules

- Use semantic tokens before raw color classes.
- Use component variants before custom classes.
- Use `cn()` for conditional class composition.
- Prefer `gap-*` over `space-*` for component layout.
- Use `size-*` for square icon/button sizing.
- Avoid manual dark-mode overrides when tokens cover the state.
- Avoid manual overlay `z-index` unless debugging proves the local stacking
  context requires it.
- Keep text, icon spacing, and focus rings consistent with generated components.
