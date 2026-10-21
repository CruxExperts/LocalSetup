---
status: ACTIVE
version: 3.4
---

# Codex Heartbeat

This repository has the LocalSetup Codex heartbeat harness initialized.

The harness is opt-in. It does not run until `config/codex_heartbeat.yaml` has `heartbeat.enabled: true` and a scheduled or manual command invokes `localsetup harness codex-heartbeat run`.

## Runtime

- Config: `config/codex_heartbeat.yaml`
- State: `.localsetup/state/codex-heartbeat/`
- Cron manifest: `cron/manifest.yaml`

Runtime artifacts are local repository state. Keep `.localsetup/state/codex-heartbeat/` ignored and preserve it when investigating interrupted runs.

## Manual Checks

```bash
localsetup harness codex-heartbeat status
localsetup harness codex-heartbeat run --no-agent
```
