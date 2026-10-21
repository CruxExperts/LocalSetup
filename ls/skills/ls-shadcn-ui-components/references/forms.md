# Forms

Use the Field family and the project's validation library rather than inventing
a new form architecture.

## Field Composition

- `FieldGroup` groups fields.
- `Field`, `FieldSet`, and `FieldLegend` structure controls.
- `FieldLabel` labels inputs.
- `FieldDescription` explains helper text.
- Use `data-invalid` and `aria-invalid` for invalid state.
- Use `InputGroupInput` and `InputGroupTextarea` inside Input Group.

## Validation

- Keep validation schemas at boundaries where the project already keeps them.
- Use React Hook Form, Zod, or other libraries only when present or explicitly
  requested.
- Make error text discoverable by assistive tech.
- Keep server actions/API submission and client validation boundaries clear.

## Common Mistakes

- Label text without an associated control.
- Error styling without `aria-invalid`.
- Mixing raw `div` wrappers when `Field` components already express structure.
- Treating Date Picker, Combobox, and Select as plain inputs in validation code.
