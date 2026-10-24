# Cron Manifest Reference

`cron_ctl.py` and `run_trigger.py` read the same YAML manifest and validate it before listing, installing, or running tasks. Validation errors include the manifest path location, such as `tasks[0].command`, so agents can fix the exact field.

## Schema

```yaml
triggers:
  midnight-utc:
    schedule: "0 0 * * *"
  after-boot:
    on_boot_delay_minutes: 5
tasks:
  - id: snapshot-daily
    trigger: midnight-utc
    sequence_order: 1
    command:
      - python3
      - ls/skills/ls-system-baseline-diagnostics/scripts/system_snapshot.py
      - --output-basename
      - reports/system-snapshots/daily
    enabled: true
    timeout_seconds: 3600
```

## Validation Rules

- `triggers` must be a non-empty mapping.
- Trigger names and task ids may contain letters, numbers, `.`, `_`, `:`, `@`, `+`, and `-`.
- A trigger must define exactly one of `schedule` or `on_boot_delay_minutes`.
- `schedule` must contain exactly five fields. Each field accepts only numeric values, `*`, comma-separated lists, inclusive ranges, and `/` steps; ranges and values must be in the Linux cron field range.
- `on_boot_delay_minutes` must be an integer from 0 to 1440.
- `tasks` must be a list when present.
- Each task must include `id`, `trigger`, `sequence_order`, and `command`; `sequence_order` is an integer from 0 to 86400.
- `enabled`, when present, must be boolean.
- `timeout_seconds`, when present, must be an integer from 1 to 86400.

## Readiness Report

Run `cron_ctl.py --manifest PATH readiness [--repo-root PATH] [--trigger NAME] [--log-dir PATH]` for a read-only check before enabling or preparing an automation. It reports each observable check as `ready`, `blocked`, or `unchecked`, followed by next actions. The command exits successfully after printing the report; read its overall status rather than treating exit code zero as proof of readiness.

The report checks that the manifest is readable and valid, the selected trigger exists, the runner and generating Python interpreter are available, each task's first command is present in the current user's PATH or executable at the selected working directory, and the working directory is readable/searchable. Generated runner commands use that interpreter with `-I -B` to keep ambient imports and bytecode writes out of the installed runtime. For task executables, relative and empty PATH entries are resolved from `--repo-root`, matching the runner's working directory. When `--log-dir` is supplied, it checks whether the runner could create or append its protected log directory and file without making changes. Checks using current PATH and permissions describe the account running the report; they do not establish the crontab owner's access.

The overall status is `blocked` when an observed prerequisite fails and `unchecked` when there are unresolved checks. It is `ready` only when all reported checks are ready. Target cron service/account details and task-specific dependencies, settings, input/output paths, secrets, and recovery arrangements remain unchecked because the report does not run workloads or infer their requirements. Task `enabled` flags and run history are displayed separately as not assessed and do not affect readiness. The report never runs a task, writes the manifest, changes permissions, creates log paths, or installs cron.

## Command Model

Commands are executed with `shell=False`. Prefer an argv list so each argument is explicit and no shell parsing is needed.

String commands are accepted for simple cases, but they are parsed with `shlex.split` and rejected when they contain shell-only operators such as `&&`, `||`, `;`, `|`, redirects, backticks, `$(`, or `${`. Use an argv list for literal arguments that contain shell-looking characters.

Generated cron lines quote every runner argument with `shlex.join` and escape `%`, which cron treats as a newline marker in command fields. On-boot delays are passed to `run_trigger.py --delay-seconds` instead of interpolating a `sleep ... &&` shell fragment. The generated file is a user-crontab fragment for review and manual merge; `cron_ctl.py` never installs it, and it does not produce `/etc/cron.d` content.

## Runner Logs

Cron hosts do not always have an MTA or other stdout/stderr sink. Use
`cron_ctl.py install --log-dir PATH` to add `run_trigger.py --log-dir PATH` to
generated cron lines. When enabled, `run_trigger.py` appends timestamped `runner_start`, `task_start`, `task_exit`, timeout/error, and `runner_exit` records to `PATH/<trigger>.log`.

Task exit records include bounded byte tails decoded with replacement, while child stdout and stderr are relayed as they arrive. If an inherited output sink fails, the runner keeps draining both child pipes, records `task_output_error`, and exits with status 1.

Store runner logs in a protected directory because stdout/stderr tails may contain secrets, tokens, local paths, or other sensitive operational data emitted by child commands. On POSIX, the runner requires log directories and existing log files to deny group and other access before it appends data.
