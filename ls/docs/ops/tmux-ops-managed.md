---
status: ACTIVE
version: 4.57
owner_package: ls-wf-sudo-tmux-session
---

# Sudo tmux managed workflow

**Purpose:** Explain the managed `tmux_ops` implementation for humans and give agents a deterministic command flow for host operations.

The normal path resolves the actual caller and creates or reuses its owned lower
pane. It never creates a separate operations tab or standalone local session.
Use exact returned handles, not whichever pane is active:

```text
pick -> run -t <returned-target> -- <argv...> -> status
```

`pick` derives the canonical repository and caller from verified runtime context.
Explicit `--repository`, `--host-alias`, `--cwd` and `--state-dir` bind an existing
context when needed. The visible name is `<repo>-sudo` at the root and
`<repo>-<exact-host-alias>-sudo` for an admitted host checkout. A child occupies
one third of its parent's original height without changing focus. The parent,
endpoint, target and live controller distinguish concurrent identical names.

The shared `ls.core.terminal_ops` owner serves Herdr and tmux. Its controlled Bash
handler checks primary-prompt and empty-input state, machine/TTY/shell generation
and writer ownership before executing staged argv. The native adapter sends only
a reserved trigger. It never appends command text, presses Enter or clears input.
Busy/unknown panes stop dispatch; a lost response never causes replay. Automatic
dispatch currently requires Linux process/TTY evidence and controlled Bash; an
unsupported environment returns an unavailable result rather than typing into it.

A managed-pane run returns a durable `status_handle`. Keep it to observe the
receipt after changing directory or restarting the controller:

```bash
./ls/tools/tmux_ops status --status-handle <status_handle> --wait --timeout 120
```

If the run used an explicit `--state-dir`, supply the same state directory when
reading its handle. `./ls/tools/tmux_ops capabilities` reports the helper version
and supported features without creating state. These handles belong to the
managed-pane route; the explicit legacy route below uses a session and run ID.

Privileged argv automatically uses one operation-bound `sudo -A` invocation and
private askpass input, preserving command stdin and command-specific sudoers
rules. Credentials come only from `GENERIC_SUDO_KEY`, resolved from the
controller environment and then EnvMan when absent. An explicit empty or
invalid value is authoritative; hostname-specific variables are ignored. A
timeout, owner loss, policy denial or uncertain result stops authentication
without replay. Cached sudo validation is not a promise that a new invocation
can execute. Automatic authentication has an independent 120-second default
budget, configurable with `run --auth-timeout <secs>`. A run provides
`sudo.manual_fallback` only when its receipt proves authentication stopped
before the workload started; execute that exact quoted command once in the
qualified pane. For timeout, owner loss, policy denial, `running`, or `unknown`,
use read-only status reconciliation only. Do not run a separate `sudo -v` or
replay an uncertain command.

Keep existing task authorization and recovery boundaries. Authentication does not
grant new command scope and routine in-scope sudo does not require reapproval.
Only a proven idle owned attachment can unwind to the original local shell before
changing directory or target. Backend selection happens before dispatch.

## Explicit legacy-session reference

The following session-based commands and state are retained only for callers
that explicitly select `--legacy-session`. A binary override does not select this
lifecycle. New automated callers use the managed pane path above.

### Legacy behavior

The managed implementation replaces prompt-regex polling as the primary path.

- `pick` creates a real detached session when no safe `ops*` session exists.
- Existing sessions are reused only when they are managed and idle.
- If every `ops*` session is active, busy, or unmanaged, `pick` returns an error instead of selecting a busy pane.
- Managed sessions use a small bash prompt hook that signals `tmux wait-for` when the prompt is actually reached.
- `run` writes a generated shell script, captures stdout and stderr through `tee`, records status JSON, and returns a log tail.
- `probe` checks cached sudo first, then can use only `GENERIC_SUDO_KEY` through the private channel. Gate and run results record the variable name and candidate statuses without recording values.
- An observation timeout means the command remains running. Its receipt is not demoted to unknown, and completion may still be recorded once.
- A second `run` is refused while `active.json` exists for that session.
- Interruption is explicit through `cancel --run-id`.
- `keepalive` records bounded sudo refresh intent and performs only single explicit refreshes with hard-coded `sudo -n -v`.

