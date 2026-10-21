# Global vs Repo-Local Scope

Repo-local scope is preferred when a project owns the secret map. Use `.localsetup/secrets/config.yaml` and `.localsetup/secrets/map.yaml`.

Global scope is useful when one operator keeps a personal map shared across projects. Use `~/.config/localsetup/secrets/config.yaml` and `~/.local/share/localsetup/secrets/maps/default.yaml`.

Both scopes store references and metadata only. Secret values stay in KeePass.
