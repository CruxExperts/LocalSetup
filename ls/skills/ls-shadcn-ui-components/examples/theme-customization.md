# Theme Customization Example

Read first: `references/theming.md` and `rules/styling.md`.

Customize semantic tokens in the documented CSS entry. Preserve generated token
names and variants so components keep working across light/dark mode.

Failure modes: raw color overrides inside every component, duplicating dark-mode
systems, and changing OKLCH/RGB formats without checking the current theme.
