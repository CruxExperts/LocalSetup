# Agent Q transport client administrator guide

## Authority and registry

Store each registry version 2 file privately. It names the local agent's full
fingerprint, isolated GnuPG home, protected `secret_ref`, private persistent trust
store, expected scope and role. Every peer has its own full fingerprint, trust
store path, expected scope, role, exact inbound recipient IDs, and allowed file
roots or mail account/address. See the [field example](../../../config/agent_trust_registry.example.yaml).
The selected peer ID comes from trusted configuration or the CLI before decrypt;
mail From and opaque filenames are not identity selectors.

Use the shared [OpenPGP lifecycle workflow](../../../workflows/ls-workflow-openpgp-lifecycle/SKILL.md)
for explicit key adoption, protected backup, routine transition, revocation and
independent recovery. The registry does not enroll trust itself. Export only a
public certificate; import the peer's pinned certificate into the local GnuPG
home before sealing. Rotate through the signed transition and atomic authority
store, then update registry pins. New content uses current authority; only an
explicit recorded predecessor during its overlap can be selected for inbound.
A revoked local recipient or peer must not admit new content even if an old
secret key remains in a home. Historical opening uses an exact accepted receipt
and never promotes a queued message.

## Transport operation

File-drop writes the complete opaque `.agentq.lspgp` file, fsyncs it, then writes
an empty `.ready` marker. Poll allowed inbound roots only; the optional
`--use-lockfile` controls claim locking for shared roots. Processed objects
retain their ciphertext and marker. A generic filename does not disclose
sender, recipient, conversation or subject.

Mail sends one signed encrypted binary attachment with a generic subject and
body through the existing `mail_send` action. The envelope is at most 4 MiB and
the complete fetched message at most 6 MiB. `mail_get` returns one complete
octet-stream attachment; missing, multiple and truncated attachments are
rejected. Mail account and destination address must match the peer's allowlist.
Routing addresses are visible to mail providers. A successful verified ingest
moves the message to a processed mailbox; a failed move is recorded for retry
with account, mailbox, UID and ciphertext digest. Retry re-fetches the bounded
message and moves it only when the exact ciphertext still matches. Use the mail provider's existing confirmation
token when one is required. A failed verification stays unpromoted.

## Queue and recovery

The client checks a signature, manifest sender/recipient binding and current
local/peer authority before recording exact ciphertext acceptance. Only then may
it stage and atomically promote under `in/<transport-id>/`. `--force` can request
reprocessing of a previously verified receipt and records an operator/reason;
it cannot bypass cryptography, policy, or an existing target. A duplicate
authenticated delivery is a terminal skip. Preserve the queue ledger for replay
accounting. Keep archived legacy objects intact for an explicit migration; v1
registries and unsigned or PGPy objects cannot enter normal ingest.

When a sync folder contains conflict copies, temporary files or incomplete
markers, wait for the original ready pair. Use `queue-pending` for acknowledgement
work and `archive-prune` or `prune-processed` with `--dry-run` before selected
retention changes. Framework version stamps remain available for PRD consumer
compatibility decisions. A missing remote archive may require attach-back or a
manual link when the transport size limit prevents attachment.

## Checks

`registry-validate` checks the selected private config. Run the focused package
suite, then a real signed/encrypted exchange with the actual selected consumer.
Verify altered content, wrong peer, stale/revoked authority, oversize and replay
denial. Record public full fingerprints, authority revision and resulting queue
path; do not log passphrases or private key material.

See the [user guide](USER_GUIDE.md), [protocol](../../../docs/AGENTIC_AGENT_TO_AGENT_PROTOCOL.md),
[mail skill](../../../skills/ls-mail-protocol-control/SKILL.md), and
[deferred items](DEFERRED.md).
