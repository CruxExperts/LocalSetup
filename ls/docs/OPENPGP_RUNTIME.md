---
status: ACTIVE
version: 4.25
owner_skill: ls-agentq-transport
---

# Shared OpenPGP runtime

LocalSetup's `ls.core.openpgp` package owns certificate inspection, explicit
local trust, signed and encrypted envelopes, owner and publisher transitions,
protected backups, and independent recovery. The
[lifecycle workflow](../workflows/ls-workflow-openpgp-lifecycle/SKILL.md)
shows how to use those Python APIs. Installation makes them available; it does
not generate keys, enroll trust, contact a provider, or authorize a consumer.

## Identity and envelope

Inspect a certificate and enroll its **full fingerprint** with the required
capabilities and profile. The default profile is RSA-4096 with a two-calendar-year
expiry. An ENV or Envman reference selects a protected passphrase source;
passphrase values stay out of arguments and public records. A discovered GitHub
certificate is comparison evidence, never an automatic trust update.
`generation_models.py` holds key identity and result types; `key_records.py`
holds inspected certificate records and colon parsing. The public generation
and inspection operations remain in `generation.py` and `keys.py`.

`seal_envelope()` signs and encrypts for the exact selected recipients.
`open_envelope()` checks the format, packets, authenticated signer and recipient
set, integrity and selected local trust before returning content. The consumer
also checks current persistent authority before accepting content. The clear
header identifies the format and schema only. Consumers must select the peer
and authority from trusted local configuration before opening ciphertext;
transport addresses, filenames and decrypted claims cannot choose them.

## Persistent authority

`initialize_trust_state()` creates a private store once. Its revision, scope,
current owner and publisher, predecessors, replay records and accepted-content
receipts survive a process restart. Routine owner and publisher proposals bind
the exact candidate certificate, expected predecessor and epoch. Verification
establishes the signed record; `apply_owner_transition()` and
`apply_publisher_transition()` recheck and change the store atomically. A
scheduled transition and its bounded predecessor overlap remain subject to
current authority on every use. Revocation denies new traffic. The
`trust_schema.py` helper holds versioned SQL and recovery tables; `trust_state.py`
retains validation, migration and atomic authority operations. The store has
no automatic trust enrollment.

`publishing_records.py` owns canonical publisher record shapes and bytes;
`publishing_transition.py` retains proposal, approval and verification operations.
`transition_contracts.py` owns owner-transition limits and validation,
`transition_records.py` owns its canonical record shapes and bytes, and
`transition_crypto.py` owns certificate and signature operations. Public owner
proposal, encrypted delivery, approval and verification remain in `transition.py`.

If an operational key is lost, `create_recovery_challenge()` binds the proposed
successor and store revision. Candidate possession must be paired with either
an exact authorization from an already authenticated local operator or a
signature from an independently enrolled recovery key. Applying recovery
revokes the old role, cancels pending routine transitions and permits no
overlap. A recovery receipt from one private store is not portable authority
for another consumer. A protected key backup and an authority transition
solve different problems; preserving historical decryption material still
matters.

`recovery_models.py` owns redacted errors and input limits,
`recovery_process.py` owns bounded GnuPG pipes and agent cleanup, and
`recovery_io.py` owns private backup and selected-home filesystem checks. The
public backup and restore operations remain in `recovery.py`.

An accepted ciphertext receipt authorizes `open_historical_envelope()` for
that exact ciphertext after its signer key expires or is revoked. Historical
opening does not authorize new traffic or queue promotion.

## Consumers and limits

- [Agent Q](AGENTIC_AGENT_TO_AGENT_PROTOCOL.md) uses one opaque binary envelope
  for file drops or a single bounded mail attachment. It records acceptance
  before promoting verified content into its queue.
- [LSCli's file broker](LSCLI_RUNTIME.md#task-bound-file-broker) reads a sealed
  file only with an explicit read and disclosure grant and selected OpenPGP
  authority. It verifies the whole envelope, then serves bounded pages.

The installed capability report describes files and interfaces present in a
selected release. A reported GnuPG executable is only `present_unprobed` until
an actual selected consumer runs a verified envelope operation. Use an isolated
consumer round trip to establish behavior; the inventory itself never reads a
keyring, resolves a secret, or activates trust.

The [Agent Q build specification](AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md)
retains historical design decisions and unimplemented transport ideas. The
runtime package and this page describe the current shared contract.
