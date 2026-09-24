---
status: ACTIVE
version: 5.6
owner_skill: ls-agentq-transport
---

# Agent-to-agent protocol: PRDs and transport

Agent A and Agent B exchange PRDs and artifacts through the existing Agent Q
client. Mail and file drops carry the same signed and encrypted binary OpenPGP
envelope described in the [shared runtime contract](OPENPGP_RUNTIME.md). A private version 2 registry and persistent LocalSetup authority stores
select one expected peer, local recipient, full fingerprints, scope and exact
recipient set before decryption. The authenticated inner JSON manifest then binds
`from_agent_id` and `to_agent_ids` to those selections.

See the [build specification](AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md) for
historical decisions and the [scenario guide](AGENTIC_AGENT_Q_SCENARIOS.md) for
same-machine, separate-repo and remote examples. The
[client guide](../tools/agentq_transport_client/docs/USER_GUIDE.md) has current
CLI commands; the [registry example](../config/agent_trust_registry.example.yaml)
has the current fields.

## Shared rules

- Both agents use the same framework, registry schema and verification pipeline.
  Each independently pins its peer and maintains its own private authority store.
- Adapter ingest never promotes plaintext, unsigned, encrypt-only or legacy PGPy
  objects. Manual PRD placement into a local `in/` queue remains a separate
  operator workflow. Preserve legacy carrier objects for explicit migration.
- Transport writes to staging first. The PRD batch reads only authenticated
  `in/` entries. Acknowledgements are sent only when `ack_required: true`.
- Per-request `delivery` and `deliverable` select the return path and artifact.
  Retain a stable `conversation_id` and increment `iteration` for follow-up.
- Tool-generated PRDs include `localsetup_framework_version` and may include
  the source hash. The consuming queue applies its configured compatibility
  policy to a version mismatch.

## Private registry and identity

The version 2 registry stores the local full fingerprint, isolated GnuPG home,
protected secret reference, expected scope/role and persistent trust-state path.
Each peer has a full fingerprint, independent trust-state path, scope/role, exact
inbound recipient IDs and allowed transport roots/accounts/addresses. The
operator verifies and enrolls public certificates out of band; remote discovery
is advisory only. The registry and trust databases stay private and outside
application installation versions. A sender imports the peer's pinned public
certificate into its selected GnuPG home before sealing.

On inbound, select `--peer` through trusted configuration, not mail From,
filename or decrypted content. Check current local recipient and selected peer
authority, verify the whole binary envelope and integrity, then compare the
manifest identities. Recheck authority before exact ciphertext receipt and
promotion. A stale or revoked participant fails even if a secret key remains.

## File-drop and mail flows

Outbound: run selected pre-ship checks, form the bounded manifest and any
attachment checksums, seal once to the exact recipients, then write through the
selected carrier. File drops use a random opaque `.agentq.lspgp` stem, fsynced
payload and sibling `.ready` marker written last. Multi-recipient shipping
shares the same ciphertext. Only configured outbound roots are allowed.

Inbound: fetch or claim from an allowed root/account, bound bytes, verify the
shared envelope, validate manifest and attachment checksums, record exact
ciphertext acceptance, stage and atomically promote to `in/<transport-id>/`,
then move the carrier object to processed storage. A duplicate accepted receipt
is a terminal skip. Rejected content is never promoted. The queue ledger records
transport outcomes and replay decisions; `--force` cannot bypass verification.

Mail sends one opaque octet-stream attachment of at most 4 MiB using the existing
mail carrier and fetches a complete message of at most 6 MiB. Mail providers see
routing addresses and generic subject/body. After a verified ingest or
already-ingested duplicate, move the message to the processed mailbox. A failed
move is retried against the same account, mailbox, UID and ciphertext digest.
The bounded message is re-fetched before mutation; use the mail provider's
existing authorization or confirmation token.

## Iteration and recovery

B retains an archive under the selected queue for follow-up. If it is missing,
A can request attach-back or a link/manual handoff when the transport limit
prevents attaching the artifact. Failed pre-ship checks stop shipment unless an
explicit per-send skip was selected. Rotation and independent emergency recovery
use the [OpenPGP lifecycle workflow](../workflows/ls-workflow-openpgp-lifecycle/SKILL.md).
Historical reads require an exact prior accepted ciphertext receipt and do not
admit or promote new traffic.
