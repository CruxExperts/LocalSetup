# PatchMon Setup Notes

PatchMon is optional for this skill. It can be useful as a dashboard for package status, reboot requirements, and scheduled jobs, but the shipped Linux Patcher helper does not integrate with its API.

## Current Integration Status

| Capability | Status |
|------------|--------|
| PatchMon dashboard documentation | Informational |
| PatchMon API querying from `patch_cli.py` | Unavailable |
| Reading PatchMon credentials | Unavailable |
| Automatic host selection | Unavailable |
| Triggering PatchMon jobs | Unavailable |

Use `python scripts/patch_cli.py auto --dry-run` to show this boundary from the CLI.

## If You Run PatchMon Separately

Follow PatchMon's current upstream documentation for installation and upgrades. Keep these operational rules:

- Run PatchMon on a host reachable from your control machine.
- Use HTTPS and firewall restrictions for production access.
- Store secrets in a secret manager or local gitignored files.
- Rotate database, Redis, JWT, and API credentials according to your policy.
- Keep PatchMon agents updated on monitored hosts.

## Using PatchMon Data Manually

Until a tested Python client exists, export or review host data from PatchMon manually, then create a local `hosts.conf` for plan generation:

```text
patchbot@webserver.example.com
patchbot@app.example.com,/opt/docker
```

Then run:

```bash
python scripts/patch_cli.py multiple hosts.conf
```

## Requirements for a Future API Client

Before documenting live PatchMon automation, add a Python implementation that:

- Uses `requests` through the framework dependency policy.
- Reads credentials from a documented, gitignored source or platform secret store.
- Validates URLs, timeouts, response schemas, and host identifiers.
- Emits actionable stderr on authentication, network, and schema failures.
- Has tests for dry-run, API errors, empty host lists, and unsafe host data.
- Updates `SKILL.md`, `references/setup.md`, and `references/workflows.md` in the same change.

Do not add shell wrappers for PatchMon automation.
