---
name: ls-wf-sudo-tmux-session
description: Automatically route sudo, root/admin elevation, require_escalated, pseudo-terminal/PTY handling, interactive sudo prompts, or managed tmux run tracking through the approved tmux workflow.
metadata:
  version: "1.1"
---

Use this workflow package for server or remote ops through tmux wrappers.
Primary source references: `ls/docs/ops/tmux-ops-managed.md` and `ls/docs/ops/tmux-ops-remote.md`. Installed packages expose the same references through the `localsetup://doc/ops/tmux-ops-managed.md` and `localsetup://doc/ops/tmux-ops-remote.md` aliases.

Load this workflow automatically before any command or retry that requests `sudo`, root/admin access, elevation, `require_escalated`, or a pseudo-terminal for privileged work. Treat a transcribed request for “pseudo” as a sudo trigger when the command needs privilege. Do not wait for a reminder, switch to direct SSH or raw tmux commands, or run an unprivileged-looking retry that still invokes sudo.

## When to use

Use this workflow when a command needs `sudo`, root/admin elevation, `require_escalated`, pseudo-terminal/PTY behavior, interactive sudo or elevated terminal password prompt handoff, or durable managed tmux run tracking through `ls/tools/tmux_ops`. Installed packages expose that tool through the `localsetup://tool/tmux_ops` alias.

Do not use this workflow for normal repo-local coding, API authentication, token setup, non-privileged file edits, or commands that can run safely without managed terminal state.

## Execution authorization boundary

Terminal and credential readiness do not authorize a command. Reuse the current
user-approved task scope and its exact target/effect checks. Routine necessary
work does not need another approval merely because sudo is required. Destructive
work retains the owning recovery plan and explicit scope approval. Never expand
command authority from a credential variable, a successful probe or a pane name.

## Managed pane sequence

1. Resolve the actual caller, canonical repository, exact target and live owner.
   Use the existing private task state root. Normal `pick` requires verified tmux
   caller context and does not create a separate operations session or tab.
2. Run `./ls/tools/tmux_ops pick`, supplying `--repository`, `--host-alias`,
   `--cwd` or `--state-dir` only when needed to bind the selected context. Retain
   the returned exact `target`, parent and ownership handles.
3. Reuse the controller-owned child directly below that parent. The root name is
   `<repo>-sudo`; an admitted host uses `<repo>-<exact-alias>-sudo`. Different
   controllers may have identical names. Ownership and native IDs distinguish
   them. The child occupies one third of the parent's original height without
   changing focus.
4. Run exact argv with `./ls/tools/tmux_ops run -t <target> -- <argv...>`.
   The managed Bash handler checks its primary prompt, empty Readline buffer,
   machine/TTY/shell generation and writer lease before executing a staged
   request. The adapter sends only the reserved trigger, never command text and
   Enter. Partial input, continuation prompts and unsupported shells stop input.
5. Direct privileged argv automatically uses the shared operation-bound sudo
   owner. It checks sudo availability and supplies the exact target key followed
   by the generic key when needed, resolving controller environment before
   EnvMan. Values travel only through private transient input. Workload children
   do not inherit `*_SUDO_KEY` variables.
6. Authentication and the exact authorized command share one `sudo -A`
   invocation; command stdin is preserved. Cached validation is an observation,
   never a guarantee for a separate invocation. Generic fallback after a rejected
   first value requires an explicitly qualified single-password conversation;
   unknown prompts or attempt exhaustion stop automatic attempts.
7. Track the returned operation/run ID through `status`. A completion receipt
   proves completion; dispatch acknowledgement does not. `busy` receives no new
   command, and `unknown` permits read-only reconciliation only. Never switch
   backends or repeat an uncertain request.

Herdr and tmux use the shared LocalSetup terminal core. FleetOps owns its exact
host admission, authority and receipts. Select a qualified SSH/tmux fallback
before dispatch when a Herdr endpoint is unavailable. A wrong-machine attachment
may detach or log out automatically only when its recorded ownership and idle
state are proven; verify the original local shell before changing directory or
connecting elsewhere.

Missing credentials and unsupported authentication return `action_required`
with a concrete remedy. The agent handles that error through the owning
credential/manual workflow. Do not type a password into command text, print it,
write it to a file or treat `sudo ready` as an execution grant. A generic
`sudo -v` does not repair zero-timeout or process-specific authentication for a
new invocation.

The old standalone `ops*` session workflow is explicit compatibility only:
`pick --legacy-session` and `run --legacy-session -t <session>`. It is not the
normal automatic path. Do not infer legacy lifecycle from a binary override.

## Legacy sudo keepalive

For active privileged work where the user has already entered sudo in the same managed pane, use bounded one-shot keepalive commands instead of ad hoc loops.

1. Request a marker only after `probe` reports sudo `ready`:

   ```bash
   ./ls/tools/tmux_ops keepalive request -t <session> --owner <id> --ttl-seconds 7200 --max-refreshes 24 --reason "<reason>"
   ```

2. Refresh only as an explicit one-shot action while privileged work remains active and the managed pane is idle between commands:

   ```bash
   ./ls/tools/tmux_ops keepalive refresh -t <session>
   ```

3. Report state with `./ls/tools/tmux_ops keepalive status -t <session>`.
4. Clear the marker with `./ls/tools/tmux_ops keepalive clear -t <session> --owner <id>` when privileged work ends.

Keepalive is restricted to managed `ops*` sessions, caps TTL at 7200 seconds and refreshes at 24, and runs only hard-coded `sudo -n -v` during refresh. Its request path performs noninteractive checks only and never starts environment-key or password authentication. It must not start a background loop, prompt for a password, or refresh while a managed run is active, another refresh is in progress, or the pane is not idle.

Load the full procedure from **ls-wf-sudo-tmux-session** and **ls-wf-tmux-terminal-mode** before privileged commands. Environment credential presence, a successful probe, an attached operator, `sudo ready`, or cached sudo state confirms transport readiness only; it never authorizes a command.
