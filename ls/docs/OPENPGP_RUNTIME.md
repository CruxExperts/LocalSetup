---
status: ACTIVE
version: 4.66
owner_skill: ls-agentq-ship-ingest
---

# Shared OpenPGP runtime

LocalSetup's `ls.core.openpgp` package owns certificate inspection, explicit
local trust, signed and encrypted envelopes, owner and publisher transitions,
protected backups, and independent recovery. The
[lifecycle workflow](../workflows/ls-wf-openpgp-key-lifecycle/SKILL.md)
shows how to use those Python APIs. Installation makes them available; it does
not generate keys, enroll trust, contact a provider, or authorize a consumer.

## Identity and envelope

Inspect a certificate and enroll its **full fingerprint** with the required
capabilities and profile. The default profile is RSA-4096 with a two-calendar-year
expiry. An ENV or Envman reference selects a protected passphrase source;
passphrase values stay out of arguments and public records. A discovered GitHub
certificate is comparison evidence, never an automatic trust update.
`secrets.py` ships the public secret-reference and resolver implementation; no
secret value or private key is stored in that source module.
`generation_models.py` holds key identity and result types; `key_records.py`
holds inspected certificate records and colon parsing. The public generation
and inspection operations remain in `generation.py` and `keys.py`.

### Native passphrase and certificate confirmation

An explicitly owned native terminal session can collect a passphrase for
`generate_key()`, `seal_envelope()`, `open_envelope()`, `seal_envelope_file()` or
`open_envelope_file()`, or require typed
confirmation in `enroll_key()`. The existing primary viewer receives the form. Selecting this
route does not install a global pinentry handler or turn a missing secret
reference into a prompt.

Use the matching `describe_generation_operation()`, `describe_seal_operation()`,
`describe_open_operation()` or `describe_enrollment_operation()` builder to
prepare a `NativeInputContext`.
The actual consumer recomputes that descriptor from its validated inputs before
requesting the form. Envelope descriptors bind the exact bytes, key and recipient
fingerprints, trust selection and private home identity. Enrollment binds the
verified inspection, expected full fingerprint, capabilities and profile.
Changing those inputs makes the context unusable for the new operation.

For files, use `describe_seal_file_operation()` or
`describe_open_file_operation()` with the same paths, limits and cryptographic
arguments as the consumer. Opening also binds the explicit `authorized` value.
These builders read and hash the complete input through a checked file
descriptor. The prompt covers its exact bytes and identity, the destination's
parent directory and name, the resource limits and the selected trust policy.
The consumer checks its own opened files against that descriptor, rechecks after
input and before publishing output, and rejects replaced source files or parent
directories. Output remains exclusive: an existing destination is never
overwritten. The operation deadline includes time spent waiting for input.

Generation binds the complete identity, profile and certification notations,
qualified parent directories, and whether the selected key home is absent or an
existing empty directory. Use that descriptor with `generate_key()` and pass
`passphrase_reference=None`, an explicit resolver and the native context.
The form asks for the new passphrase twice; a mismatch keeps the form open for
correction. Cancellation before preparation creates no directory and changes no
existing directory permissions. After acceptance, generation rechecks the
selection, prepares the private home and checks its exact identity before each
cryptographic phase. A failed operation that has already started preserves its
partial material for inspection instead of automatically generating again.

For example, after selecting the existing session owner and authorized inputs:

```python
import time

from ls.core.openpgp import (
    NativeInputContext, SecretResolver, describe_seal_operation, seal_envelope,
)
from ls.core.terminal_ops.command_service import ShellCommandService

def seal_with_native_input(payload, *, handle, owner, operation_id,
                           gnupg_home, signer, recipients, trust,
                           validate_current, state_root=None):
    selected = dict(payload=payload, gnupg_home=gnupg_home,
                    signer_fingerprint=signer, recipient_fingerprints=recipients,
                    local_trust=trust)
    context = NativeInputContext(
        service=ShellCommandService(state_root), handle=handle, owner=owner,
        operation_id=operation_id, expected=describe_seal_operation(**selected),
        deadline=time.monotonic() + 120, validate_current=validate_current,
    )
    return seal_envelope(**selected, passphrase_reference=None,
                         secret_resolver=SecretResolver(), native_input=context)
```

`owner` is the qualified controller binding used for the family session, and
`validate_current` checks the caller's continuing operation authority. Supply
exactly one input source: a secret reference or a native context. For opening,
pass a fresh context built with `describe_open_operation()` and an explicit
resolver. For enrollment, pass a fresh context built with
`describe_enrollment_operation()`; the operator must type the expected full
fingerprint. `enroll_key()` returns the resulting immutable `LocalTrust` and
does not persist it or authorize a trust-store transition.

