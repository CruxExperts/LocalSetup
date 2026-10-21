# OmniRoute Upstream Skill Coverage

LocalSetup consolidates OmniRoute v3.8.48 into four native skills:

- `ls-omniroute-task-triage`: first routing, issue triage, env/auth preflight, generic deterministic API CLI, Codex and agent-client onboarding.
- `ls-omniroute-model-discovery`: all read-only model/provider, context, observability, integration, client and endpoint discovery, plus sanitized normalized model observations.
- `ls-omniroute-admin-automation`: every write, import, purge, service, settings, provider, key, integration, backup/restore and rollback-safe control workflow.
- `ls-omniroute-skills-update`: immutable source provenance, update reporting, coverage and strict freshness validation.

Source pin:

- Repository: `https://github.com/diegosouzapw/OmniRoute`
- Package/release version: `v3.8.48`
- Annotated tag object: `4f00f84b5a12f90fca2f1d72a60404cf6f5bf059`
- Immutable source commit: `7ee5bbc64dbb03e967521227f2afffeb7c9dad1e`
- Source tree: `4048504f76c6fb3dedd00ff2aa7250109308de99`
- Skills tree: `e7b1871e0904fbdb0ff01bdc3fc1d7ea599707ff`
- Commit date: `2026-07-13T21:18:54Z`
- Upstream skill count: `44`

Rows listing both proxy and admin have a mixed read/write surface: proxy owns discovery and admin owns mutation. `omni-github-skills` discovery is read-only; any import is handed to LocalSetup vetting/import/sandbox policy before admin mutation.

## Coverage Map

| Upstream skill | LocalSetup coverage |
|---|---|
| `cli-a2a` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-backup-sync` | `ls-omniroute-admin-automation` |
| `cli-batches` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-chat` | `ls-omniroute-model-discovery` |
| `cli-compression` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-contexts` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-cost-usage` | `ls-omniroute-model-discovery` |
| `cli-eval` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-health` | `ls-omniroute-model-discovery` |
| `cli-keys` | `ls-omniroute-admin-automation` |
| `cli-mcp` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-models` | `ls-omniroute-model-discovery` |
| `cli-plugins-skills` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-policy-audit` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-providers` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-resilience` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-routing` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `cli-serve` | `ls-omniroute-task-triage`, `ls-omniroute-admin-automation` |
| `cli-setup` | `ls-omniroute-task-triage`, `ls-omniroute-admin-automation` |
| `cli-tunnel` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `config-codex-cli` | `ls-omniroute-task-triage`, `ls-omniroute-admin-automation` |
| `omni-agents-a2a` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-api-keys` | `ls-omniroute-admin-automation` |
| `omni-auth` | `ls-omniroute-task-triage`, `ls-omniroute-admin-automation` |
| `omni-budget` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-cache` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-cli-tools` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-combos-routing` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-compression` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-context-rtk` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-db-backups` | `ls-omniroute-admin-automation` |
| `omni-github-skills` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-inference` | `ls-omniroute-model-discovery` |
| `omni-mcp` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-models` | `ls-omniroute-model-discovery` |
| `omni-providers` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-proxies` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-resilience` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-settings` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-sync-cloud` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-tunnels` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |
| `omni-usage-logs` | `ls-omniroute-model-discovery` |
| `omni-version-manager` | `ls-omniroute-admin-automation`, `ls-omniroute-task-triage` |
| `omni-webhooks` | `ls-omniroute-model-discovery`, `ls-omniroute-admin-automation` |

## Maintenance Rule

When updating OmniRoute, refresh this coverage map and the `NATIVE_COVERAGE` mapping in `../../ls-omniroute-skills-update/scripts/omniroute_update.py` from the pinned upstream `skills/*/SKILL.md` inventory before changing pack membership. Keep the four-owner boundary unless a future upstream skill introduces a functional area that none of these surfaces can safely own.
