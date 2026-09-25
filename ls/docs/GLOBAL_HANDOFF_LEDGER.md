---
status: ACTIVE
version: 4.44
owner_skill: ls-framework-compliance
---

# Compact global handoff ledger

## Decision

Each controller keeps one private, append-only handoff ledger for a broad, delegated, risky, validation-heavy, or restart-sensitive objective. It records accepted evidence and decisions; it is not a task queue, chat transcript, event stream, transport receipt store, database, or agent runtime.

The ledger is deliberately separate from the client-state artifact contract. Its repository/ref binding is authoritative at resume time; a client capability snapshot may be cited as evidence but does not allocate the ledger path.

## Location and identity

Create every new controller ledger at `.agents/state/<task-slug>/ledger.md`. The controller assigns `<task-slug>` once for the Git-bound objective as `<YYYYMMDD-HHMMSS>-<git-short-sha>-<objective-slug>` and every participating agent and tool reuses that exact directory. Task-local evidence, reviews, handoffs, and transient state may live beside `ledger.md`; no agent- or client-specific run root is created.

The ledger header records the repository and initial immutable ref. Existing `.codex/`, `.omp/`, and `.localsetup-maint/` records are historical evidence: leave them in place, but do not create new task state there. `.agents/state/` is ignored by default.

## Record shape

The ledger contains a stable objective header followed by ordered immutable records:

```yaml
ledger_version: 1
objective: concise outcome
source_authority: repository and versioned references
constraints: [bounded, non-secret invariants]
records:
  - sequence: 1
    at: UTC timestamp
    owner: controller
    state: accepted | blocked_external | decision | evidence
    binding:
      repository: .
      head: immutable git revision
      artifact_sha256: optional hash
      context_freshness: optional fresh/stale summary
      client_capability_snapshot: optional artifact reference
    changed: [safe semantic facts]
    evidence: [test, review, or receipt identifiers]
    approvals: [non-secret approval references]
    next_safe_action: bounded action or terminal stop condition
```

Records use stable ordering. `sequence` never resets for an objective. Prior evidence is never rewritten; a correction appends a linked record. The controller alone accepts, blocks, or closes a slice.

## Privacy and boundary rules

The ledger must not contain secrets, credentials, raw prompts, raw commands, environment values, absolute paths, terminal transcripts, queue payloads, PRD bytes, model/provider routing data, or user private messages. It records identifiers, hashes, relative artifact references, safe status codes, and concise decisions only.

Agent Q ledgers, trusted-queue packet manifests, command telemetry, context-index run logs, and client-native state remain their own authoritative records. The controller ledger may cite their stable digest or safe receipt identifier but never duplicates their payload.

## Resume protocol

Before resuming, the controller verifies the ledger artifact, current repository/ref, and the last accepted record. It verifies a capability snapshot, sidecar, context-freshness record, or task-owned diff only when the ledger cites that specific evidence. A mismatch creates a new `decision` or `blocked_external` record; it never silently reuses stale authority.

A restart artifact may summarize the ledger for a specific client, but it is a derived handoff payload. The ledger remains the durable evidence source.

## Non-goals

This design does not add a daemon, cross-machine replication, a generic event bus, native slash-command behavior, task dispatch, remote control, or client-native persistence. The shared private controller-ledger root is the explicit exception: it is `.agents/state/` as defined above, not a registered client-state root.
