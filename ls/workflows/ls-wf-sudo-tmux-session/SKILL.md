---
name: ls-wf-sudo-tmux-session
description: Automatically route sudo, root/admin elevation, require_escalated, pseudo-terminal/PTY handling, interactive sudo prompts, or managed tmux run tracking through the approved tmux workflow.
metadata:
  version: "1.0"
---

Use this workflow package for server or remote ops through tmux wrappers.
Primary source references: `ls/docs/ops/tmux-ops-managed.md` and `ls/docs/ops/tmux-ops-remote.md`. Installed packages expose the same references through the `localsetup://doc/ops/tmux-ops-managed.md` and `localsetup://doc/ops/tmux-ops-remote.md` aliases.

Load this workflow automatically before any command or retry that requests `sudo`, root/admin access, elevation, `require_escalated`, or a pseudo-terminal for privileged work. Treat a transcribed request for “pseudo” as a sudo trigger when the command needs privilege. Do not wait for a reminder, switch to direct SSH or raw tmux commands, or run an unprivileged-looking retry that still invokes sudo.

## When to use

Use this workflow when a command needs `sudo`, root/admin elevation, `require_escalated`, pseudo-terminal/PTY behavior, interactive sudo or elevated terminal password prompt handoff, or durable managed tmux run tracking through `ls/tools/tmux_ops`. Installed packages expose that tool through the `localsetup://tool/tmux_ops` alias.

Do not use this workflow for normal repo-local coding, API authentication, token setup, non-privileged file edits, or commands that can run safely without managed terminal state.

## Execution authorization boundary

Tmux is only the elevated or PTY transport. Session selection, a successful probe, an attached operator, `sudo ready`, and cached sudo credentials do not authorize a command.

Before every managed `run`, require one of these records:

1. **Verified guarded handoff:** Accept a record from `ls-wf-risky-operations-safety-approval` only when it contains the exact command or edit and values, exact target, risk classification, likely consequences and affected scope, backup or no-backup decision, rollback action, and the user's immediate explicit approval. Verify that every recorded value still matches the pending payload.
2. **Direct safety gate:** Apply `ls-safety-and-backup` directly and record the same exact command or edit and values, target, risk classification, consequences and affected scope, backup or no-backup decision, and rollback action. Then show that frozen payload to the user and obtain immediate explicit approval for it before execution.

Reject a missing, incomplete, stale, or changed record. Return to the applicable approval workflow and do not run the command. Each managed `run` needs its own still-matching record; approval for one payload does not authorize another.

## Sequence

1. Establish the execution authorization record above for the exact pending payload.
2. Run `./ls/tools/tmux_ops pick` and show the returned attach command.
3. Run `./ls/tools/tmux_ops probe -t <session>`.
   - The probe checks noninteractive sudo first. If it needs a password, it checks the exact target-host variable `<hostname.upper()>_SUDO_KEY` and then `GENERIC_SUDO_KEY` from inside the target pane. Uppercasing preserves hostname dots and hyphens. It never forwards a local SSH environment value to the remote host.
   - The response reports `credential_outcome`, `credential_variable` (null when no variable was used), `credential_candidates` (variable names and secret-free statuses), and `credential_candidates_checked`. A cached/noninteractive success reports `cached` and does not claim that environment variables were checked.
   - If the response includes `"action_required": true` or `"sudo": "password_required"`, stop. When no usable key is available or a key is definitely rejected, tell the user to attach with the returned `attach_command`, run `sudo -v` in that exact tmux pane, enter the password, then reply `sudo ready`. Wait for the user to say `sudo ready` before probing again.
   - If the probe reports a timeout, ambiguous result, or policy/MFA failure, do not advance to another credential or start a fresh auth attempt. Let the same operation reconcile; if it remains blocked, report the safe detail and return control to the user.
4. Reconfirm that the authorized payload still exactly matches, then run it with `./ls/tools/tmux_ops run -t <session> -- <cmd>`.
5. Track long commands with `./ls/tools/tmux_ops status -t <session> --run-id <id>`.
6. Interrupt only with `./ls/tools/tmux_ops cancel -t <session> --run-id <id>`.

Use the returned `run_id`, `status`, `tail`, and `log_path` as evidence.

## Sudo keepalive

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
