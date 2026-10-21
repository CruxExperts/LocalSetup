# Base UI vs Radix Rules

- Confirm base from CLI `info`, setup flags, style values, and generated code.
- Base UI is the default for new projects as of July 2026; choose Radix with
  `-b radix` or `--base radix` when maintaining Radix compatibility. React Aria
  is also supported with `--base aria`.
- Radix composition commonly uses `asChild`.
- Base UI composition commonly uses `render`.
- Use `nativeButton={false}` only when a Base UI part that defaults to native
  button behavior is rendered as a non-button element. Some Base UI parts have
  inverse/default-false behavior and may require `nativeButton={true}` when
  rendered as a native button.
- Do not mix primitive APIs from Radix and Base in the same component.
- Use migration helpers when moving existing projects between primitive families.
- Re-check imports after migration; newer Radix migration flows may consolidate
  imports into the `radix-ui` package.
