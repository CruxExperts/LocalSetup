# Command Reference

Run supported validation commands from the skill directory or pass paths explicitly:

~~~bash
python3 scripts/localsetup_secrets.py map-validate --map examples/map.yaml
python3 scripts/localsetup_secrets.py reference '{{secret:mail.box03.example.admin:password}}'
~~~

Commands:

- doctor: show runtime, config, map path, and capability-guard availability; it never opens a vault.
- config-show: show resolved non-secret config.
- config-init: write .localsetup/secrets/config.yaml with --apply; a keepassxc configuration remains a non-operational capability guard.
- config-validate: validate backend and config shape.
- map-validate: validate and flatten map entries and aliases.
- list and search: inspect mapping metadata, and fake entries only when --backend fake is explicitly selected.
- reference: parse a supported secret reference.
- resolve, ensure, set, rotate, delete, export-env, and render-template: operate only with --backend fake in isolated tests; with keepassxc, they report missing_backend when the CLI is unavailable or interactive_backend_required before vault access.
- audit: report map consistency findings.
- vault-info: show capability-guard metadata only.
- vault-init: reserved dry-run; it does not create or open a vault.
- vault-backup: copy a configured file with --apply. It does not unlock, validate, or otherwise access a vault.
- schema-dump: print a built-in command contract.
