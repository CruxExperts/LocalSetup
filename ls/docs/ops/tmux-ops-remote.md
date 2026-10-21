---
status: ACTIVE
version: 4.56
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

The returned `attach_command`, `state_dir`, `log_path`, and `run_id` all refer to the remote host.
The sudo probe checks `sudo -Nnv` on the remote host first. If it needs a password, it reads `<remote-hostname.upper()>_SUDO_KEY` and then `GENERIC_SUDO_KEY` from inside the target pane; hostname dots and hyphens are preserved. Do not forward a local credential variable over SSH. Configure credentials in the remote host's pane environment before the tmux session starts. The response reports only the variable name and candidate status, never its value, and managed run commands do not inherit `*_SUDO_KEY` variables.

If `probe` or `run` returns `action_required: true`, attach on the remote tmux host with the returned `attach_command`, run `sudo -v` in that exact pane, enter the password, then tell the agent `sudo ready` so it can probe again. A timeout, ambiguous result, or policy/MFA failure must not advance to another credential or replay an uncertain probe; allow only reconciliation of the same pending operation and return control if it remains blocked.

Use `credential_outcome`, `credential_variable`, and `credential_candidates` from both probe and managed run output to report whether sudo was already cached, which key was used, or whether manual/no-environment handling is required. A null `credential_variable` means no key was used; cached success does not claim that keys were checked.

If a run returns `status: "running"`, keep watching by run ID:

```bash
./ls/tools/tmux_ops status -t ops --run-id <run_id> --wait --timeout 120
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
- If sudo returns `password_required`, ask the user to attach to the remote tmux session and enter the password there.

## When not to set it

If the agent process already runs on the same host as tmux, do not set `REMOTE_TMUX_HOST`. Run `./ls/tools/tmux_ops` directly.
