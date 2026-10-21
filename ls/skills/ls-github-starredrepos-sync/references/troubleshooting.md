# Troubleshooting

| Symptom | Likely cause | Next step |
|---|---|---|
| `gh` not found | GitHub CLI is not installed or not on PATH | Install GitHub CLI and rerun auth verification |
| Not authenticated | `gh auth login` has not been run for the target host | Run `gh auth login --hostname <host>` |
| No `starred_at` field | Missing star timestamp media type | Use the script defaults or set the correct Accept header |
| `--slurp` unavailable | Local GitHub CLI is older than current docs | Use these scripts; they do not depend on `--slurp` |
| Remote creation fails | Repository exists or token lacks scope | Verify `OWNER/starredrepos` and token permissions |
| Scout command hangs | External command is slow or waiting for input | Set `STARREDREPOS_SCOUT_TIMEOUT_MS` and require JSON stdin/stdout |
