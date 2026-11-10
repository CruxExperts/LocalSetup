---
status: ACTIVE
version: 4.90
owner_package: ls-wf-sudo-tmux-session
---

# Sudo tmux tool: remote

**Purpose:** Use the managed `tmux_ops` workflow when the tmux server runs on a different host than the agent process.

For the local managed workflow, state layout, JSON examples, and agent script, see [tmux-ops-managed.md](tmux-ops-managed.md). This page only explains the remote wrapper behavior.

## Managed remote backend

FleetOps selects qualified local presentation and remote execution backends
independently before dispatch. Herdr is preferred; SSH plus tmux is the managed
remote fallback. A fixed checksum-staged helper runs the shared terminal owner
on the selected machine so native process, TTY and shell checks refer to that
machine. Use exact recorded native IDs and strict SSH target/host-key binding;
never select the first pane in a session or infer a host from prompt text.

The local agent keeps its lower operations pane as the visible attachment. Its
name is `<repo>-<exact-host-alias>-sudo`; parent/controller ownership remains
separate from that label. Credentials stay controller-held and enter a bounded
private operation channel. They are never SSH environment variables, terminal
input, command arguments or regular-file contents. The remote receipt wrapper
stays outside the exact operation-bound sudo command.

Only a recorded idle owned connection can detach or log out automatically.
Requalify the original local shell before changing directory or reconnecting.
Unknown authentication, command or disconnect outcomes stop new input; observe
the original operation instead of changing backends or repeating it.

The shared terminal operations owner supplies the checksum-bound sudo runtime,
remote wrapper support and private candidate delivery used by FleetOps.
Consumers provide their own qualified transport and operation bindings; reuse
does not establish host admission or authorize an operation. Authentication
input remains separate from workload stdin, and unknown outcomes require
read-only reconciliation rather than replay.

## One-off controller-owned diagnostics

For an explicitly authorized diagnostic on a host that is not enrolled in
FleetOps, use the existing Herdr task interface. It qualifies the actual local
foreground controller through native pane evidence and kernel ancestry, then
uses that controller's protected lower operations pane. This is a separate
one-off operation, not a FleetOps host admission or a replacement for its gates.

```bash
localsetup herdr --state-dir "$task_state" task diagnostic \
  --target "$approved_ssh_target" --id "$unique_32_hex_operation_id" \
  --timeout 120 -- python3 -c "$approved_read_only_projection"
```

Run from the controller's current checkout and supply its matching native
`--socket`, `--workspace` and `--pane` when they are not inherited. The existing
task state directory must be absolute. Python 3 and the supported sudo/askpass
environment must already be available remotely. No package, daemon, scheduler,
host enrollment or configuration change is performed.

The operation stages nonsecret, checksum-bound runtime code and an exclusive
exact-argv claim under the remote user's qualified private runtime directory. Credentials
remain in the controller's existing generic-key resolver and are delivered only
after a current, exact-wrapper challenge. Native owner, protected-pane operation,
target/trust identity and the deadline are rechecked after blocking preparation,
immediately before authentication delivery. The channel does not consume or
replace the workload's stdin. SSH children never inherit sudo credential keys.

The result reports the operation ID and binding key. A repeated ID is refused,
including after timeouts or lost acknowledgements. Read the original receipt:

```bash
localsetup herdr --state-dir "$task_state" task diagnostic-status \
  --binding-key "$original_binding_key" --id "$original_operation_id"
```

Status never dispatches a command or supplies a credential. Unknown outcomes
remain unknown; do not replay, switch backends or interrupt healthy work to
make them appear complete. Preserve private claims and receipts for recovery.
Transient runtime/authentication files are removed by their owning receiver
after known completion; uncertain workloads retain their resources. Returned
output contains at most the newest 100 lines and 16 KiB per stream, with
explicit truncation flags. Review the projection before dispatch so raw logs,
private request bodies and credentials never become its output.

## Explicit legacy remote wrapper

The historical `REMOTE_TMUX_HOST` wrapper below is compatibility behavior, used
with explicit `--legacy-session` commands. It does not establish a managed
caller binding for the normal pane path.

## Configuration

- `REMOTE_TMUX_HOST` - Hostname or IP of the machine where tmux runs. When set, `ls/tools/tmux_ops` runs the Python tool over SSH on that host and returns the same JSON. Installed packages expose the same entrypoint through the `localsetup://tool/tmux_ops` alias.
- `REMOTE_TMUX_CWD` - Optional repo path on the remote host. Default: `/opt/devzone/devops`.

## Flow

Legacy remote mode changes only the transport location. Reuse the exact current task/target authorization and preserve explicit destructive-scope approval and recovery. `sudo ready` proves only credential readiness, never command authority.

Use the same managed commands locally or remotely:

```bash
export REMOTE_TMUX_HOST=sh0t
./ls/tools/tmux_ops pick --legacy-session
./ls/tools/tmux_ops probe -t ops
./ls/tools/tmux_ops run --legacy-session -t ops -- sudo apt update
```

The returned `attach_command`, `state_dir`, `log_path`, and `run_id` refer to the remote host. A sudo probe checks cached readiness first, then resolves only `GENERIC_SUDO_KEY` through the controller environment and EnvMan when absent. Explicit empty or invalid environment values are authoritative. Do not forward other credential variables over SSH; response records include only safe variable names and statuses, and workload environments strip every `*_SUDO_KEY`.

Use a manual fallback only when a run result includes `sudo.manual_fallback`; that receipt proves authentication stopped before the workload started. Run the exact quoted command once in the qualified remote pane. For timeout, owner loss, policy/MFA denial, `running`, or `unknown`, use read-only status reconciliation only. Authentication has a separate 120-second default budget, configurable with `run --auth-timeout <secs>`, and closing it does not interrupt an already-running workload.

Use `credential_outcome`, `credential_variable`, and `credential_candidates` from both probe and managed run output to report whether sudo was already cached, which key was used, or whether manual/no-environment handling is required. A null `credential_variable` means no key was used; cached success does not claim that keys were checked.

If a run returns `status: "running"`, keep watching through its durable handle:

```bash
./ls/tools/tmux_ops status --status-handle <status_handle> --wait --timeout 120
```

Interrupt only through the managed cancel path:

```bash
./ls/tools/tmux_ops cancel -t ops --run-id <run_id>
```

## Agent rules

- Do not use raw SSH plus tmux commands.
- Do not assume local `/tmp/localsetup-tmux-ops` contains remote status.
- Do not start another `run` while the remote session has an active `run_id`.
- Do not use tmux readiness as authorization; verify the exact approved payload immediately before each `run`.
- If sudo returns `password_required`, attach to the exact qualified pane and run the exact authorized command manually once.
- Check `capability_status` for `helper_version_mismatch` or `helper_capabilities_missing`; refresh the remote helper before any retry. `tmux_ops capabilities` reports local controller API support.

## When not to set it

If the agent process already runs on the same host as tmux, do not set `REMOTE_TMUX_HOST`. Run `./ls/tools/tmux_ops` directly.
