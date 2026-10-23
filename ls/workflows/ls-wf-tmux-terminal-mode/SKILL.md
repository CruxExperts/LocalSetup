---
name: ls-wf-tmux-terminal-mode
description: Use for observable agent shell sessions, tmux availability, command recovery, application profiles, or terminal-mode setup; privileged commands retain the sudo workflow.
metadata:
  version: "1.1"
---

Use this workflow for observable ordinary commands and tmux configuration. Native
permissions and sandboxing still govern execution. For `sudo`, elevated
permission, or privileged interactive handoff, load `ls-wf-sudo-tmux-session`;
its controller-owned lower pane remains a separate protected lifecycle.

## Observable agent commands

1. Inspect `localsetup shell --help` and its capability result. Missing tmux is a
   distinct unavailable state: optionally suggest installation for observability,
   without installing it or bypassing native execution restrictions.
2. Ensure an agent-owned session through the shell API. Use its returned handle,
   machine identity and verified created/joined result. Names are labels, not
   ownership proof. Announce "Created session <name> on <machine>" or "Joined
   session <name> on <machine>" only after that result.
   Supply the long-lived native agent PID and process start time from verified
   ancestry, never a transient CLI/helper identity. Unknown ownership permits
   observation rather than mutation.
3. Show the returned local join command alongside the safely displayed workload
   and repeat it in periodic progress. State which machine it runs on; never
   prepend SSH or assume the user is connected. Attachment observes existing work
   in a read-only interactive terminal and must not create a replacement or
   replay an operation.
4. Run each ordinary command in a fresh owned shell. Preserve operation IDs and
   status handles. A wait timeout or lost acknowledgement is not failure proof:
   reconcile the existing operation and inspect bounded output instead of retrying.
5. Read the newest 200 lines by default. Request another positive line limit or
   the current screen explicitly with `screen`; `view` reads captured output.
   Respect byte caps, cursors and truncation gaps.
6. Before normal exit, close eligible owned resources and report verified cleanup
   or `cleanup pending`. Preserve live silent work and human observers. Disconnect
   alone never proves abandonment; uncertain identity permits observation only.
   Report retained collector receipts on the next startup after a crash.

For a user-authorized durable collector, use `shell schedule install` with an
explicit grant ID and qualified enrollment owner. Inspect `schedule status` and
its run receipt; preserve unrelated crontab entries. The fixed every-minute job
continues after the enrolling agent exits, skips overlap and stops on source or
runtime drift. Reboot requires an explicit same-host rebind. `schedule disable`
revokes future dispatch; do not silently renew changed authority.

## Application profiles

Use `localsetup app-profile --help` to inspect, save, resolve and apply named JSON
preferences. Framework defaults and private user overrides have separate owners;
inspect effective values, origins and digest before native changes. The tmux
agent-shell default enables mouse support, bottom session/window status, 20,000
history lines and 200-line bounded views while preserving inherited colors,
theme, terminal features and key bindings. Never rewrite unrelated user sessions
or global tmux configuration. Active sessions retain their resolved settings;
apply a changed session profile explicitly while idle.
Changing pane history capacity requires a new session; inspect native readback
instead of treating an option value as proof that an existing pane was resized.

The older terminal-mode setup still controls whether new IDE terminals or shell
sessions launch inside tmux. It is not the ordinary-command process supervisor.

Checkout references: [TMUX_TERMINAL_MODE.md](../../docs/TMUX_TERMINAL_MODE.md)
and [tmux_terminal_mode](../../tools/tmux_terminal_mode).
For installed use, resolve `localsetup://doc/TMUX_TERMINAL_MODE.md` and
`localsetup://tool/tmux_terminal_mode`.
