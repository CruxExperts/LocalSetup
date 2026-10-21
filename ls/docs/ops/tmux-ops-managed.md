---
status: ACTIVE
version: 4.55
owner_package: ls-wf-sudo-tmux-session
---

# Sudo tmux managed workflow

**Purpose:** Explain the managed `tmux_ops` implementation for humans and give agents a deterministic command flow for host operations.

Managed tmux ops keeps host commands visible to the operator, resumable after agent disconnects, and safe around sudo prompts. The normal path is:

```text
pick -> probe -> run -> status
```

`send` and `wait` still exist for older callers, but new agent instructions should use `run` for commands and `status` for follow-up.

## What changed

The managed implementation replaces prompt-regex polling as the primary path.

- `pick` creates a real detached session when no safe `ops*` session exists.
- Existing sessions are reused only when they are managed and idle.
- If every `ops*` session is active, busy, or unmanaged, `pick` returns an error instead of selecting a busy pane.
- Managed sessions use a small bash prompt hook that signals `tmux wait-for` when the prompt is actually reached.
- `run` writes a generated shell script, captures stdout and stderr through `tee`, records status JSON, and returns a log tail.
- `probe` checks cached sudo first, then can use a target-host environment key and the generic fallback through private stdin. Gate and run results record the variable name and candidate statuses without recording values.
- A timeout means the command is still running. It is not reported as command failure.
- A second `run` is refused while `active.json` exists for that session.
- Interruption is explicit through `cancel --run-id`.
- `keepalive` records bounded sudo refresh intent and performs only single explicit refreshes with hard-coded `sudo -n -v`.

## State layout

Managed state lives outside the repo:

```text
/tmp/localsetup-tmux-ops/<session>/
  active.json
  idle.json
  managed.json
  pane-operation.lock
  probe.pending.json
  probe.auth-blocked.json
  probe-<operation-id>.sh
  probe-<operation-id>.status.json
  probe.status.json
  keepalive.json
  keepalive-refresh-<id>.sh
  keepalive-refresh-<id>.status.json
  last_run_id
  <run_id>.status.json
  logs/<run_id>.log
  scripts/<run_id>.sh
```

The session directory is returned as `state_dir` by `pick`. Logs and generated scripts are intentionally in `/tmp`, not in the repository.

## Transport and approval boundary

`tmux_ops` is only the elevated or PTY transport. Picking or attaching to a session, probing sudo, receiving `sudo ready`, and holding cached sudo credentials do not authorize the command sent through `run`.

Before every managed `run`, the agent must have one still-matching authorization record:

- a verified handoff from `ls-wf-risky-operations-safety-approval`; or
- a direct application of `ls-system-changes-backup` with equivalent evidence.

The record must freeze the exact command or edit and values, exact target, risk classification, likely consequences and affected users, services, files, or other scope, backup or no-backup decision, rollback action, and the user's immediate explicit approval. Reject missing, incomplete, stale, or changed records and return to the applicable approval workflow. Each `run` payload requires its own record; transport readiness never substitutes for point-of-risk approval.

## Human workflow

1. Pick a session:

   ```bash
   ./ls/tools/tmux_ops pick
   ```

2. Attach if you want to watch or enter a sudo password:

   ```bash
   tmux new-session -A -s ops
   ```

   Use the exact `attach_command` returned by `pick`; the session may be `ops`, `ops1`, or another managed `ops*` name.

3. Probe sudo:

   ```bash
   ./ls/tools/tmux_ops probe -t ops
   ```

4. If the probe says `password_required` or `action_required: true`, attach with the returned `attach_command`, run `sudo -v` in that exact tmux pane, enter the password, then tell the agent `sudo ready` so it can probe again. This confirms readiness only; it does not approve a command.

5. Verify the exact pending command has the complete, still-matching authorization record described above. If not, stop before `run`.

6. Run the approved command:

   ```bash
   ./ls/tools/tmux_ops run -t ops -- sudo apt update
   ```

7. Read the returned `tail` first. If you need more context, read the returned `log_path`.

8. If the command is still active:

   ```bash
   ./ls/tools/tmux_ops status -t ops --run-id <run_id> --wait --timeout 120
   ```

9. Cancel only when you intend to interrupt that exact run:

   ```bash
   ./ls/tools/tmux_ops cancel -t ops --run-id <run_id>
   ```

10. For long privileged maintenance, request bounded keepalive only after sudo is ready and while the approved privileged work remains active:

   ```bash
   ./ls/tools/tmux_ops keepalive request -t ops --owner agent-id --ttl-seconds 7200 --max-refreshes 24 --reason "active privileged maintenance"
   ```

   Refreshes are one-shot calls, not a background loop:

   ```bash
   ./ls/tools/tmux_ops keepalive refresh -t ops
   ```

## Agent workflow

Agents should follow this script exactly:

