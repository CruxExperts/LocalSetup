---
status: ACTIVE
version: 4.22
owner_skill: ls-codex-heartbeat
---

# Harness automation

LocalSetup's harness pack provides opt-in automation scaffolding for target repositories. The shipped harness profiles are Codex heartbeat and repo-finalizer.

The important boundary is simple: selecting or installing the `harness` pack only installs the capability. It does not create `HEARTBEAT.md`, write `config/codex_heartbeat.yaml`, edit `cron/manifest.yaml`, create `.localsetup/state/codex-heartbeat/`, or schedule autonomous work. A target repo is activated only through explicit harness commands.

## Install the harness capability

```bash
localsetup install --packs harness --tools codex --yes
```

This installs `ls-codex-heartbeat`, `ls-cron-orchestrator`, and `ls-workflow-repo-finalizer` into the managed LocalSetup package library. Normal LocalSetup install behavior remains user-initiated.

## Public-repos-only rollout

Use this when you want GitHub Copilot maintenance automation on selected public
repositories without enabling it everywhere.

1. Install the `harness` pack once on the machine that will run LocalSetup.
   Installing the pack alone does not activate any repository or schedule work.
2. In each public repository you want maintained, run the explicit harness
   commands there. Activation is repo-local; a different repository stays
   inactive until you initialize or enable it separately.
3. Do not run `localsetup harness codex-heartbeat init` or `enable` inside
   private repositories unless you intentionally want automation there.
4. Pilot one public repository first. Use read-only commands and agent-free
   runs before you enable recurring work:

   ```bash
   localsetup harness codex-heartbeat plan
   localsetup harness codex-heartbeat init
   localsetup harness codex-heartbeat run --no-agent
   localsetup harness repo-finalizer plan
   localsetup harness repo-finalizer status --json
   localsetup harness repo-finalizer run --no-commit --json
   ```

5. Keep the heartbeat task scope narrow at first: dependency updates, docs
   drift, CI failures, generated-file drift, and other safe housekeeping.
6. Keep repo-finalizer in the loop so recurring work reports dirty state and
   limits checkpointing to allowlisted managed outputs.

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

## Run and inspect

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
see the [cadence contract](../skills/ls-codex-heartbeat/references/config.md#cadence-and-stored-schedules).

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

Cron entries use the registered LocalSetup source checkout through the LocalSetup Python launcher, for example:

```text
python3 /path/to/localsetup/ls/tools/localsetup.py --source-root /path/to/localsetup --target-directory /path/to/target harness codex-heartbeat run --no-agent
```

The command does not hard-code `ls/skills/...` inside the target repo.


### Agent-free transaction checks

`run --no-agent` bypasses agent profile loading and executable resolution before
building the command plan. It can validate hooks and transaction artifacts while
an agent profile is missing, invalid, or unavailable. It does not initialize an
agent SDK or provider through that skipped agent path. Configured hooks remain
explicit commands and still follow direct-command policy. A normal agent run
continues to validate its selected profile; a disabled heartbeat skips command
planning unless explicitly forced.

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
[controller accounting commands](../skills/ls-codex-heartbeat/references/config.md#controller-accounting-commands),
and [reserved execution](../skills/ls-codex-heartbeat/references/config.md#running-a-reserved-action).
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

Use [action-plan](../skills/ls-codex-heartbeat/references/config.md#preparing-an-action-authorization)
to derive the exact binding, then initialize the reviewed policy and dispatch
one explicit reserved action. A later checkpoint continuation requires
[adding authorization to the same chain](../skills/ls-codex-heartbeat/references/config.md#authorizing-a-later-continuation);
compaction does not reset budgets or restore saved permissions.
[Result acknowledgement recovery](../skills/ls-codex-heartbeat/references/recovery.md#reserved-result-acknowledgement-recovery)
verifies retained evidence without rerunning the action. Missing or uncertain
evidence remains a blocker. These interfaces neither activate recurring work
nor establish provider, host, or exact-release qualification.
