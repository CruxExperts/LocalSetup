---
status: ACTIVE
version: 4.72
owner_skill: ls-codex-heartbeat-checks
---

# Harness automation

LocalSetup's harness pack provides opt-in automation scaffolding for target repositories. The shipped harness profiles are Codex heartbeat and repo-finalizer.

The important boundary is simple: selecting or installing the `harness` pack only installs the capability. It does not create `HEARTBEAT.md`, write `config/codex_heartbeat.yaml`, edit `cron/manifest.yaml`, create `.localsetup/state/codex-heartbeat/`, or schedule autonomous work. A target repo is activated only through explicit harness commands.

## Install the harness capability

```bash
localsetup install --packs harness --tools codex --yes
```

This installs `ls-codex-heartbeat-checks`, `ls-cron-job-scheduling`, and `ls-wf-repository-status-checkpoint` into the managed LocalSetup package library. Normal LocalSetup install behavior remains user-initiated.

## Activate a target repo

Preview:

```bash
localsetup harness codex-heartbeat plan
```

Initialize local target files with heartbeat disabled:

```bash
localsetup harness codex-heartbeat init
```

`init` creates:

- `HEARTBEAT.md`
- `config/codex_heartbeat.yaml`

The generated config starts with `heartbeat.enabled: false`.

Connect an existing installed LSCli runtime and provider profile with a read-only
preview, then repeat with `--apply` to save the selection:

```bash
localsetup harness codex-heartbeat configure-lscli --mode review \
  --provider-profile heartbeat --grant /private/heartbeat-grant.json \
  --context HEARTBEAT.md --context inventory/health.json
```

Use `--executable`, `--profiles` and `--runtime-root` for nondefault installations.
The defaults are the current user's registered `~/.local/bin/lscli`, provider
document and runtime root under `~/.local/share/localsetup`. The named provider
retains its configured credential source; configuration does not resolve secrets,
read context files or contact the provider. Launcher and provider metadata checks
are setup checks, not proof of credential, grant or execution readiness.

`--mode coding` instead requires `--resource-parent` and accepts `--state-root`;
it does not accept review `--context` inputs. Both modes select the corresponding
LSCli agent template for manual runs and retain that template's existing prompt
and limits. Hooks, other profiles, heartbeat cadence and scheduled mode remain
unchanged. Unknown content occupying a template's name is rejected.

`--apply` saves a verified compressed copy of the exact prior configuration under
the target's heartbeat state directory in `configuration-backups/`, then replaces
the config with owner-only YAML. Comments and formatting remain in the backup.
Unchanged applications create no backup. An optional
`--expected-config-sha256` binds application to the preview's configuration digest.
Cooperating configuration writers serialize; observed concurrent edits are
rejected. This is not a compare-and-swap guarantee against unrelated writers.
Changing an enabled recurring agent is refused: first use
`enable --collection-only` or `disable`, configure the connection, and explicitly
select recurring reviews again when ready. Configuration never installs cron.
Configuration must be an owned regular file with no shared writes, reached
through nonsymlinked ancestors with safe ownership and permissions; unsafe paths
are refused without changing their permissions.

Enable the target heartbeat and upsert the cron manifest:

```bash
localsetup harness codex-heartbeat enable
```

`enable` first validates a faithfully representable integer cron interval (minute
divisors of 60, or whole-hour divisors of 24), then flips `heartbeat.enabled` to `true`, creates or updates `cron/manifest.yaml`, preserves unrelated triggers and tasks, and validates the manifest through the cron orchestrator.

Installing the live crontab is a separate explicit step:

```bash
localsetup harness codex-heartbeat enable --install-crontab --yes
```

`--install-crontab` without `--yes` is rejected.

New schedules collect data without launching an agent. After configuring and
enabling the desired agent profile, use `enable --with-agent` to select recurring
reviews, or `enable --collection-only` to return to collection only. These flags
do not install a live schedule unless `--install-crontab --yes` is also supplied.
Bare enable and disable preserve the selected mode. `status` reports it as
`scheduled_agent`; profile validation during enable does not prove provider or
sandbox readiness.

The live installer merges only this target's heartbeat block into the current
user's crontab. Other jobs remain in place. It keeps a compressed prior-crontab
backup and checks readback even when the write acknowledgement is lost. Unknown
outcomes retain a pending record and are observed before any later update;
they are never automatically replayed. Duplicate markers, orphaned blocks and
manual changes to an owned block require inspection. Legacy unmarked entries
remain untouched and need an explicit migration to avoid duplicate scheduling.
Disabling with `--install-crontab --yes` removes the owned block.

## Run and inspect

Inspect the selected LSCli agent without resolving credentials, reading context
contents, running hooks or creating runtime state:

```bash
localsetup harness codex-heartbeat readiness
```

The report distinguishes blocked configuration, registration, runtime/dependency,
provider-profile and grant checks. Review inputs need separate read and disclosure
authority. Coding also requires native-bundle metadata, separate private state
paths and an owned resource delegation. `static_verified` means those static
checks passed; credentials, provider execution and native execution remain
unverified. The selected agent can be inspected while disabled. Activation flags
are reported separately; inspection never changes them.

