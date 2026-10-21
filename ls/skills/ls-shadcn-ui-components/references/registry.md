# Registry

shadcn/ui registry items are code distribution units. Treat official, private,
and community registries differently.

## Official And Custom Items

- Official namespace examples: `@shadcn/button`, `@shadcn/dashboard-01`.
- URL templates may contain `{name}` placeholders.
- Private registries can use headers or params with `${ENV_VAR}` placeholders.
- Registry schemas live at the official schema URLs in the source ledger.
- `registryDependencies` pull dependent items.
- `files[].target` may use alias placeholders so generated files land under the
  project's configured aliases.

## Safe Workflow

1. Inspect with `search`, `docs`, or `view`.
2. Preview with `add --view` or `add --dry-run`.
3. Confirm target paths and dependencies.
4. Apply the smallest install command.
5. Inspect local diffs before continuing.

## Boundaries

- Official registry docs are authoritative for official items.
- Community registry examples are not authoritative for shadcn/ui behavior.
- Do not paste raw registry code into the repo when the CLI can install and
  resolve dependencies safely.
