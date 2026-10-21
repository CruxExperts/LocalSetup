# Agent Usage Patterns

Use logical IDs in docs and workflows, never secret values.

For discovery:

```bash
python3 scripts/localsetup_secrets.py map-validate --map examples/map.yaml
python3 scripts/localsetup_secrets.py reference '{{secret:mail.box03.example.admin:password}}'
```

For tests:

```bash
python3 scripts/localsetup_secrets.py resolve mail.box03.example.admin --backend fake --map examples/map.yaml
```

Do not request or display real vault values through this package. A KeePassXC-backed vault command reports missing_backend when the CLI is unavailable or interactive_backend_required before vault access; use map validation or reference parsing, and reserve --backend fake for isolated tests.
