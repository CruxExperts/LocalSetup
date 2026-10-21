---
status: ACTIVE
version: 3.4
---

# Heartbeat Artifacts

Ordinary transaction artifacts live under the configured heartbeat state directory
in the target repository (default `.localsetup/state/codex-heartbeat/`).

Each ordinary run starts as `runs/<run-id>.staged`. Promotion requires valid hashes for `manifest.json`, `heartbeat-result.json`, `command-log.json`, and every command sidecar referenced by the command log.

Pointers and lock evidence:

- `active.json`: points at the staged run while work is active.
- `latest.json`: points at the most recently promoted run and records the manifest hash.
- `heartbeat.lock`: prevents concurrent runs.

Pointers and artifact references must stay relative to their heartbeat-state base. Absolute paths and parent traversal are rejected.

[Reserved actions](config.md#running-a-reserved-action) share the heartbeat overlap
lock but do not create this staged artifact graph. Their private attempt result
lives at `state_root/heartbeat/binding/result.json`, with policy and charged
reservations under the explicit accounting root. See the
[reserved execution owner](process-control.md#reserved-execution-owner) and
[result acknowledgement recovery](recovery.md#reserved-result-acknowledgement-recovery)
for evidence validation and uncertainty handling.