### Legacy state layout

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

Transport readiness is separate from authority. Reuse the user's current exact
task/target scope; retain explicit approval and verified recovery for destructive
work. Neither credential variables nor a ready pane authorize an expanded
payload. See the owning [sudo workflow](../../workflows/ls-wf-sudo-tmux-session/SKILL.md).

## Legacy human workflow

1. Pick a session:

   ```bash
   ./ls/tools/tmux_ops pick --legacy-session
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

4. If the probe reports that manual authentication is required, hand over the exact authorized pending command once in the qualified pane. The probe has not started that workload. If a run has already been dispatched, read its receipt first; a timeout, owner loss, policy denial, `running`, or `unknown` result permits read-only reconciliation only.

5. Verify the exact pending command has the complete, still-matching authorization record described above. If not, stop before `run`.

6. Run the approved command:

   ```bash
   ./ls/tools/tmux_ops run --legacy-session -t ops -- sudo apt update
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

## Legacy agent workflow

Agents should follow this script exactly:

1. Prepare the exact pending command or edit and values, exact target, risk classification, consequences and affected scope, backup or no-backup decision, and rollback action.
2. Reuse the user's current authorization for the exact task and target. Destructive work retains its required recovery plan and explicit scope approval. A changed payload requires review only when it materially expands that scope; terminal readiness does not add authority.
3. Run `./ls/tools/tmux_ops pick --legacy-session`.
4. Parse JSON. If it has `error`, report the error and stop.
5. Show `attach_command` to the user in a copy-paste code block.
6. Run `./ls/tools/tmux_ops probe -t <session>`.
7. If the probe reports `action_required` or `sudo: password_required`, hand over the exact authorized pending command once in the qualified pane. If a run was already dispatched, reconcile that run first and never replay an uncertain outcome.
8. If `sudo` is `failed`, report the safe `detail` and `capability_status`, then stop.
9. If `sudo` is `ready`, reconfirm that the recorded payload still matches, then run exactly that one approved logical command with:

   ```bash
   ./ls/tools/tmux_ops run --legacy-session -t <session> -- <command>
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

## Legacy command reference

| Command | Use | Important fields |
|---|---|---|
| `pick --legacy-session` | Select or create a managed session. | `session`, `reason`, `state_dir`, `attach_command` |
| `probe -t SESSION` | Classify sudo state without fixed sleeps. | `sudo`, `detail`, `credential_outcome`, `credential_variable`, `credential_candidates`, `credential_candidates_checked` |
| `run --legacy-session -t SESSION [--timeout SECS] [--tail N] -- CMD` | Run one command and capture logs/status. | `run_id`, `status`, `exit_code`, `elapsed_s`, `log_path`, `tail`, credential provenance |
| `status -t SESSION --run-id ID [--wait --timeout SECS]` | Read the legacy session receipt and optionally wait for completion. | `run_id`, `status`, `exit_code`, `tail`, `log_path` |
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

`--timeout` bounds observation of command completion; it is not a kill timer. Managed sudo has a separate `--auth-timeout`, defaulting to 120 seconds. Its foreground broker may remain alive beyond the observation budget until authentication completes or its budget expires.

When `run` returns `status: "running"`:

- The command is still active in tmux.
- In the legacy route, `active.json` remains live and a second `run` returns `error: "run already active"`. Observe it with `status -t SESSION --run-id ID --wait`.
- In the managed-pane route, keep the durable handle and use `status --status-handle HANDLE --wait`, including after a directory change or controller restart. Its writer lease fences further dispatch while work is running or uncertain.
- Legacy cancellation uses `cancel -t SESSION --run-id ID`. Managed-pane observation does not grant permission to interrupt or replay a workload.

## Sudo semantics

`probe` checks `sudo -Nnv` first and falls back to `sudo -vn` only when `-N` is unsupported. Automatic authentication resolves only `GENERIC_SUDO_KEY` from the controller environment, then EnvMan when it is absent. Explicit environment values, including empty and invalid values, override EnvMan. Hostname-specific `*_SUDO_KEY` values are ignored. The exact sudo invocation uses the private framed askpass channel; command stdin is preserved and all `*_SUDO_KEY` variables are removed from workload environments.

Authentication and observation have independent timeouts. `--auth-timeout` defaults to 120 seconds; when it closes, the broker stops delivering credentials without interrupting a workload that has already started. If an askpass request was observed but lookup did not finish before that budget, results identify `authentication_status=expired`; the execution outcome still requires status reconciliation and no manual replay is offered. An observation timeout leaves the operation running and returns a durable `status_handle`. A completed receipt is recorded once and reconciled with only its matching writer lease. Delayed or duplicate reserved triggers fail the single-use operation and live prompt checks.

Legacy probe/run results expose `credential_outcome`, `credential_variable`, `credential_candidates`, `credential_candidates_checked`, and safe `detail`. `cached` means noninteractive sudo was already ready; `automatic` names the variable used; `no_env` means the generic candidate was unavailable or empty. Managed-pane runs expose a `sudo` receipt with `status`, `capability_status`, `credential_candidates`, safe `detail`, and optional `credential_lookup_status`, authentication diagnostics, and `manual_fallback`. A candidate recorded as `provided_to_askpass` proves delivery, not successful authentication. Lookup statuses distinguish `unavailable`, `empty`, `invalid`, `loading_error`, and `expired`. They contain no values. Only explicit sudo retry evidence establishes rejection. Remote probe `capability_status` distinguishes stale helpers from credential and policy failures. Use `tmux_ops capabilities` to inspect local API support.

- `ready`: sudo is available noninteractively; use the reported `credential_outcome` and `credential_variable` to tell whether it was cached or which environment key was used.
- Legacy `password_required` from a probe: the pending workload has not been dispatched; hand over its exact authorized command once if manual execution is needed. For a managed-pane run, use a manual fallback only when its receipt includes `sudo.manual_fallback`, proving the workload did not start. Execute that exact command once in the qualified pane.
- `failed`: sudo is unavailable, denied, or had an ambiguous outcome; report the safe `detail` and stop. Do not retry or advance credentials after a timeout, policy/MFA error, or ambiguous operation.

Do not treat a shell prompt as proof of sudo readiness. Trust the `probe` JSON. The legacy `run --legacy-session` checks for a fresh `ready` sudo gate before sending a command and refuses without creating run state when sudo action is required. Managed-pane runs authenticate within the exact operation-bound sudo invocation.

`tmux_ops keepalive` exists only for active privileged work in managed `ops*` sessions. It does not start a loop, alter sudo policy, or read command strings from marker files. `request` caps TTL at 7200 seconds and refreshes at 24, records the current pane identity when available, and requires a ready sudo gate in that same pane. `refresh` validates the marker, expiry, refresh count, managed session, recorded pane identity, active-run state, pane idleness, and per-session refresh lock before running a generated script whose only sudo command is hard-coded `sudo -n -v`. If the pane is busy, another refresh is in progress, or a managed run is active, refresh returns `action_required: true` without sending input. If the sudo refresh fails or times out, the marker is disabled and agents should stop refreshing and hand control back to the user. `status`, `sweep`, and `clear` never run sudo; `status` and `sweep` disable expired active markers before reporting them.

`pane-operation.lock` is a short-lived guard around managed `send-keys` operations. A probe records `probe.pending.json` before dispatch and accepts only the matching operation ID, completion signal, pane identity, and returned idle prompt. If dispatch or completion is uncertain, later probes reconcile that same result and never replay it. An authentication ambiguity also creates `probe.auth-blocked.json`; only a later successful noninteractive check clears it. If a process is killed while holding the lock, later `run`, `probe`, or `keepalive refresh` calls will report that a pane operation is already in progress. Remove the stale lock only after confirming no `tmux_ops` process is still active for that session.

## Remote hosts

For remote tmux servers, set `REMOTE_TMUX_HOST` and use the same commands. See [tmux-ops-remote.md](tmux-ops-remote.md).

## Legacy commands

`send` and `wait` remain available for compatibility and low-level diagnostics. They are not the normal documented path because they do not provide the managed run status, active-run lock, generated logs, or explicit cancellation contract.
