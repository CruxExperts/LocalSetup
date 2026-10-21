# Config Resolution

Resolution order:

1. CLI flags.
2. `LOCALSETUP_SECRETS_*` environment variables.
3. Repo-local `.localsetup/secrets/config.yaml` and `.localsetup/secrets/map.yaml`.
4. Legacy `secrets/keepass-config.yaml` plus `secrets/*-secrets-map.yaml`.
5. Global `~/.config/localsetup/secrets/config.yaml` and `~/.local/share/localsetup/secrets/maps/default.yaml`.

Config files may contain paths and backend metadata. They must not contain passwords, tokens, keyfiles, private keys, or other secret values.
