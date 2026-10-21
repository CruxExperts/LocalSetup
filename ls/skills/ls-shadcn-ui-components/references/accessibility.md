# Accessibility

shadcn/ui components compose accessible primitives, but local composition can
still break accessibility.

## Baseline Checks

- Dialog, Alert Dialog, Sheet, and Drawer need titles.
- Avatar needs a meaningful fallback.
- Tabs triggers stay inside `TabsList`.
- Menu, Select, and Command items stay inside their group components.
- Tooltip content should not replace visible labels for core actions.
- Icon-only buttons need accessible names.
- Loading buttons should be disabled and include perceivable state.
- Form controls need labels and invalid state wiring.
- Keyboard navigation should work without custom tab-index choreography.

## Test Checklist

- Keyboard open, close, and focus return.
- Screen-reader names for interactive controls.
- Escape and outside-click behavior for overlays when appropriate.
- Reduced-motion behavior for custom animation.
- LTR/RTL layout when `rtl` is enabled.
