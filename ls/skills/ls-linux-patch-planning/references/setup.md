# Linux Patcher Setup

This setup guide reflects the current implementation: `scripts/patch_cli.py` generates plans only. It does not execute SSH, package manager, Docker, or PatchMon API operations.

## Prerequisites

- Python 3.12+ on the control machine.
- SSH client on the control machine if you intend to run generated commands.
- SSH key access to each target host.
- A maintenance window, backup plan, and rollback path.
- Target-host package manager: `apt`, `dnf`, `yum`, or `zypper`.
- Docker and Docker Compose only for `host-full` plans.

PatchMon is optional for dashboard visibility. The bundled helper does not read PatchMon credentials or query its API.

## Install Location

In LocalSetup, the canonical source path is:

```text
ls/skills/ls-linux-patch-planning/
```

Generated platform adapter paths may differ. Run the commands from wherever your installed skill directory contains `scripts/patch_cli.py`.

## Configure SSH

Create a dedicated key for the maintenance user when possible:

```bash
ssh-keygen -t ed25519 -C "linux-patcher-maintenance" -f ~/.ssh/id_linux_patcher
ssh-copy-id -i ~/.ssh/id_linux_patcher.pub patchbot@targethost.example.com
ssh -i ~/.ssh/id_linux_patcher patchbot@targethost.example.com echo "SSH OK"
```

Optional SSH config:

```sshconfig
Host webserver-maint
    HostName webserver.example.com
    User patchbot
    IdentityFile ~/.ssh/id_linux_patcher
```

## Configure Sudo Safely

Passwordless sudo is sensitive. Do not use broad examples as final policy. Generate a plan first, inspect the exact commands, then grant only the approved command paths for a dedicated user.

Verify the executable path on the target host, then select only its matching readiness command. Sudoers rules with arguments are exact: permission for one row does not grant a different manager or operation.

| Package manager | Readiness command listed by the generated preflight |
| --- | --- |
| `apt` | `<resolved-path>/apt update` |
| `dnf` | `<resolved-path>/dnf check-update` |
| `yum` | `<resolved-path>/yum check-update` |
| `zypper` | `<resolved-path>/zypper list-updates` |

Example Ubuntu/Debian pattern:

```sudoers
# /etc/sudoers.d/linux-patcher
patchbot ALL=(root) NOPASSWD: /usr/bin/apt update
patchbot ALL=(root) NOPASSWD: /usr/bin/apt upgrade
patchbot ALL=(root) NOPASSWD: /usr/bin/apt autoremove
```

Example Docker additions only when needed:

```sudoers
patchbot ALL=(root) NOPASSWD: /usr/bin/docker compose pull
patchbot ALL=(root) NOPASSWD: /usr/bin/docker compose up
patchbot ALL=(root) NOPASSWD: /usr/bin/docker compose ps
```

Validate the file:

```bash
sudo chmod 0440 /etc/sudoers.d/linux-patcher
sudo visudo -c -f /etc/sudoers.d/linux-patcher
ssh patchbot@targethost.example.com 'sudo -n -l -- /usr/bin/apt update'
```

`sudo -n -l` asks whether the exact path and arguments are authorized; it does not execute the package manager. If the listing fails, correct that exact executable/argument rule or the noninteractive listing policy before the maintenance window. A successful listing does not authorize later upgrade, autoremove, or Docker commands and does not replace backup, rollback, or service-readiness checks.

## Check CLI Status

```bash
python scripts/patch_cli.py status
python scripts/patch_cli.py --json status
```

Expected result: `Mode: plan-only` and a list of unavailable live-execution features.

## Generate Plans

Single host, packages only:

```bash
python scripts/patch_cli.py host-only patchbot@webserver.example.com
```

Single host with Docker Compose:

```bash
python scripts/patch_cli.py host-full patchbot@app.example.com /opt/docker
```

Multiple hosts:

```text
# hosts.conf
patchbot@webserver.example.com
patchbot@app.example.com,/opt/docker
```

```bash
python scripts/patch_cli.py multiple hosts.conf
```

Automatic/PatchMon mode boundary:

```bash
python scripts/patch_cli.py auto --dry-run
```

This reports that PatchMon automatic execution is unavailable and guidance-only. It will not query PatchMon or run updates.

## Verify After Manual Updates

After running approved commands manually:

```bash
# Ubuntu/Debian
ssh patchbot@host 'tail -100 /var/log/apt/history.log'

# RHEL-family
ssh patchbot@host 'tail -100 /var/log/dnf.log 2>/dev/null || tail -100 /var/log/yum.log'

# Reboot check
ssh patchbot@host '[ -f /var/run/reboot-required ] && echo reboot-required || echo no-reboot-flag'

# Docker
ssh patchbot@host 'cd /opt/docker && sudo docker compose ps'
```

## Troubleshooting

### Invalid Host Input

`patch_cli.py` rejects shell operators and control characters in host arguments. Use `user@host`, host aliases from SSH config, or `host:port`.

### Docker Path Rejected

`host-full` requires an absolute remote path:

```bash
python scripts/patch_cli.py host-full patchbot@app.example.com /opt/docker
```

### Sudo Still Prompts

Check the sudoers file and command paths:

```bash
ssh patchbot@host 'sudo -l'
ssh patchbot@host 'command -v apt dnf yum zypper docker'
```

### PatchMon Automation Needed

Add and test a Python PatchMon API client before documenting credentials, live API queries, or automatic host selection. Until then, use `multiple` with a reviewed local host list.
