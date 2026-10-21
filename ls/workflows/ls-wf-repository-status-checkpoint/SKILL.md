---
name: ls-wf-repository-status-checkpoint
description: Use when safely inspecting repo dirty state and optionally checkpointing allowlisted managed outputs without destructive git operations.
metadata:
  version: "1.0"
---

Use this workflow package when a target repository needs an explicit finalization pass that classifies dirty files and can stage or checkpoint only configured allowlisted outputs.

Primary reference: `ls/docs/HARNESS_AUTOMATION.md`.
