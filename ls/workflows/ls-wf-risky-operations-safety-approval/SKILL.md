---
name: ls-wf-risky-operations-safety-approval
description: Use when risky operations need approval checkpoints, impact review, or guarded execution; hand off sudo, elevated, PTY, or interactive password execution to ls-wf-sudo-tmux-session.
metadata:
  version: "1.0"
---

Use this workflow package to establish the applicable authorization and bound
risky operations before they run. Reuse the user's current request or an existing
grant when it covers the operation. Compose `ls-system-changes-backup`; preserve
its risk, backup, and rollback controls.

Primary references: `ls/skills/ls-system-changes-backup/SKILL.md` and
`ls/workflows/ls-wf-sudo-tmux-session/SKILL.md`.

## Required gate sequence

1. Identify the applicable user authorization, its named targets, permitted scope,
   consequences, recovery strategy, and any lifetime or exact-command conditions.
   Classify the operation as LOW, MEDIUM, HIGH, or CRITICAL under
   `ls-system-changes-backup`. Record affected users, services, files, and persistent
   state. Prefer a dry run when supported. Risk classification does not itself
   require another approval when the existing authorization covers the operation.
2. Before changing sensitive configuration or persistent state, create and
   verify the required backup and record the exact rollback or restore action.
   If a backup is not applicable or the user declines one, record the reason
   and residual risk before continuing.
3. Prepare one execution record containing the exact command or edit, target,
   values, risk class, likely consequences, affected scope, backup result,
   rollback action, and safer manual option when one exists. Bind it to the
   applicable authorization. Complete this preparation before asking for any
   missing approval so the user can review the actual operation.
4. Immediately before execution, check that authorization still covers the
   operation and has not expired or been revoked. Reuse it for routine command,
   path, value, or version choices within its scope; update the execution record
   without another user round trip. Obtain explicit user approval when authority
   is missing or the targets, destructive scope, consequences, recovery, or risk
   materially exceed it. A grant explicitly restricted to exact commands or
   values remains restricted to those commands or values. Native client
   permissions and authentication remain effective and cannot be bypassed.
5. Execute only the authorized operation. Capture the result and verify the
   intended state without widening its scope. Existing authority does not become
   an indefinite grant for other work. Revocation stops future dispatch.
   Continuing after owner exit requires a separately authorized durable-job
   contract.

## Privileged or interactive handoff

When the authorized operation needs `sudo`, root/admin elevation,
`require_escalated`, pseudo-terminal/PTY handling, or an interactive password
prompt, hand execution to `ls-wf-sudo-tmux-session` only after all four
pre-execution gates pass and authorization still covers the recorded operation.
That check reuses valid authority; it does not impose a fresh approval ceremony.

The tmux workflow owns session selection, readiness probes, authentication,
any `sudo ready` exchange, execution, status, and cancellation. Never collect the
password, bypass its probe, or use the handoff to widen the authorized scope.
