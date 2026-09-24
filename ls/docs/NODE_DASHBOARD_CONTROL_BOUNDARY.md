---
status: ACTIVE
version: 5.6
owner_skill: ls-system-design
---

# Node dashboard control boundary

## Decision

A future Node dashboard is an optional human-facing control plane, not a LocalSetup executor, terminal emulator, queue worker, or agent router. It renders bounded telemetry and submits explicit capability requests to one authenticated, node-local target helper. The browser never connects directly to Herdr sockets, tmux, WezTerm, SSH, queue storage, Agent Q/A2A endpoints, or OmniRoute credentials.

The target helper is the sole translation boundary between browser-safe requests and approved LocalSetup/Herdr-facing operations. It must use existing source-owned command surfaces and authenticated target configuration; it must not scrape terminals, rely on undocumented internal sockets, or create a second remote-control protocol.

## Trust and deployment boundary

```text
Browser ── same-origin HTTPS ──> Node dashboard ── typed local IPC/HTTPS ──> target helper
                                                                    │
                                                                    ├─ bounded LocalSetup status/plan APIs
                                                                    └─ approved Herdr remote attach integration
```

The dashboard and helper run on the same trusted node unless a later deployment design introduces mutually authenticated transport. A browser request is not authority to access another machine. Remote attachment remains a target-helper concern and must use the supported Herdr remote attach model, never browser-originated SSH, tmux, or terminal-emulator control.

The dashboard must not receive raw shell commands, arbitrary filesystem paths, environment maps, secrets, credentials, model-provider keys, terminal output, agent prompts, or queue payload bytes. It exposes relative logical identifiers and bounded status/error codes only.

## Capability request contract

A dashboard request is declarative, allowlisted, and auditable:

```json
{
  "request_id": "opaque-id",
  "target_id": "configured-target",
  "capability": "read_status | plan_operation | request_operation",
  "operation": "registered-operation-or-null",
  "parameters": {},
  "idempotency_key": "opaque-id"
}
```

`target_id`, `capability`, `operation`, and parameter schema are resolved against a node-local static allowlist. Unknown fields, arbitrary commands, executable paths, hostnames, ports, credentials, and nested unbounded objects are rejected. A request for a mutating operation creates an explicit pending action; it cannot silently execute because a browser rendered a button. Authentication, authorization, confirmation, and audit retention remain helper-owned concerns.

The corresponding result is intentionally small:

```json
{
  "request_id": "opaque-id",
  "state": "accepted | pending_confirmation | completed | rejected | failed",
  "code": "stable-safe-code",
  "telemetry": {},
  "updated_at": "RFC3339 timestamp"
}
```

The result contains neither command text nor raw process output. Error telemetry is bounded and redacted before it crosses the helper boundary.

## Read-only telemetry

The dashboard may poll or subscribe to a normalized status envelope containing configured target state, available capabilities, queue depth/counts, operation state, and timestamps. Streaming is optional; a polling implementation is the baseline. The helper owns rate limits, pagination bounds, and redaction. A dashboard does not read the trusted-work-queue filesystem or context-index database directly.

Operational state is a convenience view, not the controller ledger. Accepted work, approvals, provenance, and restart checkpoints belong to the private controller ledger defined in [GLOBAL_HANDOFF_LEDGER.md](GLOBAL_HANDOFF_LEDGER.md). Queue provenance remains governed by [TRUSTED_WORK_QUEUE.md](TRUSTED_WORK_QUEUE.md).

## Explicit non-goals

This contract does not add a Node dependency, dashboard service, HTTP endpoint, daemon, remote-control channel, Herdr API wrapper, agent dispatch mechanism, queue executor, model selection surface, or credential store. It does not change the existing `localsetup harness` surface.

A delivery change must first define the target-helper protocol, configured-target authorization, CSRF/session handling, rate limits, output redaction, audit schema, health behavior, and focused black-box tests. It must preserve the boundary in [FRAMEWORK_LIBRARY_ARCHITECTURE.md](FRAMEWORK_LIBRARY_ARCHITECTURE.md): dashboard capability requests and bounded telemetry stay separate from queue transport, Herdr/tmux/WezTerm control, Agent Q/A2A, and OmniRoute routing.
