# Login Form Example

Read first: `references/forms.md`, `rules/forms.md`, and `components/forms-inputs.md`.

Use Field, Input, Button, Alert, and optional Input Group. Keep validation state
on fields with `data-invalid` and `aria-invalid`. If using a schema library,
match the project's existing form stack.

Failure modes: unlabeled inputs, disabled submit without visible loading state,
client validation that disagrees with server validation, and importing from the
wrong alias.
