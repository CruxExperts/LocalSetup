---
status: DRAFT
version: 0.1
---

### Secrets overview

This repository uses KeePass as the primary store for infrastructure secrets. The goal is to keep all secret values out of tracked files while still giving agents and humans a stable way to reference them.

Key pieces:

- secrets/keepass-config.yaml describes external KeePass database locations, for example ../vaults/infra.kdbx, without embedding master passwords or keyfiles.
- `secrets/*-secrets-map.yaml` files map logical IDs such as `mail.host01.example.admin` to KeePass entry paths like `Servers/host01/Mail/admin@example.com`.
- An adopting repo may provide a .keepass_secrets helper or another tracked helper CLI; ls-keepass-secret-references defines logical-ID mapping, reference, and safety rules only. It does not resolve vault values.

### Referencing secrets in docs

When you need to mention credentials in documentation, reference the logical ID instead of pasting the username and password. For example:

- `Secret ID: mail.host01.example.admin`
- `Secret ID: postgres.host01.app1`

When you or an agent need actual values, use an approved human-operated secret-manager process. ls-keepass-secret-references does not display or resolve values from a KeePassXC vault.

### Where the actual secrets live

- Secret values (passwords, tokens, key material) live only in KeePass `.kdbx` databases and in short-lived CLI output.
- KeePass databases, keyfiles, and master passwords must not live under `secrets/`, an adopting repo's `.keepass_secrets/` helper directory, or any tracked helper directory. They stay outside the repo and are managed by humans or external secret managers.
