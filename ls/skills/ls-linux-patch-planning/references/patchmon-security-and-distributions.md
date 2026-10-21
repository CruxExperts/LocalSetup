# PatchMon, Security, and Distributions

## PatchMon Boundary

PatchMon can provide a dashboard, package status, update history, and scheduled jobs. The bundled `patch_cli.py` does not query PatchMon, read credentials, or trigger PatchMon jobs.

Use:

```bash
python scripts/patch_cli.py auto --dry-run
```

to get a guidance-only plan that explains the unavailable automatic mode and points to manual plan generation.

## Security Considerations

- Use SSH keys, not passwords.
- Prefer a dedicated maintenance user such as `patchbot`.
- Grant only specific package and Docker commands in sudoers.
- Avoid unrestricted passwordless sudo.
- Review generated commands before running them.
- Schedule updates during maintenance windows.
- Keep Docker Compose files and rollback notes in version control.
- Do not store PatchMon credentials in tracked files.

## Reboot Management

The skill does not reboot hosts. Check and schedule reboots separately:

```bash
ssh user@host '[ -f /var/run/reboot-required ] && echo reboot-required || echo no-reboot-flag'
```

For non-Debian systems, use the distribution's standard reboot-required signal or kernel comparison process.

## Distribution Notes

| Distribution family | Package manager | Review points |
|---------------------|-----------------|---------------|
| Ubuntu, Debian | `apt` | Confirm `apt` path and reboot-required handling. |
| RHEL, AlmaLinux, Rocky Linux, CentOS, Amazon Linux | `dnf` or `yum` | Confirm major version and log path. |
| SUSE, openSUSE | `zypper` | Confirm update policy and reboot checks. |

The CLI validates local arguments only. It does not detect remote OS details or choose exact package commands.

## Documentation Files

- `SKILL.md`: primary operating guide.
- `references/setup.md`: secure setup and validation.
- `references/workflows.md`: plan-only workflows.
- `references/patchmon-setup.md`: dashboard setup notes and API boundary.