1. Prepare the exact pending command or edit and values, exact target, risk classification, consequences and affected scope, backup or no-backup decision, and rollback action.
2. Before any `run`, either verify a complete `ls-wf-risky-operations-safety-approval` handoff or apply `ls-system-changes-backup` directly and obtain the user's immediate explicit approval for that frozen payload. Record the approval. If the payload changes, reject the record and repeat the gate.
3. Run `./ls/tools/tmux_ops pick`.
4. Parse JSON. If it has `error`, report the error and stop.
5. Show `attach_command` to the user in a copy-paste code block.
6. Run `./ls/tools/tmux_ops probe -t <session>`.
7. If `action_required` is `true` or `sudo` is `password_required`, tell the user to attach with the returned `attach_command`, run `sudo -v` in that exact tmux pane, enter the password, and reply `sudo ready`. Do not run commands yet; `sudo ready` confirms readiness only and is not command approval.
8. After `sudo ready`, run `probe` again.
9. If `sudo` is `failed`, report `detail` and stop.
10. If `sudo` is `ready`, reconfirm that the recorded payload still matches, then run exactly that one approved logical command with:

   ```bash
   ./ls/tools/tmux_ops run -t <session> -- <command>
   ```

11. Check `status`:
   - `completed`: read `exit_code`, `tail`, and `log_path`; continue only after verifying the result.
   - `running`: keep the `run_id`; use `status --wait` to continue watching.
12. Start another `run` only after the active run has completed and the next payload has its own still-matching authorization record. Cancel only when the user explicitly asks to interrupt the active run.
13. Interrupt only with:

   ```bash
   ./ls/tools/tmux_ops cancel -t <session> --run-id <run_id>
   ```
14. If approved privileged work must keep the same sudo timestamp alive, request a bounded marker only after sudo is `ready`:

   ```bash
   ./ls/tools/tmux_ops keepalive request -t <session> --owner <id> --ttl-seconds 7200 --max-refreshes 24 --reason "<reason>"
   ```

   Run `keepalive refresh -t <session>` only as an explicit one-shot refresh while the approved work is active and the managed pane is idle between commands. Check `keepalive status -t <session>` for heartbeat/reporting, and end it with `keepalive clear -t <session> --owner <id>`.

## Command reference

| Command | Use | Important fields |
|---|---|---|
| `pick` | Select or create a managed session. | `session`, `reason`, `state_dir`, `attach_command` |
| `probe -t SESSION` | Classify sudo state without fixed sleeps. | `sudo`, `detail`, `credential_outcome`, `credential_variable`, `credential_candidates`, `credential_candidates_checked` |
| `run -t SESSION [--timeout SECS] [--tail N] -- CMD` | Run one command and capture logs/status. | `run_id`, `status`, `exit_code`, `elapsed_s`, `log_path`, `tail`, `credential_outcome`, `credential_variable`, `credential_candidates` |
| `status -t SESSION [--run-id ID] [--wait --timeout SECS]` | Observe active or completed runs without sending input. | same fields as `run` |
| `cancel -t SESSION --run-id ID` | Send the only supported interrupt for an active run. | `run_id`, `status`, `cancel_sent` |
| `keepalive request -t SESSION --owner ID --ttl-seconds N --max-refreshes N --reason TEXT` | Record bounded sudo refresh intent for a managed session with a ready sudo gate. | `state`, `owner`, `expires_at`, `refresh_count`, `max_refreshes`, `attach_command` |
| `keepalive refresh -t SESSION` | Run one hard-coded `sudo -n -v` refresh in the managed pane. | `state`, `last_refresh_ok`, `refresh_count`, `disabled_reason` |
| `keepalive status [-t SESSION]` | Report one or all keepalive markers without running sudo. | `ok`, `sessions`, `seconds_remaining`, `last_refresh_ok` |
| `keepalive clear -t SESSION --owner ID` | Disable a marker only when the owner matches. | `state`, `disabled_reason` |
| `keepalive sweep` | Disable expired or invalid markers without running sudo. | `ok`, `sessions` |

## JSON examples

Pick:

```json
{
  "session": "ops",
  "reason": "created",
  "state_dir": "/tmp/localsetup-tmux-ops/ops",
  "attach_command": "tmux new-session -A -s ops"
}
```

Completed run:

```json
{
  "run_id": "1778300000-abcd1234ef",
  "session": "ops",
  "status": "completed",
  "exit_code": 0,
  "elapsed_s": 1.234,
  "log_path": "/tmp/localsetup-tmux-ops/ops/logs/1778300000-abcd1234ef.log",
  "tail": "last log lines",
  "attach_command": "tmux new-session -A -s ops",
  "credential_outcome": "automatic",
  "credential_variable": "GENERIC_SUDO_KEY",
  "credential_candidates": [
    {"variable": "NODE-WEST.EXAMPLE_SUDO_KEY", "status": "rejected"},
    {"variable": "GENERIC_SUDO_KEY", "status": "used"}
  ],
  "credential_candidates_checked": true
}
```

