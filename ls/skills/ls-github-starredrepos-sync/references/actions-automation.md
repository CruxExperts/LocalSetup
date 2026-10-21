# Actions Automation

The workflow template defaults to manual `workflow_dispatch`.

Scheduled synchronization is opt-in because the default repository-scoped `GITHUB_TOKEN` usually cannot read an arbitrary user's stars or create/update a separate personal archive with the needed identity. Use a fine-grained token or GitHub App only after reviewing scopes.

Keep generated artifacts token-free and avoid printing secret values in logs.
