---
name: ls-wf-server-triage-patch-planning
description: Use when capturing a Linux server baseline, diagnosing service issues from read-only evidence, and producing a patch plan without executing changes.
metadata:
  version: "1.1"
---

# Server Triage And Patch Planning

Use this workflow to turn current host evidence into a baseline, a service triage report, and a reviewed patch plan. The workflow is planning-only: it does not open SSH sessions, run package managers, restart services, update containers, or query or invoke PatchMon.

## Workflow

1. Use `ls-system-info` to define the baseline evidence needed for the target. Consume evidence supplied by the user, or capture only unprivileged read-only evidence through access authorized separately from this workflow.
2. Use `ls-linux-service-triage` to classify the observed failure from status and log evidence. Produce a minimal fix plan; do not restart, reload, edit, or otherwise mutate the service.
3. Use `ls-linux-patcher` to generate a host or multi-host patch plan. Its bundled helper emits plans only and never executes SSH, package-manager, container, or PatchMon operations.
4. Return the baseline, triage findings, patch plan, verification criteria, maintenance constraints, and unresolved blockers. Label every command as plan output rather than executed evidence.

Stop before any command that connects to or changes a server. Missing host evidence, target identity, privilege model, maintenance constraints, or rollback information is a blocker to execution, not permission to infer it.

## Optional Manual Execution Boundary

Manual execution is outside this workflow. If the user later requests it, start a separate operation with `ls-safety-and-backup`, then use `ls-wf-risky-operations-safety-approval` and the `ls-wf-sudo-tmux-session` handoff. Before the point of risk, freeze and show:

- every exact command and value;
- every target host, service, package, and container;
- expected consequences and verification checks;
- backup evidence and an exact rollback path; and
- the immediate explicit user approval for that unchanged payload.

Tmux is transport only. It does not authorize the command. Any target, command, value, consequence, backup, or rollback change invalidates approval and requires a new point-of-risk review.

Primary reference: `ls/docs/WORKFLOW_QUICK_REF.md`.
