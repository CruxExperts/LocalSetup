# Command Dialog Example

Read first: `components/overlays-menus.md` and `references/accessibility.md`.

Compose Command inside Dialog for a command palette. Keep the dialog title
available to assistive tech, group command items, and handle keyboard shortcuts
in the existing app pattern.

Failure modes: no accessible dialog title, command items outside command list,
and global shortcuts that fire while typing in inputs.