Use `readiness --probe` for one explicit bounded attempt. Review probes disclose
the selected granted context files to the selected provider and request a brief
schema-checked acknowledgement. Coding probes use the normal native/resource
preflight and private run journals, with one provider request and a zero model-tool
budget. The supervisor independently refuses model tool calls when that budget
is exhausted; checkpoint persistence is still available. Probe limits never
increase configured limits: at most 30 seconds of selected execution and 64 KiB
of process output, plus the existing 20-second bootstrap allowance and bounded
termination cleanup. Review allows at most 256 output tokens; coding allows at
most 4,096 total tokens, including input and tool schemas. Reported usage can
identify an excess only after a response, so this is not a billing guarantee.
Configuration and normal run limits are not rewritten.

`probe_passed` proves only that bounded attempt. Collection hooks, live cron,
future runs and actual coding tool actions remain unverified. Probe results omit
raw context, model output and provider diagnostics. Failures do not trigger
retries or fallback; a request may already have been sent. Resolve the reported
prerequisite or reconcile private run evidence before choosing another attempt.
When available, failed reviews retain the validated `completion_status`, such as
`unavailable`, `provider_error` or `schema_rejected`, with a relevant next step.
This is a protocol classification, not a raw HTTP error or proof of its exact
cause. Fixed process termination reasons and `cleanup_reaped` also remain visible;
unconfirmed cleanup prevents a successful probe result. Unknown status text,
response bodies and command output are never copied into these diagnostics.
Neither inspection nor probing activates scheduling or runs collection hooks.

Run without model use:

```bash
localsetup harness codex-heartbeat run --no-agent
```

Check state:

```bash
localsetup harness codex-heartbeat status
```

Inspect read-only task budget:

```bash
localsetup harness codex-heartbeat budget
```

`budget` reads `config/codex_heartbeat.yaml` and, when configured, a repo-local YAML task queue from `heartbeat.task_queue_path`. It reports policy, summary, and task reservations as JSON. It does not spawn agents, enforce scheduling, commit changes, or activate heartbeat.

Disable without removing history:

```bash
localsetup harness codex-heartbeat disable
```