Timed-out but still running:

```json
{
  "run_id": "1778300000-abcd1234ef",
  "session": "ops",
  "status": "running",
  "exit_code": null,
  "elapsed_s": 30.001,
  "log_path": "/tmp/localsetup-tmux-ops/ops/logs/1778300000-abcd1234ef.log",
  "tail": "partial output",
  "credential_outcome": "cached",
  "credential_variable": null,
  "credential_candidates": [],
  "credential_candidates_checked": false
}
```

## Timeout and cancellation semantics

`--timeout` is the time the tool waits for completion before returning to the agent. It is not a kill timer.

When `run` returns `status: "running"`:

- The command is still active in tmux.
- `active.json` remains live.
- A second `run` returns `error: "run already active"`.
- `status --wait` can keep watching the same `run_id`.
- `cancel --run-id` is the only documented interrupt path.

## Sudo semantics

`probe` checks `sudo -Nnv` first and falls back to `sudo -vn` only when `-N` is unsupported. If the check clearly requires a password, it resolves credentials inside the target pane in this order:

1. `<hostname.upper()>_SUDO_KEY`, with dots and hyphens preserved in the host name.
2. `GENERIC_SUDO_KEY` when the target value is unavailable, empty, or definitely rejected.
3. Manual `sudo -v` in the exact pane when no usable variable remains.

The Python helper reads the environment from inside that pane. A local environment key is never forwarded over SSH. Each non-empty value is sent only on private stdin to the fixed `sudo -S -v -p ''` command; child output is captured privately for classification. Newline-bearing values are rejected without being sent. Timeout, ambiguous authentication, or policy/MFA failure blocks fallback and leaves a fence that allows only noninteractive checking until sudo is ready again.

Probe and managed run results include `credential_outcome`, `credential_variable`, `credential_candidates`, and `credential_candidates_checked`. `cached` means noninteractive sudo was already ready; environment candidates are not reported as absent unless they were checked. `automatic` names the variable that succeeded. `manual` means a user must authenticate in the pane; `no_env` means both candidates were checked and unavailable or empty. A null `credential_variable` means no environment variable was used. Candidate statuses are limited to `unavailable`, `empty`, `rejected`, `used`, or `ambiguous` and contain no values. Managed run scripts remove `*_SUDO_KEY` variables from the command environment before capturing output.

- `ready`: sudo is available noninteractively; use the reported `credential_outcome` and `credential_variable` to tell whether it was cached or which environment key was used.
- `password_required`: the user must attach to the returned tmux session, run `sudo -v` in that pane, enter the password, then have the agent probe again.
- `failed`: sudo is unavailable, denied, or had an ambiguous outcome; report the safe `detail` and stop. Do not retry or advance credentials after a timeout, policy/MFA error, or ambiguous operation.

Do not treat a shell prompt as proof of sudo readiness. Trust the `probe` JSON. `tmux_ops run` also checks for a fresh `ready` sudo gate before sending a command and refuses without creating run state when sudo action is required.

`tmux_ops keepalive` exists only for active privileged work in managed `ops*` sessions. It does not start a loop, alter sudo policy, or read command strings from marker files. `request` caps TTL at 7200 seconds and refreshes at 24, records the current pane identity when available, and requires a ready sudo gate in that same pane. `refresh` validates the marker, expiry, refresh count, managed session, recorded pane identity, active-run state, pane idleness, and per-session refresh lock before running a generated script whose only sudo command is hard-coded `sudo -n -v`. If the pane is busy, another refresh is in progress, or a managed run is active, refresh returns `action_required: true` without sending input. If the sudo refresh fails or times out, the marker is disabled and agents should stop refreshing and hand control back to the user. `status`, `sweep`, and `clear` never run sudo; `status` and `sweep` disable expired active markers before reporting them.

`pane-operation.lock` is a short-lived guard around managed `send-keys` operations. A probe records `probe.pending.json` before dispatch and accepts only the matching operation ID, completion signal, pane identity, and returned idle prompt. If dispatch or completion is uncertain, later probes reconcile that same result and never replay it. An authentication ambiguity also creates `probe.auth-blocked.json`; only a later successful noninteractive check clears it. If a process is killed while holding the lock, later `run`, `probe`, or `keepalive refresh` calls will report that a pane operation is already in progress. Remove the stale lock only after confirming no `tmux_ops` process is still active for that session.

## Remote hosts

For remote tmux servers, set `REMOTE_TMUX_HOST` and use the same commands. See [tmux-ops-remote.md](tmux-ops-remote.md).

## Legacy commands

`send` and `wait` remain available for compatibility and low-level diagnostics. They are not the normal documented path because they do not provide the managed run status, active-run lock, generated logs, or explicit cancellation contract.
