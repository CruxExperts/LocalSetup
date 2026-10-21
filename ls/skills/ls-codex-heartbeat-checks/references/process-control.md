---
status: ACTIVE
version: 3.4
---

# Process Control

Commands are launched with a new session where the host supports it. On timeout, the harness terminates the process group, waits briefly, then kills the group if needed.

Direct command policy blocks common destructive executables plus `git commit` and `git push` by default, including Git commands with supported global options before the subcommand. Hooks plus `direct-argv` and `resolved-path` profiles run with `shell=False`; `shell-login` is explicit opt-in compatibility mode and records the rendered command.

The heartbeat runtime executes only the configured agent profile’s argv and records its client label and launcher metadata. It does not supply client-specific model, sandbox, approval, or authentication semantics, and it is not a sandbox replacement.

## Bounded process I/O

The runner drains stdout and stderr incrementally with a combined 4 MiB output
budget. Exceeding it stops the command with termination_reason=output_limit;
sidecars retain at most 12,000 decoded characters per stream. Prompt stdin is
limited to 128 KiB of UTF-8 and is closed after delivery. Commands without an
explicit prompt receive closed input instead of inheriting the controller's
terminal. The monotonic timeout includes stdin delivery, output draining, and
waiting for process exit.

Timeout, SIGINT, and SIGTERM stop the original process group with TERM and then
KILL, with bounded grace and reap waits. Sidecars distinguish the effective
returncode (124 for timeout, 130/143 for cancellation) from the observed
process_returncode. cleanup_reaped reports only whether the direct child was
reaped; it does not prove that an escaped descendant has terminated. A generic
hook is not sandboxed. Descendants retaining output pipes cannot extend the
deadline indefinitely. Signal handling applies to the normal main-thread CLI
execution and restores the prior handlers afterward.

These process outcomes alone do not validate an agent's structured completion
protocol or establish controller acceptance of a task or issue.

## LSCli receipt validation foundation

The internal LSCli process adapter accepts success only when a zero process exit
agrees with a complete schema-version-1 JSONL receipt: contiguous sequence
numbers, one start, matching task/session identifiers, and one completed result
with a checkpoint digest. Missing, truncated, duplicated, unknown, or trailing
events fail validation. A completion string in ordinary output is insufficient.
Unexpected approval requests fail because this adapter has no approval channel.

Protocol mode discards raw stdout and stderr after bounded validation; its
metadata contains identities, status, sequence, and checkpoint, not result text.
Its optional activity deadline advances only on complete valid start/progress
frames. Stderr noise and partial frames do not reset it. This bounds protocol
inactivity, not semantic progress or repeated unproductive tasks. Neither a
receipt nor its saved metadata grants authority or closes an issue.

Typed LSCli profiles use the protected registered launcher and coding receipt.
Explicit reserved actions add run/compaction accounting; generic profiles
retain their existing process contract.

## Compound continuation evidence

The internal compaction receipt adapter accepts one JSON object of at most
16 KiB, with exactly schema_version (integer 1), source_checkpoint, checkpoint,
profile, and usage. Digests use the existing checkpoint format. Source and
profile must match the selected action; destination must differ from source.
Usage retains the compaction owner's one-request, zero-tool and allocated-token
checks. Duplicate keys, extra objects, malformed/truncated output and nonzero
process exits cannot establish completion. Partial output does not count as
streaming progress. The bounded process pump suppresses raw output tails when
using this adapter.

Successful process output is only candidate evidence. Before continuation, a
caller holding the matching task/session lease must verify the private owned
compaction receipt file, its agreement with process output, and both source and
destination checkpoints through the existing session owner. Missing, unsafe,
interrupted, incompatible or uncertain history fails; this verifier never
chooses a latest checkpoint, repairs evidence or replays an operation. It returns
only the verified destination digest and preserves the source history.

The reserved execution owner below uses this adapter and verifier. Neither
receipt text nor the verifier itself authorizes dispatch; the explicit action
and full allocation must be bound before invoking either phase.

## Reserved execution owner

The internal execution owner consumes the action planner's exact validated
inputs and an explicit expected binding and accounting head. It checks the
policy task, selected runtime and requested history before reserving the full
run/compaction envelope. Fresh sessions must be absent; the coding child also
enforces atomic creation with --require-new-session, so a concurrent creator
cannot turn a fresh action into reuse of a settled session. This new run option
is mutually exclusive with resume/recover-from and rejects explicit context,
skills and images; ordinary run modes retain their existing loading behavior.
Existing history must
have safe coordination locks before the session lease is acquired; missing
coordination evidence remains missing rather than being repaired implicitly.
Exhaustion, pending review, uncertain reservations and reused operations block
dispatch through the accounting state machine.

After reservation, the owner creates private attempt storage at
state_root/heartbeat/binding. Grant and provider documents are copied from the
same bytes used to compute the binding. Commands use those copies, so later
changes to the original files cannot change a running compound action. Storage
is create-only; collisions and interrupted creation require reconciliation.
It holds the selected runtime's shared use lease through both phases. Framework
controller code must remain outside the editable workspace, as must the planned
control and execution state. This owner executes no workspace hooks or queue
commands.

A compound attempt invokes the protected compact CLI first. Continuation uses
only the destination verified against the session owner's receipt and settled
history. Any failed process, invalid receipt, expired deadline, or failed
continuation check skips the coding phase. Coding uses explicit task/session,
current grants, bounded requests/tools/tokens, and the coding JSONL receipt.
Each phase has an outer deadline; one second is reserved for process teardown
within its allocation, and both phases share the original overall deadline.
The phase CLI timeout excludes the allocated 20-second startup/cleanup allowance.

A bounded private result.json records phase process/protocol outcomes, final
checkpoint and elapsed time, without raw output tails or model text. Its exact
SHA-256 becomes the accounting result. Successful coding is
execution_completed and still awaits controller review; it cannot mark a task
accepted or close an issue. Failures retain the whole reserved allocation. If
storage or result acknowledgement is uncertain, the existing reservation blocks
replay; inspect retained attempt and session evidence before reconciliation.

The [reserved action CLI](config.md#running-a-reserved-action) supplies explicit
selection and the existing heartbeat overlap lock. Installed qualification
remains a separate gate. This owner does not add a scheduler, activate
configuration, or change the legacy no-agent and generic-profile paths.
