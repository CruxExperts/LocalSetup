---
status: ACTIVE
version: 3.4
---

# Heartbeat Recovery

A fresh ordinary run acquires `heartbeat.lock` before it scans `*.staged` directories or reads `active.json`. Any staged run left after a prior owner is finalized as `failed_recovered`, preserved under a `*.recovered-*` directory, and recorded in the next run manifest.

An existing lock stays authoritative unless all of these hold: its JSON has a creation time, same-host name, and positive PID; the recorded age meets `heartbeat.stale_after_seconds`; and the same-host PID no longer exists. Only then does the harness acquire the old inode lock, unlink that held stale pathname, close the old descriptor, and retry exclusive creation. It never renames a locked inode. Cross-host, malformed, unreadable, permission-denied, future-dated, young, or live-PID locks return `status: locked` without changing state.

## Ambiguous lock procedure

1. Run `localsetup harness codex-heartbeat status` and preserve the reported lock evidence.
2. Confirm with the lock owner or host operator that no heartbeat process owns the recorded PID. Age alone is not sufficient.
3. Do not delete or rename the lock manually. The runtime reclaims only a proven stale lock while it holds that inode; any ambiguous lock remains a stop condition.
4. If ownership cannot be established, leave the lock in place and investigate.

Corrupt or unsafe pointers also stop the ordinary run instead of being silently trusted.

Reserved execution shares the overlap-lock checks but does not scan or recover
ordinary staged transactions. Its result/accounting recovery follows the separate
procedure below. Reserved `--no-agent` skips before either recovery path; it is
not evidence that an ordinary transaction check ran.

## Reserved result acknowledgement recovery

A reserved action can finish and preserve its private result while accounting
still reports reconciliation_required. Inspect accounting and the retained
state-root/heartbeat/action-binding/result.json before taking further action.
If that exact result exists, review its SHA-256 and use the original private
action input to join it to the pending reservation:

~~~bash
localsetup --target-directory /work/project harness codex-heartbeat accounting reconcile --accounting-root /private/task-control --input /private/action.json --expected-binding ORIGINAL_ACTION_BINDING --expected-head CURRENT_ACCOUNTING_HEAD --result-sha256 REVIEWED_RESULT_SHA256
~~~

The command revalidates the action and its original protected registration,
checks its frozen profile/grant bytes and private result file, and requires the
same pending operation, authorization and current accounting head. Completed
results additionally require successful process/protocol evidence and the exact
settled profile-bound session checkpoint; compound completion also requires the
matching private compaction receipt and source/destination checkpoints at the
same settled historical journal prefix. Historical checkpoints confer no new
resume authority; the final coding checkpoint must remain current. It appends
only the missing result record,
retains every charged allocation, and returns awaiting_controller_review. Record
a separate controller progress disposition afterward. Execution completion does
not accept an issue or authorize another attempt.

No agent phase or provider request is dispatched. If the result was already
recorded, inspect the new head and review that recorded result instead. Missing,
changed, unsafe or unverifiable evidence leaves the reservation unresolved; do
not replay work, reset accounting or manufacture a replacement result. A failed
retained outcome is recorded as failure, without claiming a settled session;
any later resume still requires the session owner's uncertainty checks.

The original action inputs and runtime registration must still produce the
original binding. After an upgrade or input change, recover the exact compatible
prior selection or preserved input through its owning recovery procedure before
retrying reconciliation. This command does not migrate bindings or choose a
replacement runtime. It follows the accounting command's private-file, generic
error and exit 0/2/130 contract. Existing heartbeat overlap locks and ordinary
transaction recovery remain separate from this accounting operation.
