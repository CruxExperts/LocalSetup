# Migration From v1

Existing legacy files can stay in place:

- `secrets/keepass-config.yaml`
- `secrets/*-secrets-map.yaml`

The new preferred repo-local paths are:

- `.localsetup/secrets/config.yaml`
- `.localsetup/secrets/map.yaml`

Run map-validate before adopting the new map and reference-validation CLI. Real-vault automation is outside this package.
