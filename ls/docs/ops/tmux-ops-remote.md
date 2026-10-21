---
status: ACTIVE
version: 4.57
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
