# Forms And Inputs

| Component | Use for | Notes |
|---|---|---|
| Field | form structure | Prefer Field family for labels, descriptions, groups, and invalid state. |
| Input | text-like input | Pair with Label or FieldLabel; wire invalid state. |
| Input Group | affixed inputs and textareas | Use `InputGroupInput` or `InputGroupTextarea` inside the group. |
| Textarea | multiline input | Keep descriptions/errors close to the field. |
| Label | accessible labels | Ensure it is associated with a control. |
| Checkbox | boolean choices | Use controlled state in form libraries. |
| Radio Group | one choice from many | Keep items grouped with labels. |
| Switch | immediate boolean toggle | Do not use for deferred submit-only choices unless UX expects it. |
| Slider | bounded numeric value | Provide accessible labels and visible value when useful. |
| Select | constrained list | Keep items inside Select structure. |
| Native Select | simple native select | Prefer when native platform behavior is desired. |
| Combobox | searchable selection | Often a pattern composed from Command and Popover. |
| Calendar | date grid | Pair with Date Picker or custom state. |
| Date Picker | date selection pattern | Pattern/example; verify install path and dependencies. |
| Input OTP | one-time codes | Preserve grouped slots and accessible labels. |

Failure modes: missing labels, invalid state without `aria-invalid`, treating
pattern docs as direct installs, and losing client boundaries in RSC projects.
