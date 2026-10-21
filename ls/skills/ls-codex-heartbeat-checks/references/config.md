---
status: ACTIVE
version: 3.4
---

# Heartbeat Config

`config/codex_heartbeat.yaml` is target-repo state created only by `localsetup harness codex-heartbeat init`.

## Core fields

- `heartbeat.enabled`: `false` after init. `enable` flips it to `true`; `disable` flips it back.
- `heartbeat.interval_minutes`: integer cron cadence; minute values below 60 must
  divide 60 evenly, and larger values must be whole hours that divide 24.
  Examples: 15 minutes, 120 minutes, 480 minutes, or 1440 minutes. Values such as
  35, 90, and 420 are refused before enabling; strings and booleans are invalid.
- `heartbeat.state_dir`: repo-relative runtime artifact directory. Absolute paths and parent traversal are rejected.
- `heartbeat.stale_after_seconds`: minimum age before a same-host lock whose recorded PID is no longer live is eligible for reclamation under the [recovery contract](recovery.md). It defaults to 3600 and must be a positive integer.
- `agent.enabled`: controls whether normal `run` may launch the configured agent
  profile. `run --no-agent` skips profile loading and executable resolution as
  well as launch. Hooks and transaction validation still run. A disabled heartbeat
  returns before command planning unless explicitly forced.
- `agent.profile`: profile name under `agent_profiles`; the shipped disabled default is `heartbeat`.
- `agent.timeout_seconds`: optional override for the selected profile timeout.

## Agent profiles

Each `agent_profiles.<name>` mapping is client-neutral:

- `client`: non-empty identity label recorded in command evidence. The runtime accepts any framework agent CLI; it does not infer vendor-specific flags.
- `command`: non-empty argv list for that client. With `prompt_transport: argv`, it contains exactly one `{heartbeat_prompt}` argument. `stdin` sends the prompt on standard input; `none` sends no prompt.
- `launcher`: `resolved-path` resolves the first argv element through `path` or `path_env` and runs the absolute executable with `shell=False`; `direct-argv` preserves a fully specified argv list; `shell-login` is opt-in compatibility for profile-managed installs and records the rendered shell command.
- `prompt`: heartbeat instruction string.

The shipped profile is disabled and demonstrates the current Codex non-interactive form `codex exec --json {heartbeat_prompt}`. Replace its `client` label and `command` argv for another framework CLI; do not add client-specific behavior to the heartbeat runtime.

## Hooks and direct-command policy

- `hooks.before` and `hooks.after`: serial argv-list commands with optional `timeout_seconds` and `allow_direct`.
- `direct_command_policy.allow_git_writes`: required for direct `git commit` or `git push`, including commands with Git global options before the subcommand.
- `direct_command_policy.allow_destructive`: required for blocked destructive executable names.
- `direct_command_policy.allowlist` and per-command `allow_direct` do not bypass either prohibition.


## Cadence and stored schedules

