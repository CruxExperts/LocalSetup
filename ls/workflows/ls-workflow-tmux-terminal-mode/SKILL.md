---
name: ls-workflow-tmux-terminal-mode
description: Use when enabling, disabling, defaulting, or checking tmux terminal mode; do not use for one-off sudo or interactive password handoff.
metadata:
  version: "1.0"
---

Use this workflow package for tmux terminal mode setup, defaults, and checks. It manages whether terminal sessions are launched under tmux by default; for one-off `sudo`, elevated permission, PTY, or interactive password handoff, use `ls-wf-sudo-tmux-session`.

Checkout references: [TMUX_TERMINAL_MODE.md](../../docs/TMUX_TERMINAL_MODE.md)
and [tmux_terminal_mode](../../tools/tmux_terminal_mode).
For installed use, resolve `localsetup://doc/TMUX_TERMINAL_MODE.md` and
`localsetup://tool/tmux_terminal_mode`.
