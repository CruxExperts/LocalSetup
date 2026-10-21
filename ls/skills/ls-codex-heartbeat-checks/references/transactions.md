---
status: ACTIVE
version: 3.4
---

# Transactions

The ordinary heartbeat run uses this file transaction:

1. Acquire `heartbeat.lock`, or return current owner evidence without touching state.
2. Recover abandoned staged runs only while holding the lock.
3. Create `runs/<run-id>.staged`.
4. Write `active.json`.
5. Run hooks and the optional configured agent command.
6. Write the result, command log, sidecars, and manifest hashes.
7. Validate the staged artifact graph.
8. Atomically promote to `runs/<run-id>`, update `latest.json`, and remove `active.json`.

If validation fails, the staged run is not promoted as successful. Future ordinary
lock-owning runs preserve abandoned staged work as recovered failure evidence.

[Reserved actions](config.md#running-a-reserved-action) use the same overlap lock
with separate protected reservation/result accounting. They do not run hooks or
perform the staged transaction above; reserved `--no-agent` skips before lock
acquisition. Follow [reserved execution](process-control.md#reserved-execution-owner)
and [result reconciliation](recovery.md#reserved-result-acknowledgement-recovery)
for that route. Ordinary recovery preserves failure evidence but does not block
subsequent configured runs pending effect reconciliation. Operators must reconcile
uncertain effects before repeating a command; reserved execution enforces its
separate reconciliation gate.
