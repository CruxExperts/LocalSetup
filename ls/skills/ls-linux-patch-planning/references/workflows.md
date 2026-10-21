# Linux Patcher Workflows

The current workflow is plan-only. Every path ends with a reviewed plan; no shipped command applies updates.

## Capability Check

```text
operator or agent
  -> python scripts/patch_cli.py status
  -> review available modes
  -> confirm unavailable live execution features
```

Use this first when automation needs to decide whether it can safely continue.

## Host-Only Plan

```text
operator or agent
  -> python scripts/patch_cli.py host-only user@host
  -> CLI validates host text
  -> CLI emits package preflight, package update placeholder, reboot check
  -> operator reviews and runs approved commands manually
  -> operator verifies logs and reboot status
```

The helper does not detect the remote distribution. The generated package command is intentionally a placeholder that points back to `SKILL.md` and local policy.

## Host-Full Plan

```text
operator or agent
  -> python scripts/patch_cli.py host-full user@host /absolute/docker/path
  -> CLI validates host and Docker path text
  -> CLI emits host-only steps
  -> CLI adds Docker directory, pull, up, and ps checks
  -> operator reviews service impact and runs approved commands manually
```

Use this only during a maintenance window. Docker Compose refreshes can restart services.

## Multiple Host Plan

Input file:

```text
# host or host,/absolute/docker/path
patchbot@webserver.example.com
patchbot@app.example.com,/opt/docker
```

Flow:

```text
operator or agent
  -> python scripts/patch_cli.py multiple hosts.conf
  -> CLI validates each row
  -> CLI emits one combined markdown or JSON plan
  -> operator batches work manually according to risk and maintenance windows
```

The helper does not run hosts in parallel. Stagger production updates unless the service owner explicitly approves a batch.

## Automatic/PatchMon Boundary

```text
operator or agent
  -> python scripts/patch_cli.py auto --dry-run
  -> CLI reports PatchMon automatic execution is unavailable
  -> CLI lists required future inputs
  -> operator uses host-only, host-full, or multiple instead
```

`--dry-run` is accepted for compatibility. Because all modes are plan-only, it does not change behavior.

## JSON Workflow

```bash
python scripts/patch_cli.py --json status
python scripts/patch_cli.py --json multiple hosts.conf
```

Use JSON when another tool needs to inspect `mode`, `steps`, or unavailable capabilities.

## Manual Execution Checklist

Before running any generated command:

1. Confirm host identity and environment.
2. Confirm backups and rollback path.
3. Confirm maintenance window and owner approval.
4. Confirm sudo policy is narrow and validated.
5. Run one low-risk host first.
6. Check package logs, container health, and reboot requirements.

## Failure Handling

If a generated command fails during manual execution:

1. Stop on that host.
2. Capture stdout, stderr, exit status, and package logs.
3. Do not continue to a larger batch until the failure is understood.
4. Use rollback or service recovery procedures if health checks fail.
5. Update the host list or command plan before retrying.
