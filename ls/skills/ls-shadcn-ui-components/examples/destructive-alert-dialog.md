# Destructive Alert Dialog Example

Read first: `components/overlays-menus.md` and `references/accessibility.md`.

Use Alert Dialog for irreversible or high-impact confirmation. Keep the title
and description explicit, make cancel easy, and ensure the destructive action
cannot be triggered twice while pending.

Failure modes: using a normal Dialog for destructive confirmation, vague button
text, and missing disabled/loading state during submission.
