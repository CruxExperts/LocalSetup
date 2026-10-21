# Linux Patcher Overview

Linux Patcher is a plan-only maintenance helper for Linux package updates and Docker Compose refreshes. It is designed for agents and operators that need a safe, auditable plan before touching production hosts.

## What Ships

- `scripts/patch_cli.py`: Python CLI that emits markdown or JSON plans.
- `SKILL.md`: concise operating guidance.
- `references/setup.md`: secure setup checklist.
- `references/workflows.md`: current plan-only workflows.
- `references/patchmon-setup.md`: PatchMon dashboard notes and API boundary.
- `references/patchmon-security-and-distributions.md`: security and distro support notes.

The skill does not ship shell helper wrappers. Do not document or call shell entrypoints unless new files are added and tested.

## Commands

Run from the skill directory:

```bash
python scripts/patch_cli.py status
python scripts/patch_cli.py auto --dry-run
python scripts/patch_cli.py host-only admin@webserver.example.com
python scripts/patch_cli.py host-full admin@app.example.com /opt/docker
python scripts/patch_cli.py multiple ./hosts.conf
```

Use `--json` before the subcommand when another tool needs structured output:

```bash
python scripts/patch_cli.py --json status
python scripts/patch_cli.py --json multiple ./hosts.conf
```

## Current Boundaries

| Area | Status |
|------|--------|
| Plan generation | Available |
| Host validation | Basic CLI validation only |
| PatchMon API querying | Unavailable |
| SSH execution | Unavailable |
| Package or Docker execution | Unavailable |
| Parallel updates | Unavailable |

`auto` is a compatibility and guidance mode. It explains the inputs a future PatchMon integration would need, then points to the manual plan modes.

## Supported Distribution Guidance

The generated commands are generic and must be reviewed for the target distribution before use.

| Distribution family | Package manager | Notes |
|---------------------|-----------------|-------|
| Ubuntu, Debian | `apt` | Ubuntu is the best-tested family. |
| RHEL, AlmaLinux, Rocky Linux, CentOS, Amazon Linux | `dnf` or `yum` | Confirm the correct tool for the major version. |
| SUSE, openSUSE | `zypper` | Review local update policy before use. |

## Typical Use

1. Ask for status:
   ```bash
   python scripts/patch_cli.py status
   ```
2. Generate a plan:
   ```bash
   python scripts/patch_cli.py host-full admin@app.example.com /opt/docker
   ```
3. Review commands and risk with the service owner.
4. Run approved commands manually through SSH or a trusted terminal workflow.
5. Verify package logs, service health, and reboot requirements.

## Security Posture

- Use dedicated SSH keys and a dedicated maintenance user.
- Grant only the exact sudo commands needed for your host.
- Avoid unrestricted passwordless sudo.
- Store PatchMon credentials only in your platform secret store or a local gitignored file if you later add a tested client.
- Keep rollback and backups ready before running generated commands.