Each context accepts input once. Cancellation, expiry, changed ownership or
uncertain delivery discards it without an automatic retry. The form hands off a
bounded mutable buffer and releases the terminal lock before GnuPG runs. The
consumer rechecks its binding before use, clears its buffers on every exit and
delivers passphrases through inherited pipes. Input acceptance is separate from
cryptographic success. Python and native libraries can retain temporary copies;
this is not a guarantee that every process-memory copy is erased.

For persistent enrollment, build a fresh context with
`describe_trust_initialization_operation(path, scope=..., owner_certificate=...,
publisher_certificate=..., recovery_certificates=...)` and pass it as
`native_input` to `initialize_trust_state()` with the same arguments. The operator
types the full owner, publisher and any recovery fingerprints. The descriptor
binds their exact certificate bytes and roles, scope, selected GnuPG executable,
and absent destination with its directory identities. No database or staging
file is created before confirmation. The consumer reinspects the certificates
afterward and checks current validity, owner, destination and the context deadline
through publication. Cancellation, substitution or uncertain delivery creates no
store. This native route requires Linux/WSL2 held-file-descriptor publication;
the existing initializer without a native context remains portable.

Persistent file acceptance and historical APIs continue to use their explicit
secret references. Existing signed transition and recovery procedures remain the
owners of changes to an already initialized authority store.

`KeyIdentity.certification_notations` accepts an optional immutable tuple of
bounded, namespaced name/value pairs. The default is empty. Generation preserves
these signed certification fields through primary and subkey expiry updates,
including exact Unicode and literal percent text. Long descriptions can retain
their complete provenance without overloading the visible user-ID comment.
Only non-sensitive provenance belongs in these public certificate fields.

`seal_envelope()` signs and encrypts for the exact selected recipients.
`open_envelope()` checks the format, packets, authenticated signer and recipient
set, integrity and selected local trust before returning content. The consumer
also checks current persistent authority before accepting content. The clear
header identifies the format and schema only. Consumers must select the peer
and authority from trusted local configuration before opening ciphertext;
transport addresses, filenames and decrypted claims cannot choose them.

For larger local files, `seal_envelope_file()` and `open_envelope_file()` use
the same signed envelope and selected-key policy with incremental file I/O.
The caller supplies `FileEnvelopeLimits` for plaintext, ciphertext, temporary
storage, diagnostics, and elapsed time. These explicit limits govern the file
operation; the existing byte APIs retain their 16 MiB plaintext and 32 MiB
envelope limits. A limit failure never returns a partial successful result.

The file APIs currently require Linux (including WSL2), `/proc/self/fd`, and
Python's directory-relative, symlink-following hard-link support. Publication
links the validated open file descriptor, so replacing a staging pathname cannot
publish different bytes. An unavailable capability returns
`FILE_PUBLICATION_UNAVAILABLE`; there is no weaker pathname fallback. This file
publication path is not available on macOS. Existing byte APIs are unchanged.

File operations require regular inputs and a protected local output directory.
They create a new output exclusively, reject symlink traversal, and check the
anchored input for changes. Opening requires explicit local output authorization
and keeps plaintext in private staging until signature, integrity, participants,
and selected local trust have passed. The consuming runtime must still recheck
current persistent authority before accepting or disclosing that content.

`accept_envelope_file()` combines verified opening with a fresh persistent
authority check and exact ciphertext receipt. It publishes the output within
the authority transaction and removes that output if receipt storage fails.
The initial authority query shares the operation deadline. Source identity and
cancellation are checked again immediately before and after output publication;
late changes remove only the operation's own output and preserve replacements.
`open_historical_envelope_file()` requires that stored receipt for the exact
anchored ciphertext, then verifies the envelope again before release. Historical
access preserves prior acceptance after signer expiry or revocation; it does
not admit new traffic. Each file operation shares one elapsed-time and
cancellation budget across local I/O, cryptographic work, and final publication.

Envelope and transition-signing subprocesses keep automatic agent startup
disabled and use the explicitly selected home. Before signing, sealing or opening
with a private key, LocalSetup starts that home's `gpg-agent` when needed,
preserves an agent that was already running, and stops only an agent it started.
Key generation stops its temporary agent before returning. Callers do not need to
launch an agent manually. Do not run concurrent operations against the same
GnuPG home; cleanup cannot distinguish another same-user caller's agent use.

## Persistent authority

`initialize_trust_state()` creates a private store once. It builds the committed
database in memory, then writes through an exclusively created, retained file
descriptor. It never reopens the destination pathname during initialization;
failure cleanup removes only its own file, preserving replacements. This requires
SQLite serialization support and retains the portable create-once API. Its revision, scope,
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
