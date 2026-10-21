"""Constants for tmux terminal mode."""

from __future__ import annotations

TOOL_VERSION = "1.0.0"
SENTINEL_BEGIN = "# BEGIN tmux-default-terminal-mode"
SENTINEL_END = "# END tmux-default-terminal-mode"
BAK_SUFFIX = ".tmux-mode.bak"

DEFAULT_SESSION = "ops"
DEFAULT_MODE = "ide"
DEFAULT_RULES_FILE = ".cursor/rules/operator-rules.mdc"

IDE_SETTINGS_CANDIDATES = [
    "~/.cursor-server/data/Machine/settings.json",
    "~/.vscode-server/data/Machine/settings.json",
    "~/Library/Application Support/Cursor/User/settings.json",
    "~/Library/Application Support/Code/User/settings.json",
]

AGENT_RULE_BLOCK = """\
{sentinel_begin}
## MANDATORY: Server/ops (tmux + sudo gate)

Any request that involves running commands on the host uses the tmux workflow.
**Sudo is always assumed required.** Use the **tmux_ops** tool; do not infer
session from `tmux ls` or parse capture yourself.
Automatically load **ls-wf-sudo-tmux-session** before any command or
retry that requests sudo, root/admin access, elevation, or a pseudo-terminal
for privileged work. Treat a transcribed request for "pseudo" as a sudo
trigger when the command needs privilege. Do not wait for the user to remind
you or bypass the workflow through direct SSH or raw tmux commands.

1. **Session.** Run `./ls/tools/tmux_ops pick`. Use the returned
   `session`. Right away, show the join command in a copy-paste code block:
   the returned `attach_command`. Do not wait for the user to confirm they
   joined. Then run the probe.

2. **Gate.** Run `./ls/tools/tmux_ops probe -t <session>`. The probe checks
   noninteractive sudo first, then reads `<hostname.upper()>_SUDO_KEY` from
   the target pane and `GENERIC_SUDO_KEY` when the first value is absent,
   empty, or definitely rejected. Hostname dots and hyphens stay intact.
   Values travel only on private stdin to fixed `sudo -S -v -p ''`; output
   records only the variable name and candidate status. If the response has
   `"action_required": true` or `"sudo": "password_required"`, stop. Tell the
   user to attach with the returned `attach_command`, run `sudo -v` in that exact tmux pane,
   enter the password, and reply "sudo ready". Wait for that reply before
   probing again. A timeout, ambiguous outcome, or policy/MFA
   failure must not advance or replay credentials. If `"sudo": "ready"`, use
   `credential_outcome` and `credential_variable` to report whether readiness
   was cached or which variable was used; null means no variable was used.
   Managed `run` and `status` results carry the same secret-free provenance.

3. **Run.** One logical step per managed run:
   `./ls/tools/tmux_ops run -t <session> -- <cmd>`. Read the returned
   `tail` and `log_path`. If status is `"running"`, continue with
   `status -t <session> --run-id <run_id> --wait --timeout <secs>` or interrupt
   only with `cancel -t <session> --run-id <run_id>`. If sudo expires, probe
   again.

Full procedure: **ls-wf-sudo-tmux-session** and
**ls-workflow-tmux-terminal-mode** workflow packages.
{sentinel_end}
""".format(sentinel_begin=SENTINEL_BEGIN, sentinel_end=SENTINEL_END)

SHELL_BLOCK_TEMPLATE = """\
{sentinel_begin}
# Auto-attach to tmux session if not already inside tmux.
# Applies to interactive non-tmux shells only (SSH, local terminal, etc).
if [ -z "$TMUX" ] && [ -n "$PS1" ]; then
  exec tmux new-session -A -s {session}
fi
{sentinel_end}
"""