The generated schedule follows wall-clock fields, starting at minute zero for
whole-hour intervals and midnight for daily execution. It is not a monotonic
timer measured from activation or the previous run's completion. The harness
does not emit a timezone override: the installed cron daemon's configured
timezone and clock-change behavior apply. Daylight-saving transitions can change
elapsed spacing; consult that daemon's documentation before activation.
[Crontab field semantics](https://www.man7.org/linux/man-pages/man5/crontab.5.html)
and [Cronie clock-change handling](https://www.man7.org/linux/man-pages/man8/crond.8.html)
describe why field steps do not imply arbitrary elapsed-minute intervals.

Inspection and disabling do not rewrite a stored trigger's schedule. Re-enabling
explicitly regenerates the heartbeat trigger from the validated interval.
Unrelated triggers/tasks and historical heartbeat identifiers remain intact.
Existing unsupported intervals are not silently rounded or migrated.

## Typed LSCli profile

The shipped `lscli-heartbeat` example is unselected and disabled. Replace every
/explicit/... path and the provider profile name with your reviewed setup,
then deliberately select `agent.profile=lscli-heartbeat` and set
`agent.enabled=true` for manual agent runs. Heartbeat enablement remains a
separate switch. Generated cron commands still request `--no-agent`; changing a
recurring job's authority requires its own explicit schedule decision.

This profile uses `client=lscli` and `launcher=lscli` and requires exactly the
fields shown in the template. Generic command, shell, PATH overrides,
prompt-transport overrides, and allow_direct are rejected. All six paths must
be absolute and canonical. The executable must be an owned registered command
for the explicitly supplied runtime root. The framework validates its receipt
and selected runtime, then invokes the protected dispatcher directly.

The provider profile name accepts 1–256 characters. Prompt text must be nonempty,
at most 128 KiB of UTF-8, and is sent only through stdin. It does not appear in
command plans or result sidecars. Workspace is always the heartbeat target.
The grant separately authorizes file access, writes, disclosure, and recipes;
mentioning HEARTBEAT.md in a prompt grants none of these permissions.

Limits are strict integers: timeout 1–3600 seconds, requests 1–64, tools 0–256,
tokens 1–1048576, protocol inactivity 1–3600 seconds (at most the coding timeout),
and combined output 1024–4194304 bytes. An explicit `agent.timeout_seconds`
overrides the profile timeout and must satisfy the same bounds. The outer
process deadline adds 20 seconds for protected startup and terminal delivery;
registration qualification has separate bounded lock waits before process
launch. Only valid protocol start/progress events reset inactivity. The coding
runtime enforces request/tool/token limits and its existing fixed resource
limits; the outer runner independently bounds output and process lifetime.

No credentials are discovered by planning. Runs require the selected provider's
explicit credential configuration and a qualified delegated sandbox backend.
Approval requests are rejected because this profile has no interactive approval
channel. Completion requires both valid JSONL and process success; it does not
mean the controller accepted an issue. Each run starts a new session; automatic
resume, compaction, and semantic no-progress policy use the explicit reserved
action interface below, rather than changing this fresh-profile schema.

Use the owning framework's harness command. A copied standalone skill without
the framework cannot resolve this launcher; ambient framework imports are refused.

## Controller accounting commands

The accounting interface initializes and inspects protected task budgets and
records controller dispositions. It does not dispatch an agent. Supply a private
owner-held JSON input file (mode 0600, one link, at most 64 KiB) outside the target
workspace. Workspace-generated proposed reviews cannot be used directly as
controller input.

~~~bash
localsetup --target-directory /work/project harness codex-heartbeat accounting init --plan --accounting-root /private/task-control --input /private/policy-input.json
localsetup --target-directory /work/project harness codex-heartbeat accounting init --apply --accounting-root /private/task-control --input /private/policy-input.json --policy-sha256 REVIEWED_PLAN_SHA256
localsetup --target-directory /work/project harness codex-heartbeat accounting inspect --accounting-root /private/task-control
localsetup --target-directory /work/project harness codex-heartbeat budget --accounting-root /private/task-control
localsetup --target-directory /work/project harness codex-heartbeat accounting review --accounting-root /private/task-control --input /private/review.json --expected-head CURRENT_HEAD_SHA256
~~~

Plan returns the canonical policy SHA-256 without creating the accounting root.
Apply requires that digest and an empty destination. Inspection returns the
current head, task policy, charged/remaining allocations, pending result, and
no-progress state. The optional budget report adds execution_accounting without
changing legacy queue accounting. No pricing input currently exists, so the
financial estimate is explicitly unavailable, not zero spend.

Policy input has exactly these fields:

~~~json
{
  "schema_version": 1,
  "workspace": "/work/project",
  "policy": {
    "schema_version": 1,
    "task": "task-id",
    "revision": "64-lowercase-hex-digest",
    "criterion": "64-lowercase-hex-digest",
    "budget": {
      "attempts": 4, "requests": 20, "tools": 64,
      "tokens": 131072, "seconds": 1440, "compactions": 2
    },
    "no_progress_limit": 2
  },
  "authorizations": {
    "operation-id": {
      "binding": "64-lowercase-hex-digest",
      "run": {"requests": 8, "tools": 16, "tokens": 32768, "seconds": 322},
      "compact": null
    }
  }
}
~~~

Digest placeholders are illustrative, not valid input. Binding must identify the
reviewed exact action; do not invent a digest or treat a prompt as authorization.
Use action-plan below to compute that binding, then select the reserved run
interface explicitly.
Compaction allocation, when authorized, replaces null with an object containing
tokens and seconds and is charged together with the run.

Review input has exactly operation, result, decision, evidence, and rationale.
Result must equal the pending execution-result digest; evidence is the SHA-256
of controller-reviewed evidence; rationale is nonempty and at most 2048
characters. Decision is progress, no_progress, or accepted. Only progress resets
the consecutive failure-to-progress counter; no decision refunds allocations.
Accepted closes local task accounting, not an external issue. This interface
cannot create a reservation or fabricate an execution result.

For accounting init, inspect, and review, a stale expected head, missing result,
changed policy, invalid private input, or unsafe record fails with exit 2 and a
generic diagnostic. These commands emit JSON and exit 0 on success; interruption
exits 130. The existing budget command retains framework error diagnostics.
After uncertain writes, inspect the current state before deciding how to reconcile it. Never replay a dispatch or
reset the directory merely because a command did not return a receipt.

### Preparing an action authorization

The provider-free action planner derives a binding instead of requiring the
controller to invent one. It does not dispatch, reserve budget, create state,
validate a saved checkpoint, or establish sandbox availability. The reserved
run interface below performs execution preflight and reservation.

~~~bash
localsetup --target-directory /work/project harness codex-heartbeat accounting action-plan --accounting-root /private/task-control --input /private/action.json
~~~

Action input is a private owned regular file outside the workspace, with the
same no-symlink, single-link and 64 KiB limits as accounting inputs. Provider
and grant files use that same stricter 64 KiB private-file contract for this
interface. The canonical binding material must also fit 64 KiB. No credentials
are read. The selected provider must advertise tools and streaming. An owned,
current PATH registration and qualified installed dispatcher are required;
missing or stale registration fails without creating state.

~~~json
{
  "schema_version": 1,
  "operation": "attempt1", "task": "task", "session": "session",
  "checkpoint": null, "profile": "coding", "prompt": "Implement the selected task",
  "executable": "/private/bin/lscli",
  "profiles": "/private/profiles.json", "grant": "/private/grant.json",
  "runtime_root": "/private/runtimes", "state_root": "/private/sessions",
  "resource_parent": "/sys/fs/cgroup/owner-delegated",
  "run": {"requests": 2, "tools": 3, "tokens": 32768, "seconds": 320},
  "compact": null, "idle_seconds": 120, "output_bytes": 1048576
}
~~~

All fields shown are required; unknown fields fail. Paths are canonical absolute
strings. Operation, task and session use the existing journal identifier syntax.
A non-null checkpoint is an explicit 64-character lowercase hexadecimal digest;
there is no implicit latest checkpoint or uncertain-operation recovery. Run
request/tool/token limits retain the accounting envelope bounds. Each phase's
seconds is an integer from 21 through 3620, allocating 20 seconds for startup
and cleanup in addition to the intended coding/compaction timeout. Idle seconds
is an integer from 1 through run seconds minus 20; output bytes is an integer
from 1024 through 4194304. Prompt is nonempty UTF-8 bounded by the input and
canonical-material file limits.

For an explicit compound action, set checkpoint and replace compact with
an object containing exactly tokens (1–1000000), seconds (21–3620),
keep_messages (0–256), and disclose_history (true). The returned envelope charges
both phases, one additional request, and one compaction before any future
execution. A successful plan does not prove that the selected history can be
compacted; the execution owner must verify that checkpoint and resulting receipt.

The result includes operation, binding, authorization, envelope, profile_sha256,
action, checkpoint, task, and session, plus schema_version. Copy authorization
under that operation in the policy input's authorizations map, then review and
initialize the complete policy. The digest binds the full action, workspace,
accounting root, protected dispatcher argv and exact grant/profile file bytes.
Changes to those inputs require a new plan. Output omits prompt and file contents.
Action-plan uses the accounting command's exit 0/2/130 and generic-error contract.

The planner consumes explicit private configuration rather than workspace hooks
or a model-editable queue. All selected paths remain outside the workspace;
runtime, session and resource roots must not overlap controller inputs,
registration, profile/grant files or accounting state. This is a planning
boundary. Reserved execution revalidates the binding and copies these exact
private input bytes for both phases; later edits to original grant/profile files
do not change the in-flight action. Existing fresh-session profiles and no-agent behavior are
unchanged.


### Running a reserved action

After action-plan and policy initialization, select exactly one operation with
all four controller options. Keep the action, policy, grants and profile files
outside the workspace; do not put authority in a model-written queue.

~~~bash
localsetup --target-directory /work/project harness codex-heartbeat run --action-input /private/action.json --accounting-root /private/task-control --expected-binding REVIEWED_ACTION_BINDING --expected-head CURRENT_ACCOUNTING_HEAD
~~~

This mode uses the existing heartbeat enabled flag, configured state directory
and overlap lock. Missing or disabled configuration skips execution; --force
permits an explicit one-off run without changing configuration. --no-agent
skips before reading configuration, private inputs or accounting state. Neither
option activates cron. Without controller options, the ordinary harness run
retains its existing profiles, hooks and transaction artifacts.

Reserved mode runs only the private action's protected phases. It does not run
workspace pre/post hooks or derive commands from a queue. Its configuration is
an owned regular YAML file bounded to 64 KiB, without symlinks, multiple hard
links or group/other write permission. Existing configuration/state ancestors
must also be owned by the user or root and not group/other writable (a root-owned
sticky temporary ancestor is allowed). The enabled field must be a boolean;
state_dir, if present, must be a relative string of at most 4096 characters.
Unsafe configuration or lock state is refused and preserved. Adjust permissions
only through an explicit owner decision; this command does not normalize them.

The captured grant must not allow writes to the configuration file or anywhere
in the configured heartbeat state directory, including through ancestor scopes
such as a whole-workspace write grant. Narrow editing scopes to the task's source
files. Process recipes operate on their existing isolated snapshots and cannot
write these host control paths. The same heartbeat lock serializes ordinary and
reserved runs using that state directory. An overlap returns locked without
reserving or dispatching work. Custom state paths and stored schedules remain
unchanged.

Output is one JSON result. Exit 0 means execution_completed or an explicit skip;
1 means locked or failed execution, 2 means invalid/unavailable input or evidence,
5 means output limit, 124 means timeout, and 130 means cancellation. Errors use
a generic diagnostic without private input contents. A successful execution
result includes the private evidence path, its exact result digest and current
accounting state. It still requires a controller progress disposition; inspect
accounting before the next attempt. Never replay after an uncertain result or
reset accounting to regain budget.

See [reserved execution details](process-control.md#reserved-execution-owner)
for input preservation, compound deadlines, checkpoint validation and failure
handling. Installed qualification is tracked separately from source-level
support; only qualified artifacts may be used for provider-backed work.

### Authorizing a later continuation

When a completed action supplies a new checkpoint, first record its controller
review, then plan a new operation using that exact checkpoint. Add the returned
authorization to the existing accounting chain; do not initialize another policy
to regain budget. Prepare a private JSON input with exactly these fields:

~~~json
{
  "operation": "next-attempt",
  "policy_sha256": "INITIAL_REVIEWED_POLICY_SHA256",
  "authorization": {
    "binding": "NEW_ACTION_PLAN_BINDING",
    "run": {"requests": 2, "tools": 3, "tokens": 32768, "seconds": 320},
    "compact": null
  }
}
~~~

Replace the digest placeholders with the initial policy plan's SHA-256 and the
new action-plan binding; copy the complete authorization object from that plan.
The compound form retains its planned compact allocation. Apply it against the
inspected current chain head:

~~~bash
localsetup --target-directory /work/project harness codex-heartbeat accounting authorize --accounting-root /private/task-control --input /private/authorization.json --expected-head CURRENT_ACCOUNTING_HEAD
~~~

This adds one immutable receipt and returns the new head for subsequent execution.
It performs no provider call or reservation. The original policy, previous grants,
charged budget and consecutive no-progress count remain unchanged. Existing
operation names cannot be replaced or reused. Pending results or reviews and
accepted/no-progress stops refuse authorization; stale heads require inspection.
The combined initial and added grant inventory is limited to 256 operations and
the complete chain to 3072 receipts. Added grants do not guarantee remaining
capacity: execution still reserves its full allocation against the original budget.
Input privacy, bounded JSON output and exit 0/2/130 follow the other accounting
commands. Missing-result reconciliation remains a separate recovery requirement.
