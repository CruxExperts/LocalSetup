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
## MANDATORY: Managed terminal and sudo workflow

Automatically load **ls-wf-sudo-tmux-session** before a command or retry that
needs sudo, root/admin privileges, elevation, a pseudo-terminal for privileged
work, or managed terminal tracking. Treat a transcribed request for "pseudo"
as a sudo trigger when the command needs privilege. Keep command authority
bound to the user's exact request and target.

1. **Select.** Run `./ls/tools/tmux_ops pick` from the actual controller pane.
   Use the exact returned target, parent and status handles. The normal path
   uses the controller's owned child pane; legacy sessions require the explicit
   `--legacy-session` compatibility option.
2. **Authenticate.** Direct sudo argv uses the operation-bound shared sudo
   broker. It checks cached readiness, then resolves only `GENERIC_SUDO_KEY`
   from the controller environment and EnvMan when that variable is absent.
   Explicit empty or invalid values remain authoritative. Credentials stay in
   the private framed channel, workload stdin is preserved, and every
   `*_SUDO_KEY` is removed from workload environments. Automatic authentication
   has a separate 120-second default budget (`run --auth-timeout <secs>`).
   A manual fallback is allowed only when the run result explicitly says
   `action_required` and includes `sudo.manual_fallback`, proving the workload
   did not start. Run that exact quoted command once in the qualified pane.
   For timeout, policy denial, owner change, `running`, or `unknown`, use
   read-only status reconciliation only; never replay. Do not run a separate
   `sudo -v`.
3. **Run and observe.** Run one authorized logical step per invocation:
   `./ls/tools/tmux_ops run -t <target> -- <argv...>`. A slow workload remains
   running when the observation timeout expires. Save its returned
   `status_handle`, then use
   `./ls/tools/tmux_ops status --status-handle <handle> --wait --timeout <secs>`
   to read its durable receipt, including after a directory change or controller
   restart. Completion requires a receipt; acknowledgement alone is not proof.

Use `./ls/tools/tmux_ops capabilities` to inspect the local API and helper
capabilities. Probe results identify stale or incompatible remote helper
versions separately from sudo policy or credential failures.

Full procedure: **ls-wf-sudo-tmux-session** and
**ls-wf-tmux-terminal-mode** workflow packages.
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