`disable` flips config back to disabled and disables the heartbeat cron task,
preserving its stored trigger schedule and historical artifacts. Cron is a
wall-clock schedule in the daemon’s configured timezone, not an elapsed timer;
see the [cadence contract](../skills/ls-codex-heartbeat-checks/references/config.md#cadence-and-stored-schedules).

## Repo finalizer profile

Use repo-finalizer to report dirty Git state and optionally stage or checkpoint only allowlisted managed outputs.

Plan and status are read-only:

```bash
localsetup harness repo-finalizer plan
localsetup harness repo-finalizer status --json
```

Run with no mutation:

```bash
localsetup harness repo-finalizer run --no-commit --json
```

Checkpoint commit is explicit and gated:

```bash
localsetup harness repo-finalizer run --checkpoint --message "chore: checkpoint managed outputs"
```

Policy defaults come from built-in settings. If present, `config/localsetup_finalizer.yaml` overrides classification and stage allowlists. Repo-finalizer never runs `git push`, `git reset`, or delete/revert commands.

`run` writes the latest JSON and text reports under the repo-local ignored state directory:

```text
.localsetup/state/repo-finalizer/latest.json
.localsetup/state/repo-finalizer/latest.md
```

When only runtime-ignored finalizer state is present, status reports `clean_except_ignored` instead of blocking cleanup.

## Runtime artifacts

Runtime state stays in the target repo under:

```text
.localsetup/state/codex-heartbeat/
```

Each run starts under `runs/<run-id>.staged`. The harness writes `manifest.json`, `heartbeat-result.json`, and `command-log.json`, validates artifact hashes, then atomically promotes the staged directory to `runs/<run-id>`. A run is not successful until that validation and promotion finish.

Pointers stay relative to the state directory:

- `active.json` tracks a currently staged run.
- `latest.json` points to the most recently promoted run and records the manifest hash.
- `heartbeat.lock` prevents concurrent runs.

Interrupted staged runs are preserved as recovered failures before a fresh run starts.

## Command and agent boundary

Hooks and launch commands run in serial order with explicit timeouts, stdout/stderr tails, sidecar JSON logs, PID/process group/session metadata where available, and manifest hashes. Hooks plus `direct-argv` and `resolved-path` agent profiles run with `shell=False`; `shell-login` is explicit opt-in compatibility mode and records the rendered command.

Direct hook policy blocks `git commit`, `git push`, and common destructive executables unless the target config explicitly allows them.

Agent execution is additionally constrained by the configured agent profile, launcher mode, selected agent-client configuration, sandbox, and approval settings. The heartbeat harness records and gates execution; it does not replace an agent client's sandbox or approval model.

## Cron launcher

Cron entries use the owning LocalSetup installation and its Python interpreter,
without resolving an ambient LocalSetup command from PATH, for example:

```text
/path/to/python -I -B /path/to/localsetup/ls/tools/localsetup.py --source-root /path/to/localsetup --target-directory /path/to/target harness codex-heartbeat run --scheduled
```

The command does not hard-code `ls/skills/...` inside the target repo. It reads
the persisted scheduled mode on each invocation, so a previously generated
command also respects a later switch to collection only or disabled state.
Regenerate schedules when relocating the owning installation.


### Agent-free transaction checks

`run --no-agent` bypasses agent profile loading and executable resolution before
building the command plan. It can validate hooks and transaction artifacts while
an agent profile is missing, invalid, or unavailable. It does not initialize an
agent SDK or provider through that skipped agent path. Configured hooks remain
explicit commands and still follow direct-command policy. A normal agent run
continues to validate its selected profile; a disabled heartbeat skips command
planning unless explicitly forced.

Place routine inventory and health collection in `hooks.before`. Those hooks
finish before agent resolution, so a missing profile or unavailable runtime
cannot prevent fresh collection. An agent failure still makes the run fail and
skips subsequent hooks, while preserving completed collection sidecars and
artifacts. Repeated runs have distinct identifiers even within the same second
and process.

Cron validation, fragment generation and the generated runner preserve the
owning Python interpreter with isolated imports and bytecode disabled. They do
not add Python cache files to a sealed SDK runtime. Configured workload commands
retain their explicit arguments; this isolation applies to the framework helpers.

The explicit `lscli-review` profile provides a tool-free advisory mode when
autonomous tooling is unavailable. It sends bounded, separately granted local
observations to the selected provider and stores a private structured review.
It requires the verified SDK runtime but no native tool sandbox; the supervised
Python worker is not a filesystem sandbox. It never automatically replaces or
retries a failed coding run. See the [read-only review configuration](../skills/ls-codex-heartbeat-checks/references/config.md#read-only-lscli-review).

### Shared execution accounting foundation

The internal heartbeat accounting owner folds ordered reservation, result, and
controller-review events against a task revision and acceptance criterion.
It charges full allocated request, tool, token, runtime, attempt, and compaction
limits. A compound compaction-and-coding reservation includes one compaction
request, zero compaction tools, and both phases' token/runtime allocations
before either phase can dispatch. Failed or uncertain attempts receive no refund.

An unresolved reservation requires reconciliation, never automatic replay.
A recorded result waits for a controller disposition bound to its digest and
evidence. Only reviewed progress resets the consecutive no-progress counter;
it never refunds total allocations. Repeated no-progress dispositions stop new
attempts even if operation/session identities change or compaction is requested.
Acceptance stops the task, separately from any authorized external issue closure.

These state-transition rules are integrated with protected durable storage,
[controller accounting commands](../skills/ls-codex-heartbeat-checks/references/config.md#controller-accounting-commands),
and [reserved execution](../skills/ls-codex-heartbeat-checks/references/config.md#running-a-reserved-action).
Ordinary fresh-profile runs without a reserved action and legacy queue reports
do not enforce this task-wide accounting policy. Streaming activity and model
claims cannot create a controller disposition. Allocated tokens are enforceable
resource reservations, not proof of provider billing; any financial projection
must be labeled an estimate.

### Protected accounting records

The internal storage owner creates a reviewed policy once in an owner-controlled
directory outside the workspace. Its immutable policy binds the workspace, task
revision, acceptance criterion, budgets, and explicit operation authorizations.
Each authorization binds an action digest and its exact coding/compaction
allocations. Reservation rejects a mismatched action or allocation.

Canonical private files and existing anchored path/lease helpers protect the
policy and append-only hash chain. Writers compare the expected current head
under an exclusive directory lease; stale callers must inspect before retrying.
Reads create no state. Changed ownership, modes, links, unexpected files, broken
chains, and malformed records are refused. Existing records are never reset or
overwritten to recover budget.

If publication succeeds but its acknowledgement fails, inspection still finds
the charged reservation. It remains reconciliation-required; another dispatch
is not authorized by retrying the write. Controller review records bind the
result and evidence and never refund allocations. The storage owner remains an
internal interface composed by the public action planner, controller commands
and reserved dispatcher.

Use [action-plan](../skills/ls-codex-heartbeat-checks/references/config.md#preparing-an-action-authorization)
to derive the exact binding, then initialize the reviewed policy and dispatch
one explicit reserved action. A later checkpoint continuation requires
[adding authorization to the same chain](../skills/ls-codex-heartbeat-checks/references/config.md#authorizing-a-later-continuation);
compaction does not reset budgets or restore saved permissions.
[Result acknowledgement recovery](../skills/ls-codex-heartbeat-checks/references/recovery.md#reserved-result-acknowledgement-recovery)
verifies retained evidence without rerunning the action. Missing or uncertain
evidence remains a blocker. These interfaces neither activate recurring work
nor establish provider, host, or exact-release qualification.
