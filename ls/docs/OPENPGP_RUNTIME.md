---
status: ACTIVE
version: 4.90
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
expiry. An ENV, Envman, or explicit protected-file reference selects a passphrase source;
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

`SecretProvider.PROTECTED_FILE` resolves a symbolic name only beneath the trusted
caller's explicit `protected_file_root`. The root must be owner-private, and the
selected file must be an owner-only regular file with one link. Anchored,
nonblocking reads reject unsafe ancestry, symlinks, replacement, and excessive
content. Reads and create-or-reuse operations recheck that the configured root
still names the held private directory before reporting success. A replaced root
leaves both directories intact and returns a redacted failure. The resolver
neither creates a file nor selects another provider when
that source is unavailable. Secret values remain private process input; references
and errors contain no value. This protection relies on local filesystem access
controls and does not provide isolation from other processes running as that user.

`create_protected_secret(root, name)` creates a random protected-file secret once
and returns its reference and whether it was created. Concurrent callers reuse a
valid existing secret; malformed or partial files are preserved for repair.
Creation does not select a key or authorize enrollment.

`inspect_generation_candidate()` reconciles a recorded generation attempt using
an exact fingerprint or the expected identity, profile and certification fields.
A requested identity must match a verified self-certification's exact issuer,
user ID and hashed certification fields; unrelated packet text is insufficient.
A missing or empty home returns no candidate without invoking GnuPG. Existing
homes are checked for unsafe or persistent changes; GnuPG's transient lock files
can still change directory timestamps. Ancestors are checked again before a
candidate is returned, including when the key directory itself is unchanged.
The result contains public material only.
Adoption requires a separate proof of private signing and decryption capability;
an ambiguous or incomplete home must be repaired rather than generating a
replacement identity.

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

### Artifacts across carriers

The artifact APIs prepare and accept files together with their provenance using
the same shared envelope. Select the existing key-lifecycle workflow for this
operation. The encrypted record binds the binary content, origin, exact recipient
audience, purpose, scope, content revision, governance epoch, and admission window.
Optional source names and paths stay inside that record. They are descriptive
data and never select an output path or confer authority.

Before opening an artifact, independently select its expected publisher, audience,
purpose, scope, revision, freshness limits, private key home, persistent trust
store, and protected local directory. An incoming artifact, downloaded certificate,
storage account, filename, or message sender cannot select these expectations or
enroll itself. The directory must already exist and pass ownership, permission,
and symlink checks. This is an operator-designated plaintext boundary; the checks
do not establish disk encryption or physical workstation security.

`prepare_container()` accepts file-backed inputs and writes a complete set of
new `.lssc` volumes. `inspect_container_metadata()` verifies the small metadata
message without reading bulk content. `accept_container_metadata()` records
metadata acceptance after a fresh authority and replay check; its receipt does
not claim that any payload has been verified. `extract_container_message()`
independently verifies a selected message and its exact signed inventory binding
before publishing a new private output and recording its separate receipt.
Source names never choose that destination. These operations do not execute
content, change policy, enroll keys, promote a queue item, or authorize disclosure
to a model or provider. Consumer read and disclosure grants remain separate.

For a controller-approved distribution, `approve_container_distribution()` wraps
the unchanged origin metadata in an owner-signed encrypted metadata message. The
controller must be an explicitly approved recipient so it can inspect the
artifact. Recipients require both the selected origin and controller signatures
and the same independently selected audience. An ordinary artifact need not be
shared with the whole fleet. Approval by one controller or acceptance by one
recipient does not prove delivery or acceptance by every intended recipient.

Move the intact ciphertext through an allowed object store, NAS, shared drive,
removable medium, or attachment without changing its trust rules. Use an opaque
external name; do not copy source names or provenance into carrier metadata.
Byte transfer, cryptographic verification, and authorized acceptance are separate
results. Redacted operation receipts contain status and opaque logical/volume
identities, with a message index when applicable. Keep detailed verification
evidence and carrier locators private, and encrypt them
before sharing. Carrier policies still govern disclosure, and size, timing, and
other traffic observations are not hidden by this format.

Each volume is a single YAML-parseable file. Its public header contains format
and framing fields plus bounded full-fingerprint recipient hints. Logical names,
paths, provenance, inventory, versions, and relationships stay encrypted.
Metadata comes first and can be read through a range source; verifying it does
not certify an unread tail. Each payload message has independent encryption and
an authenticated position, length, ciphertext hash, and logical segment binding.

The default serialized-volume target is 100,000,000 bytes with 10% tolerance.
Packing prefers whole files and starts a new volume before splitting a file
that fits a fresh volume. Only an oversized fresh-volume input needs segments.
Final decisions measure encrypted and base64-encoded sizes. Small final volumes
and unused gaps are allowed. `ContainerPreparationLimits` configures file,
framing, packing, time, temporary storage, generated-byte, and cryptographic-work
bounds; these do not replace the existing byte-envelope limits. Missing keys,
trust, capabilities, or capacity leave preparation pending locally; there is no
plaintext fallback.

`ContainerLimits.max_files`, `max_volumes`, `max_messages` (per volume), and
`max_total_segments` bound protected inventories as well as preparation; these
are configurable processing limits, not format-defined maxima. Preparation
rechecks authority, cancellation, and its deadline after publishing each volume;
a late failure rolls back only outputs owned by that operation.

The canonical external key is
`lssc/v1/<tenant>/<project>/<publisher>/<artifact>/<version>/<volume>.lssc`.
Tenant and project identifiers must be explicitly approved for public use;
publisher and remaining identities are opaque. The signed encrypted inventory
binds each key to its logical artifact, version, and volume. A location is still
only a carrier locator and never establishes trust. An optional hard carrier
ceiling takes precedence over the packing tolerance.

Container and protected-metadata schemas have explicit versions and required
feature identifiers. Unsupported required features return `UNSUPPORTED`;
bounded optional extensions cannot change security semantics. Existing
schema-one byte and file envelopes retain their own decoders and exact signed
ciphertext. Historical compatibility requires its existing receipt and signature
checks; it never promotes unsigned legacy content into authenticated traffic.

### Inspecting and migrating legacy envelopes

`localsetup container inspect-legacy --source <absolute-file>` reads bounded
framing and computes the exact source digest without decrypting, accepting or
changing the input. A recognized schema-one envelope still reports `unverified`:
recognizing a header is not signature verification. Future envelope versions
return `migration_unsupported_version`; active secure containers use their
existing readers rather than this legacy migration path.

`migrate-legacy` creates a new, private, create-only output directory. It encrypts
the exact original bytes and a separate `localsetup.legacy-custody` version-one
record as two logical files in a new container. The record binds source format,
length, SHA-256, verification status, custodian fingerprint and migration time.
Historical verification also records the selected legacy signer, role and scope.
The new signature attests to custody of those bytes, not retroactive origin
authentication. All source and custody fields remain inside protected metadata
and encrypted payloads. Original files, signatures and receipts stay unchanged.

The private authority file has `schema: localsetup.legacy-migration-authority`,
integer `version: 1`, an approved `namespace`, and independent `publisher` and
`controller` contexts using the same `policy`, `trust_state_path` and `key`
records as [configured consolidation](#configured-consolidation-modes). Its
`legacy` member is either null or an object containing `trust_state_path`,
`scope`, `role`, `signer`, exact `recipients`, and a recipient `key` selection.
Selections come from the controller, never from unverified source headers. This
command does not require or enable an indexing runtime.

With a legacy context, the existing historical file decoder requires an exact
accepted-ciphertext receipt and independently verifies the original envelope.
Later authority revocation does not erase previously receipted authenticity.
Missing receipts, changed ciphertext, wrong participants or a bad signature stop
migration; `--preserve-unverified` cannot override that requested verification.
The verification plaintext exists only in bounded private scratch space and is
removed; the archived file contains the original ciphertext, not its plaintext.

With `legacy: null`, explicit `--preserve-unverified` permits opaque, unsigned or
unverifiable legacy bytes to be preserved with an `unverified` origin status.
It does not enroll the old origin or create an acceptance receipt for those bytes.
Malformed schema-one framing may be preserved this way; unsupported schema
versions cannot be silently interpreted as schema one.

Current publishing and approval authority, recipients, source identity, private
authority bytes, cancellation and resource limits remain checked throughout.
The controller must be an intended recipient so the operation can reconstruct
both approved output files and compare their exact hashes before reporting
success. The local `receipt.json` is a completion locator, not independent trust
evidence; consumers still verify the container. Use an operator-controlled
private output parent outside untrusted workspaces. Partial ciphertext may remain
after failure, with no successful result. Inspect it before explicitly selecting
a new output directory for a restart; an existing output is never overwritten
or automatically replayed. Migration does not upload, delete history or replace
legacy public APIs. [Command examples](COMMAND_REFERENCE.md#legacy-container-migration).

`ArtifactPolicy` supplies the expected local trust-store ID and revision,
governance epoch, scope, publisher fingerprint, exact audience, purpose, content
revision, creation and expiry times, boundary, and optional controller
fingerprint. The admission window is bounded to 30 days. `ArtifactProvenance`
can hold an original name and path, source revision, up to 16 source SHA-256
hashes, and up to 16 operational metadata fields. Those fields are descriptive
data inside the envelope; they cannot choose trust or trigger a fetch.

The policy's trust-store ID, revision, and governance epoch describe the local
receiver's authority. An independently enrolled receiver has its own store and
supplies the sender's exact origin context separately through
`origin_trust_store_id`, `origin_trust_revision`, and `origin_governance_epoch`.
Supply all three together from independently approved configuration. Omitting
them uses the local context for a same-store operation. Sender preparation binds
the actual selected origin authority; incoming claims cannot populate either
context. Remote recipients need their own decryption key and approved public
certificates, not a copy of the sender's trust database or controller private key.

For discovery of previously unknown versions, `ArtifactDiscoveryPolicy` retains
those independent authority, origin, audience, purpose, and scope constraints.
It adds a configured admission interval and maximum content age and lifetime.
The runtime learns version and timestamps only after signature verification.
It never obtains authority from an incoming claim. Deterministic inspection may
use an explicit trusted evaluation time; acceptance and output publication use
the current clock again within the authority transaction. A metadata cache hit
requires exact header and encrypted-metadata bytes plus current policy and trust;
it is an observation shortcut, not permission to release payload.

Trusted callers prepare outbound and inbound contexts independently from
approved local configuration. Each supplies its own `policy`, `trust_state_path`,
`gnupg_home`, and selected secret reference/resolver when needed. Preparation
returns an inventory of new opaque volume keys, file paths, serialized lengths,
and transport hashes. Metadata verification returns protected claims for the
trusted runtime; keep these private. Acceptance returns a redacted
`ContainerReceipt` identifying the operation, status, logical commitment,
volume, and optional message index. A receipt is never bearer authority.

Selected extraction and historical opening share one `FileEnvelopeLimits`
deadline and temporary-storage allowance across metadata verification, ciphertext
copying, decryption, and final output. Their optional `cancel_check` applies
through publication. Metadata is decrypted once per selection, and ciphertext,
verified plaintext, and the final staging file share the storage allowance.

`open_historical_container_message()` requires the exact previously accepted
metadata and selected-message ciphertext receipts. Historical reading does not
create a fresh acceptance or override new
traffic policy. Removing a recipient changes future distributions; it cannot
remove access to existing ciphertext. New recipients do not automatically gain
historical access. Preserve historical recipient keys and the stricter recovery
recipient policy; losing every usable decryption key and backup makes the old
ciphertext unrecoverable.

### Ciphertext transfer and publication

Transfer inspection uses the existing range-source contract to read and verify
metadata before fetching selected message data. A carrier without partial reads
must report its whole-object fallback rather than claim metadata-only transfer.
Source locations and generation tokens are observations, never trust or
enrollment authority.

Resume state is encrypted and authenticated under the local protection context.
It binds the metadata commitment, selected message descriptor, source generation,
policy, authority revision and cached segment hashes. Resuming requires current
authority and verification of retained segments. Cached data remains ciphertext;
partial transfer and metadata verification do not establish verified plaintext.
Byte, work, temporary-storage, elapsed-time and cancellation limits apply across
the transfer. One deadline covers metadata checks, encrypted progress records,
cached-file hashing and selected-message reconstruction. `max_cached_bytes`
counts all retained cache files plus concurrent crypto and replacement stages,
including the reconstructed ciphertext and interrupted files. Limit failures
preserve resumable inputs; the runtime does not discard them to make space.
The returned `cached_bytes` reports the retained total.

Default publication preserves the prepared ciphertext and creates an immutable object
without replacing an existing destination. Identical existing bytes permit an
idempotent result; different bytes are a collision. A lost acknowledgement
requires observation before another attempt. Transport completion remains
distinct from cryptographic verification and acceptance.
Only complete framed `.lssc` volumes are published; selected embedded messages
remain private resume-cache material. Before a provider receives bytes, the
runtime hashes the same held file descriptor that it streams. Provider adapters
must implement create-if-absent and exact observation, honor the supplied
remaining timeout and cancellation callback in their native transport, and read
chunks no larger than one MiB. This is a required adapter contract, not a promise
to interrupt arbitrary blocking callback code. A late provider failure may have
created the object and reports an uncertain result without replaying the write.

Carriers without conditional creation can use the explicit
`publication_mode="content_addressed"` option on `publish_prepared_volume`.
Its adapter implements bounded `put` and exact `observe`. The destination adds
`.sha256-<transport-digest>` to the canonical key, so different ciphertexts use
different locations and no mutable head is updated. `PublicationResult.key`
retains the signed canonical identity; `locator_key` reports the delivery
location. Receivers still verify the protected canonical identity independently
of that location. A successful put alone is insufficient: exact length and
SHA-256 readback are required, including after a lost acknowledgement. A matching
preexisting object needs no write; mismatched bytes are a collision.

After any uncertain dispatch, retain the prepared bytes and operation identity.
Use `reconcile_only=True` to observe without writing, in either publication mode.
Missing or inaccessible bytes remain uncertain and never trigger a resend from
that call. The caller owns durable operation records and restart decisions;
this transfer primitive does not provide distributed worker fencing. Hash-based
locations do not make an untrusted carrier immutable or guarantee availability,
and readback does not grant acceptance, signing or publication authority.

`container_publication_job.publish_prepared_job` adds a local encrypted restart
record for a fixed prepared volume set. The operator supplies a stable job ID,
a digest binding the approved destination configuration, exact destination
adapters, and a separate current write-authorization check. The record binds
the ordered volumes, their exact ciphertext digests and paths, the logical
commitment, admission policy, local trust revision and destination binding.
Changed bindings fail rather than silently starting a different job.

Before a volume can be dispatched, the runner durably records that its outcome
may be uncertain. Restart observes that volume without writing, skips already
verified volumes, and dispatches only volumes still marked pending. An absent
readback remains uncertain; it does not cause a resend. A checkpoint failure
before dispatch sends nothing, including when the checkpoint write itself lost
its acknowledgement. The journal uses the existing signed and encrypted private
record engine and a nonblocking local exclusive lease. Concurrent use reports
busy. One preparation budget covers record encryption and volume verification,
with at most 1,024 volumes and a job deadline of at most one hour.

Completed entries record historical verified delivery, not present availability.
The job does not delete inputs, grant write access, choose a branch, approve a
snapshot, or establish distributed worker exclusivity. Its destination digest
must come from the operator's trusted configuration; adapters must resolve that
exact destination. Protect the journal with private permissions and sandbox
isolation from other processes. Encrypted records alone do not prevent replay
of an older valid record, provide distributed fencing, or establish an
independent rollback-resistant authority.

### Complete-file reconstruction

`extract_container_file` accepts a mapping of carrier keys to range sources and
one protected file ID. Input order is irrelevant: the authenticated global
inventory determines segment order and coverage. Supplied volumes must agree on
the logical commitment and inventory; missing required volumes remain incomplete.
Only the selected file's messages are decrypted. Each segment obtains its exact
acceptance receipt, and the complete length and SHA-256 must match the protected
file record before any caller-visible output is created.

Reconstruction uses one deadline and aggregate work and temporary-storage budget.
Private segment stages are removed after use; final publication temporarily
retains the assembled file and the destination stage. The final writer-locked
acceptance checks current authority, freshness and every selected-message receipt,
then hashes the held assembled file again and creates the explicit destination
without replacement. Cancellation or verification failure removes owned stages
and outputs. This API performs current acceptance; existing receipt-bound
historical message access retains its separate semantics.

### Grant-bound container file reads

Trusted callers can provide a `ContainerReader` to the ordinary Agent file
broker. Its admission policy, trust store, key selection, secret resolver and
explicit carrier inventory stay in the supervisor; file requests still contain
only a path and optional continuation cursor. A private CLI grant can select an
enabled runtime through `container_runtime`; the same resolver serves CLI reads,
explicit run context and coding-tool reads. Indexed references use the same
grant-bound page interface.

A single-file carrier returns that file after complete reconstruction and
verification. A multi-file carrier returns a verified metadata index containing
names, lengths, hashes and member paths of the form `carrier/@files/<file-id>`.
Index verification does not claim payload verification. Reading one member
verifies its complete bytes before returning the first bounded page. Other
carriers come only from the supplied inventory; missing volumes report
`incomplete`, without scanning adjacent directories or changing trust.

Read and provider-disclosure grants are checked separately for the requested
path. A member-only grant does not grant its parent index or sibling members.
Private staging, trust state and the selected keyring must be outside the
workspace. Reconstruction and verification share one resource budget and a
deadline capped by the grant. Whole-file consumers retain their ordinary size
limit; paged reads use the existing bounded response format.

The reader retains one private verified file for at most 30 seconds by default
(configurable up to 300 seconds, still capped by the grant). Each continuation
checks current trust and freshness, grant revocation, the staged file and held
carrier identities. An optional trusted binding callback can also return a
64-character lowercase SHA-256 token for its runtime configuration or catalog
generation; a changed token during preparation or paging rejects the read.
Continuation cursors are single-use. Completion, failure or owner-lease cleanup
closes the held files and removes private staging. These checks never enroll an
identity or grant disclosure on the caller's behalf.

Runtime configuration version 2 adds `source_directories`, paired in order with
the existing opaque `source_ids`, and an explicit incoming `read_policy` using
`ArtifactDiscoveryPolicy`. Its trust store and scope must match `remote_content`;
its boundary is the runtime's private `reads` directory. Version 1 configuration
remains readable without rewriting, enabling it or generating identities. An
explicit configuration update uses the existing revision compare-and-swap.
Version 3 adds the optional `delta_codec` policy and `max_delta_depth` (default
16, maximum 64). The codec policy pins an absolute executable path and its
SHA-256 digest, along with the codec resource limits. Versions 1 and 2 remain
readable in place with delta decoding disabled. Configuration changes invalidate
existing read bindings; loading a configuration never installs a codec.

Each new content read discovers carriers in the configured local or mounted
directories. Discovery sorts entries, bounds entry count and depth, skips links
and special files, and recognizes framing independently of filenames. It does
not treat a discovered carrier as accepted content. Source outages retain their
unavailable state; exceeding the scan bound fails the request. A later ordinary
read can discover a newly delivered missing volume in the same agent session.
The reader currently bounds the candidate inventory at 1,024 carriers; this
local read path does not itself install scheduled reconciliation.

The resolver proves the configured runtime identity, checks independently selected
incoming trust, and binds paging to unchanged runtime configuration, identity,
machine binding and local catalog trust. The read policy pins its current
incoming trust revision; changes require an explicit policy update. Protected
CLI startup forwards only selected ENV passphrases as data under one fixed
supervisor variable. It never installs source-selected environment names in the
sealed interpreter or passes these secrets to the model worker.

### Protected per-file delta records

`ContainerInput` can describe an independently encrypted patch using a
`FileDelta` claim. Protected metadata minor version 1 declares the required
`per-file-delta-v1` feature. Existing full-file records retain their version-1.0
encoding; older readers reject the new required feature. A delta binds its exact
parent version, parent logical commitment, base file identity and content hash,
patch length and hash, change identity, codec, and reconstructed length and hash.
The enclosing origin signature and provenance bind the author. Controller
approval preserves that signed inner record. Inventory offsets describe patch
bytes while the logical file descriptor describes the reconstructed result.

The optional `container_delta_codec` API encodes and applies `zstd-patch-v1`
using an operator-selected executable and expected executable digest. It accepts
fixed arguments and held, owner-private input descriptors; it does not take
commands or executable selections from incoming metadata. Before publication it
verifies base and input hashes, output length and hash, and unchanged input
identities. Output is private and create-only. An uneconomic raw patch returns
no patch. Raw savings alone do not establish a delivery benefit; use the final
ciphertext selection below. Codec checksums do not supply trust.

Codec execution has explicit address-space, CPU, wall-time, output-file and
diagnostic bounds, no inherited secret environment, and cleanup of its owned
process group. These are process resource controls, not a general filesystem
sandbox. Successful output retains its byte reservation in the shared operation
budget until the caller removes it. No codec is installed, upgraded or bundled
by these APIs. The supplied executable and its runtime libraries need deployment
qualification in the selected environment.

`extract_container_file` accepts an optional `DeltaReconstructionPolicy` with a
trusted base-carrier resolver and codec policy. It reconstructs an exact chain
under one operation budget, verifying each base's artifact, version, commitment,
file identity and content evidence before decoding. The configurable maximum
depth defaults to 16 changes and is bounded at 64. Missing or mismatched bases,
cycles, excessive depth and invalid reconstructed bytes prevent publication.
Final release requires the selected-message receipts for the whole chain and
rechecks every dependency's admission and freshness under the current trust
transaction. Intermediate files are private and removed on success or failure;
only the final output retains a temporary-byte reservation.

Ordinary reads use the configured codec and a frozen catalog source inventory
to resolve the exact bases automatically. Persisted version conflicts also
apply to base selection. The reader retains selected dependency descriptors
through paging and checks their identities, current admission and freshness on
each continuation. A later request can discover new arrivals; they do not change
the dependency set of an existing read. Without a configured codec, delta reads
return `unsupported` before releasing patch bytes.

### Final encrypted delivery selection

`container_delivery_selection.select_prepared_delivery` compares an already
prepared and controller-approved full alternative with an approved delta
alternative. It checks every volume's actual serialized length and transport
hash, complete inventory, current signatures and authority. Both alternatives
must have identical logical files, provenance, ancestry and policy apart from
their version IDs and delta encoding. It reconstructs all files through the
existing receipt-bound reader before selecting a representation.

The reader freezes its configured source inventory for the entire comparison.
Only dependency volumes needed by the selected files enter the delivery cost;
shared dependencies are counted once. The cost includes complete ciphertext,
framing, encrypted metadata and controller approval, not only patch payloads.
The configured destination adapter's exact length/hash observation discounts
bytes already present. Conditional local destinations use anchored file hashing;
content-addressed carriers use the exact hash-qualified locator. An unavailable
observation or a collision fails selection. An unavailable reconstruction base
selects the verified full alternative; invalid content, changed authority and
resource exhaustion do not permit a silent fallback. The full alternative wins
equal costs, including when both deliveries are already present.

Selection takes a current `authorization_check` and one shared preparation
budget; the source count includes both alternatives and the frozen inventory.
It does not generate identities, sign either alternative or upload
anything. Supply a private `dependency_output_dir`: when the delta wins, the
selector retains exact dependency ciphertext there using create-only,
hash-qualified files. This also supports shared or mounted base carriers whose
permissions are unsuitable for a private publication source. Existing retained
files are reused only after exact-byte verification. Unselected candidates and
original history remain intact.

Pass the returned `prepared` and `dependencies` together to
`container_publication_job.publish_prepared_job`, with destinations for every
returned volume. The same encrypted journal binds the complete ordered set and
publishes dependencies before the selected version. Even dependencies observed
present during selection stay in that set: publication rechecks their actual
bytes, and restart observes completed dependencies without replaying their
writes. An uncertain write remains observation-only. No-dependency jobs retain
their existing binding format. Cost and delivery observations do not promise
future carrier availability; ordinary reads still verify the complete chain.

### Complete snapshots and exact coverage

`prepare_snapshot` reconstructs every file of one selected version with an
operator-bound `ContainerReader`, then uses separately supplied publishing
authority to prepare a new complete encrypted version. Authorization must be
explicit. Reading, reconstruction and sealing share one deadline and resource
budget; private intermediate files are removed and their reservations released.
The snapshot preserves file identities, content evidence and provenance, while
replacing patch payloads with complete files.

Protected metadata minor version 2 declares `consolidated-snapshot-v1`. Its
snapshot claim identifies the exact source version and commitment, and a
canonical covered set of the version, commitment, file, content evidence and
optional change identity actually used in reconstruction. The claim has a
deterministic checkpoint digest. Coverage is bounded by the metadata limits and
100,000 entries. All source-version files must match the snapshot's complete
file inventory. The snapshot has the source version as its sole parent; it
cannot silently merge other heads or claim to cover itself.

The configured carrier inventory is frozen once. Late arrivals are not added
to the covered set, removed or marked consumed; divergent branches remain in
the version graph. Current source authority, dependency freshness and held
carrier identities are rechecked at preparation's publication gates. An optional
caller publication check can withdraw a job's authority; failed checks roll
back the local output through the existing create-only preparation owner.
The result is a prepared volume set, still subject to configured controller
approval and separate carrier publication. This API does not upload or delete
history, and a read grant never authorizes snapshot publication.

`approve_snapshot` takes separate, explicit controller authorization and approves
the complete prepared snapshot set. Before signing, it checks each exact source
ciphertext, the logical commitment and covered-set claim, and a consistent,
complete origin volume inventory. It stages the resulting approved volumes
privately and returns only after the complete local set passes its publication
checks. A failed check removes only outputs owned by that call; original
ciphertext and unrelated files remain intact. It performs no carrier upload.

Controller approval can share the caller's preparation budget and current
publication-authority check. The shared check runs before approval work, before
signing, and before and after local publication. An optional metadata check
receives already verified origin metadata before signing and supplements the
existing trust checks. Snapshot-set approval uses it to bind the complete
requested snapshot. Temporary accounting returns to the caller's prior level
after success or failure; cryptographic work remains charged to the shared
budget. These checks support job and worker authority enforcement but do not
themselves establish a distributed worker assignment.

`container_consolidation_job.approve_snapshot_job` records exact prepared
snapshot receipts in a signed and encrypted private job before controller
approval. Its binding includes the source commitment, job location and identity,
operator-approved assignment digest, controller policy and local protection
context. Local journal protection remains separate from incoming-content and
controller authority. A current authorization callback must return `None` on
success and raise on rejection; other return values fail closed.

The runner records `approving` before invoking approval. After interruption it
does not invoke approval again. It inspects only the bounded published files
in that job's approval directory, verifies current signatures and the complete
origin inventory, and compares exact origin metadata and encrypted payload
digests with the retained prepared inputs. A complete matching set becomes the
saved approved receipt; an empty or partial set remains pending, and duplicates
or conflicting sets are rejected without selecting a winner. Hidden unfinished
staging and unrelated files remain intact. The caller may pass `snapshot=None`
on restart to use the encrypted record's retained input receipts.

The record uses the existing 32 KiB protected-record limit and at most 1,024
volumes; space for both the prepared and approved receipts is checked before
approval, so oversized jobs stop before signing. A nonblocking local lease
prevents overlapping writers. Prepared inputs and approved outputs are retained
and reverified, rather than trusted from a saved success flag. The returned
approved set can be passed to `publish_prepared_job` for independent no-replay
delivery. This recovery owner does not restart interrupted initial snapshot
preparation, automatically retry incomplete approval, delete history or provide
distributed assignment authority. Private filesystem isolation remains required
against replay of an older valid local journal.

`container_consolidation_job.prepare_snapshot_job` adds a protected checkpoint
before reconstruction and origin signing. It binds the selected source
commitment, reader configuration, namespace, assignment and publishing identity.
After interruption, it authenticates the retained published files and accepts
only one complete snapshot for that source. Its encrypted journal stores a digest
of the exact prepared receipt; recovery rechecks the files instead of relying on
that digest as proof of current trust. Verification uses the authorized reader's
recipient identity; the publishing key need not be able to decrypt its output.
Missing or partial outputs remain pending,
duplicates conflict, and a directory containing unexplained prior work is not
adopted as a new job. Inputs and unfinished outputs are retained. Recovery does
not silently repeat an uncertain preparation; a separately requested new job
uses its own directory and preserves the earlier attempt.

`run_snapshot_job` connects preparation, approval and delivery under one local
lease and one shared resource/deadline budget per invocation. Each stage uses
its existing encrypted checkpoint and current authority checks. The lifecycle
binds the destination configuration, transport mode and all stage identities
before the first signing operation, so a restart cannot change the later
destination while reusing the same preparation. The supplied adapter must resolve
that exact destination configuration. Delivery uses the reader's receiver policy
to verify the approved bytes; the read grant does not supply publishing authority.
The caller must separately authorize writing and supply its current authority
check. Completed delivery receipts remain historical evidence, not a promise
that a carrier will retain the bytes indefinitely.

### Fresh worker checks at job phase boundaries

`container_worker_authority` uses the existing signed configuration store for
an explicitly authorized assignment containing `assignment_id`, `artifact_id`,
`worker` (a full key fingerprint), and `enabled`. Every committed configuration
revision is a new generation, including reassignment to the same key. The exact
signed revision, digest, controller, recipient set and assignment enter the job
binding. Unrelated owner changes in the same store also invalidate that binding.

`AssignmentController` runs at the authoritative controller endpoint. It shares
the configuration writer lease while verifying a worker-signed request and
returning a controller-signed response. Both messages are encrypted to the
configured audience. It rejects an unfinished configuration change, a disabled
assignment or a request that does not match the current signed generation.
The assigned worker signature pin comes from that configuration, never from
the request. No certificate is enrolled and no private key is copied.

`AssignedWorker` makes a fresh random challenge for every phase. The response
must match the exact challenge, artifact, job, source commitment, destination
binding and phase. A cached response, missing worker private key, timeout or
unavailable controller prevents the phase from proceeding. Checks surround
cryptographic operations; the supplied transport must also honor cancellation
and its native deadline. The enclosing job budget accounts for both message
directions and the controller's configuration verification.

Pass `worker_authority` to `run_snapshot_job` to require these checks before
preparation, approval and delivery. Its worker key must differ from the shared
content publisher and controller keys; its controller must match the approval
policy. The source artifact is checked before preparing a new container, and
recovered preparation must match the same artifact. A changed generation cannot
reuse an old job binding. Existing jobs without this argument preserve their
previous checkpoint format and still require separate write authorization.

### Worker result proof and serialized controller admission

Worker snapshots add the required protected metadata feature
`worker-snapshot-admission-v1` at schema minor 3. The encrypted worker request
binds the proposed version, complete file content evidence and provenance,
covered checkpoint, source commitment, job, assignment generation and destination.
Its digest excludes only the nested worker signature itself. The origin signs
that proof with the rest of the snapshot. Older readers reject the unknown
required feature; existing snapshots keep their original record shape and bytes.

`AssignmentController.approval_guard` holds the authoritative configuration writer
lease across approval of every volume and the existing durable approval-job
checkpoint. It verifies the worker signature using the key in the current signed
assignment, then checks the exact result and job bindings. Configuration changes
contend on that same lease. A changed or disabled assignment cannot admit a new
result from the previous generation. The guard expires on exit and shares the
enclosing operation's budget; it cannot be reused as a lasting authorization.

The existing controller-signed approval wrapper carries the admission receipt.
No extra envelope layer or independent approval store is added. Generic approval
rejects a worker snapshot without the active guard. Receivers verify the trusted
controller signature, required feature and exact proof/result binding before
acceptance. Origin-only inspection remains available to preparation and recovery;
it does not grant payload release. A snapshot approved before reassignment remains
valid immutable history, including when delivery arrives later. An unconditional
carrier can retain stale bytes, but retention alone cannot provide admission.

Set `require_worker_snapshots=True` on the receiving artifact policy to reject
new snapshots that omit worker proof. Complete ordinary base versions remain
readable. This requirement changes the policy/cache binding. Historical reads
retain the existing exact-ciphertext receipt requirements and can inspect
previously accepted legacy snapshots; they do not grant new legacy acceptance.
Persisted runtime configuration uses version 4 only when this requirement is
enabled, so an older runtime cannot silently ignore it. Default policies preserve
the version-3 serialization and earlier configurations remain readable.

This SDK admission path requires a controller-local `AssignmentController` for
the approval lease. `run_snapshot_job` accepts an explicit `assignment_controller`
when the worker uses a separate phase-check transport; otherwise it uses the
worker's typed controller endpoint. No transport, daemon or key enrollment is installed. The
operator must provide one authoritative store with protected HEAD and exclusive
controller signing custody. A stale signing replica or copied controller key is
outside that guarantee. Configuration and activation remain explicit; the
following runtime binding routes selected modes through these same primitives.

### Configured consolidation modes

An enabled runtime can separately bind a private snapshot write authority file.
The ordinary file grant still grants only reading/disclosure. It cannot select
keys, change destinations or enable publication. The controller opts into writing
through the runtime configuration:

```bash
localsetup container --runtime-root "$runtime_root" configure-consolidation \
  --authority-file "$snapshot_authority" --expected-revision "$revision"
```

The binding pins the exact private file bytes by SHA-256 and advances the runtime
revision. Configuration version 5 carries this binding; older configurations
keep their previous shape. Editing the authority file alone invalidates its
binding. Binding does not enable a disabled runtime, create keys, enroll trust,
install a daemon or create a recurring grant.

The authority is a bounded JSON object with `schema` set to
`localsetup.snapshot-consolidation`, integer `version: 1`, and these fields:

- `mode`: `ad-hoc`, `client` or `worker`.
- `artifacts`: a sorted, unique list of authorized artifact IDs, at most 64.
- `events`: a sorted, unique subset of `manual`, `change` and `scheduled`.
- `namespace`: the existing `ContainerNamespace` fields `tenant_id`,
  `project_id` and `publisher_opaque_id`.
- `publisher` and `controller`: each contains an exact `ArtifactPolicy` object
  under `policy`, an absolute `trust_state_path`, and the existing runtime
  `KeySelection` representation under `key` (`fingerprint`, base64 `certificate`,
  `home`, `reference` with `provider`/`name`, and nullable `protected_file_root`).
  These are separate content authorities, not the runtime's local catalog role.
- `destination`: `kind: local-directory`, an existing private absolute `path`,
  and `mode` set to `conditional` or `content_addressed`.
- `worker`: null outside worker mode. In worker mode it contains the existing
  signed configuration `authority_file`, its `authority_sha256`, assignment
  `owner`, worker `key` in the same KeySelection format, and configuration `home`.
- `max_jobs`: an integer from 1 through 1024 bounding retained local jobs.

Passphrase values never belong in this file. Environment references cross the
protected supervisor boundary only as explicitly selected private values, never
as model-visible environment or dynamic interpreter settings. Authority files,
key homes, trust stores, private output boundaries and destinations must be
outside the granted agent workspace.

In **ad-hoc mode**, an explicit request names an exact catalog version:

```bash
localsetup container --runtime-root "$runtime_root" consolidate \
  --reference "@artifacts/$artifact_id/$version_id" --event manual
```

The same command accepts `--event change` or `--event scheduled` only when that
event appears in the authority file. This is an event entrypoint, not enrollment
of a watcher or recurring task. Existing periodic indexing remains metadata-only;
a read-only indexing grant never becomes permission to publish snapshots.

In **client mode**, an ordinary authorized file read reconstructs and verifies
the requested content, then consolidates an authorized artifact before returning
it. File indexes, artifacts outside the write grant and already consolidated
snapshots do not trigger publication. Without a consolidation binding, reads
retain their previous behavior. A configured write failure is explicit; uncertain
jobs remain available for observation and are not silently retried as new writes.

In **worker mode**, reads remain read-only and explicit events use the assigned
worker proof plus serialized controller admission. The signed configuration
authority declares a `snapshot-worker` owner with `assignment_id`, `artifact_id`
and `worker` defaults. Its editable value is the existing `WorkerAssignment` record,
including `enabled`; generated defaults are disabled. The receiving runtime must
set `require_worker_snapshots=True`. Other readers do not resolve worker signing
keys merely to consume an artifact.

All modes share the existing preparation, approval and delivery jobs. The job ID
binds the exact source commitment, private write binding and worker generation.
Repeated reads/events observe the retained result rather than minting another
version. A workspace copy resolves to the same catalog carrier where available;
an updated catalog head alone does not change the durable job identity. Sources
and branches remain intact, and no event chooses a winner among concurrent heads.
Reads and triggered jobs share their bounded operation budget and grant deadline.

Configured modes accept full content or authenticated deltas when the runtime
has an explicitly selected delta codec. A consolidated snapshot records the
exact source and covered base/change versions, and stores complete file bytes.
Reading that snapshot requires neither the codec nor its original base carriers;
normal snapshot trust, admission and worker checks still apply. Consolidation
does not delete those original carriers or revoke their separate retention policy.

The configured carrier currently supports a private local directory. It maps
carrier keys to flat SHA-256 filenames, publishes through exclusive staging,
verifies actual retained bytes and never overwrites a collision. Remote providers
must supply an adapter through the existing injected publication protocols;
this binding does not manufacture a remote transport or enroll remote credentials.

Revoke future publication with `disable-consolidation --expected-revision` on the
same container CLI. Existing readers observe the configuration change and new
reads resolve the remaining read-only configuration. Retained jobs, encrypted
snapshots, source history and carrier bytes are preserved. Reaching `max_jobs`
stops new jobs; it does not authorize automatic history deletion.

### Catalog-backed ordinary references

An enabled runtime reconciles its configured directories into its existing
encrypted catalog before serving an indexed reference or completing a physical
container read. Metadata inspection, source discovery and file reconstruction
share the request budget. A separate nonblocking scan lease prevents duplicate
concurrent reconciliation; contention returns `pending`. Metadata-only scans
do not decrypt payload messages. Cached observations are reused only through
the catalog's proof-backed metadata cache and current incoming authority checks.

The runtime retains complete versus unavailable source coverage, immutable
versions and conflicting claims. It commits changes through the catalog's
existing encrypted compare-and-swap owner. Unchanged scans do not append journal
entries; a proven committed chain is checkpointed before its configured bound.
An unresolved pending catalog write stops further reconciliation and reports
`pending`, leaving recovery to the catalog owner. It is never automatically
replayed by another ordinary read.

`@artifacts` is a reserved, read-only reference namespace:

- `@artifacts` returns artifact references and all known version heads.
- `@artifacts/<artifact-id>` returns immutable version references, including
  explicit conflicts and whether metadata was verified in the current scan.
- `@artifacts/<artifact-id>/<version-id>` returns its single verified file or
  a metadata-only member index when the version contains multiple files.
- Append `/@files/<file-id>` to read a particular member.

These references require explicit read and, for model use, disclosure grants
covering `@artifacts` or a narrower reference. A workspace-wide `.` grant never
covers this namespace, and read authority does not authorize writes, uploads or
merges. Divergent heads are all retained; timestamps do not choose a winner.
Conflicting claims for one immutable version refuse content reads. Recorded
metadata without a current verified source is not presented as verified payload.

Every retained page sequence binds the exact authenticated catalog head in
addition to runtime and incoming authority. A changed head, pending write,
revocation or changed selected carrier rejects continuation and clears staging.
Explicit physical paths do not enroll themselves as configured catalog sources.

### Signed live resource tuning

An existing runtime can bind to a `container-runtime` owner in the shared
[signed configuration set](COMMAND_REFERENCE.md#signed-configuration). Populate
that owner with the complete current runtime configuration, then explicitly bind
it without changing activation or identity:

```bash
localsetup container --runtime-root "$runtime_root" configuration \
  --authority-file "$authority_file" --config-owner containers
```

Only scan interval, scan-object limit, metadata-byte limit and maximum delta
depth may differ from the existing runtime configuration. Their existing bounded
validators remain authoritative. The exact authority file, owner descriptor and
legacy configuration digest are pinned; changing keys, recipients, sources,
trust policy, codec, activation or legacy revision requires the owning lifecycle.
The binding does not rewrite `runtime.json`, generate keys or enroll trust.
After an explicit legacy lifecycle change, update the signed owner and repeat
the bind command to acknowledge that base revision through the same pinned
authority. Existing readers and schedule enrollments do not silently adopt it.

Ordinary reads resolve the signed set beneath their existing grant and trust
checks. Long-lived readers use the shared configuration cadence and apply tuning
at the next operation boundary. Already-created read budgets remain fixed.
Invalid tuning retains the last applied values in a live consumer; current
authority is still required. A fresh process refuses an invalid current set
instead of trusting an unsigned fallback. Authority paths and key material must
remain outside the model workspace. Only explicitly referenced ENV values cross
the existing protected supervisor hop; values are never returned in status.
The accepted signed revision and digest enter the encrypted scan provenance.

### Automatic metadata reconciliation

For a configured, enabled runtime with a ready identity, explicitly enroll
periodic indexing with `localsetup container --runtime-root <absolute-runtime-root>
enable-indexing --grant-id <recurring-grant-id>`. Use the same `--state-root`
when managing a nondefault shared scheduler store. The existing durable scheduler
installs one fixed, per-runtime cron job; it preserves unrelated cron entries and
creates a private restorable backup before changing them. Installation of the
framework alone does not enable indexing or create identities.

Cron checks once a minute. The runtime's `scan_interval_seconds` determines when
a scan is due, so dispatch may follow the due time by up to one cron tick. The
existing configuration also owns source directories, incoming policy and resource
bounds. Metadata scans use the same reconciliation implementation as ordinary
reads, including current trust checks, encrypted catalog updates and pending-write
recovery boundaries. Overlapping invocations skip work. A private attempt receipt
prevents an interrupted or failed scan from restarting repeatedly before its next
interval; uncertain catalog writes remain pending until explicitly recovered.
Backward wall-clock movement refuses dispatch until the clock catches up.

The recurring grant binds the exact source implementation, Python environment,
machine/boot, runtime configuration, identity and local catalog authority. An
enrollment made after an explicit signed tuning binding also pins that binding
and authorizes its schema-bounded dynamic interval and resource limits. A changed
interval is measured from the preceding scan's start, without replaying an
uncertain catalog mutation. Earlier enrollments retain exact interval pins;
adding a binding requires a replacement grant. Changes outside this tuning scope
require a replacement grant; a reboot requires explicit same-host re-enrollment.
Passphrases are resolved from the configured secret provider in the scheduler's
environment, never embedded in the cron command or receipt. Use a protected-file
or supported Envman reference for unattended operation; a transient shell ENV
value is not automatically available to cron. Missing authority refuses a scan.

`localsetup container --runtime-root <absolute-runtime-root> status` reports
configuration, scheduling state and the last bounded run result.
`disable-indexing` revokes future scheduled dispatch and removes its exact cron
entry, preserving catalog history and independently authorized ordinary reads.
An already admitted scan may finish; disabling the runtime configuration prevents
further reads and maintenance through the live authority checks.

### Machine runtime identity

`container_runtime.py` owns private runtime configuration and machine identity
setup. `configure_runtime()` uses an expected revision; `setup_identity()` is the
explicit setup operation. Installing LocalSetup or inspecting status does not
create keys, activate services or enroll peers.

Setup reuses a persisted identity or the caller's custom selection. When neither
exists, it creates distinct local publisher and owner keys through the shared
lifecycle. Generated machine keys are visibly labeled `LocalSetup Encryption`
with a stable, non-sensitive identifier and use `pgp@cruxexperts.com`. Their
signed description includes the repository URL, creation-year copyright,
company URL and complete private-asset rights notice.

Each generation attempt is recorded before creating protected material. Recovery
adopts only the exact complete candidate after proving private signing and
decryption capability. Partial or ambiguous attempts are preserved for repair;
missing, expired or revoked persisted keys require lifecycle action instead of
automatic replacement. A changed machine binding requires explicit recovery.

`inspect_runtime_status()` is observational. Local catalog protection and remote
content approval retain separate trust contexts; generated local keys cannot
authorize an incoming peer or decrypt ciphertext addressed only to older keys.
`resolve_runtime_contexts()` can return a usable local catalog context while
`remote_content` is `None`; accepting incoming content remains pending until
remote trust is independently configured.
`backup_not_configured` reports that independent recovery has not been configured.

Runtime records and catalog files use the shared anchored file checks. Directory
creation rejects symlinked or replaceable ancestors before creating descendants;
reads and atomic writes recheck the complete named parent chain. Replacement
fails without writing into the replacement directory. Concurrent callers retain
the existing runtime lease and revision checks. These checks rely on private
filesystem permissions and the sandbox; they do not isolate hostile processes
running as the same user.

### Encrypted local catalog and version history

`container_catalog.CatalogStore` maintains a bounded encrypted local record of
verified metadata, source observations, expected file segments, and version
relationships. `global_catalog_root()` resolves its per-user state location
under `secure-containers/<opaque-catalog-id>` without activating the runtime.
Catalog records use immutable signed encrypted `.blob` objects. These internal
byte envelopes are distinct from the YAML `.lssc` delivery format. The plaintext
`HEAD` contains only its schema and an opaque ciphertext digest; descriptive
records, source locators, and reconciliation inputs remain encrypted.

Catalog protection and incoming content approval have independent configured
trust contexts. Routine catalog signing uses the machine's local publisher,
with a distinct local owner governing its lifecycle. The content trust store
retains the independently approved origin and controller pins. Catalog receipts
and head anchors belong to the local store; content metadata and payload
receipts belong to the content store. The receiver needs only its own private
key, never a remote publisher's or controller's private key. A catalog signature
attests local state and cannot authorize incoming content.

Select that receiver as the catalog encryption audience. Additional recovery
or sharing recipients require authority to disclose every affected catalog
entry. Do not inherit the audience of one incoming container or add an owner
as a decryption recipient merely because it governs a lifecycle.

Commits lock the local store and compare the expected head before publishing.
The existing trust store records pending and committed opaque head anchors,
which detect replay of an older `HEAD`. An interrupted switch requires explicit
recovery of the exact verified, receipted candidate; an orphan is never selected
because it is newest. Checkpoints and verified migrations write new encrypted
objects and retain the old ciphertext. These local anchors cannot detect
restoration of both the entire catalog and its trust database to an older copy.

`CatalogStore.commit()` also accepts a `ReconcileBatch` of two through sixteen
independently verified admission transitions. They must describe the same scan,
evaluation time, source configuration and source-coverage declaration. Each
observation retains its exact policy proof; a carrier accepted under more than
one context is rejected as ambiguous. The reducer computes availability once
across the complete batch and retains a separate trust decision for each policy.
Batch ordering is deterministic, and unchanged batches do not create a new
catalog revision. Byte and observation-work bounds apply across the whole batch.

Batch catalogs declare minor version 1 and the required feature
`independent-admission-batch-v1`. The feature remains bound to authenticated
snapshots through checkpoints and subsequent writes. Readers that do not support
it must reject it explicitly. Existing single-policy catalogs remain readable,
and new single-policy catalogs retain minor version 0. This catalog primitive
does not enroll another origin or bypass per-origin verification; operator
configuration and ordinary-reader integration remain separate requirements.

`container_reconcile` separates authenticated claims from source observations
and payload verification. Metadata verification does not establish complete-file
verification. Duplicate carriers add locators to the same immutable identity;
missing objects and source outages retain history and differ from deletion.
Reconciliation records explicit evaluation and configuration inputs and applies
deterministic ordering and deduplication. Limits apply while consuming inputs,
including duplicates, before building large records or graph results.
Repeated observations of the same immutable content and carrier retain the
latest observation time, preferring fresh verification on ties. Contradictory
claims for that exact identity are rejected instead of choosing an arrival-order
winner; distinct conflicting versions remain visible in the version graph.
Each captured observation must match the transition's exact admission-policy
digest; an older observation cannot be relabeled under a new authority revision.

An unchanged metadata cache requires an authenticated catalog capture, exact
prior content receipts, matching source metadata and expected key, and current
content authority, policy, and admission time. Caller-supplied inspection time
cannot extend admission. Snapshot, cache, observation, and transition proofs
bind their original captures; editing nested fields or copying a dataclass
cannot authenticate changed claims. A cache shortcut avoids repeated metadata
decryption but never authorizes payload release.

`container_versions` keeps logical artifact identity separate from immutable
versions. It reports all current heads, missing dependencies, collisions,
cycles, and maximal common ancestors. It never chooses a timestamp winner or
merges content. Preparation can retain an explicit `artifact_id` and bind
`parent_versions` into the signed logical commitment; omitted values retain
the existing fresh-artifact, empty-parent behavior. An authorized merge creates
a new version referring to both parents and preserves its inputs.

Runtime configuration generation 6 adds `additional_admissions`: up to fifteen
`ContentAdmission` entries alongside the existing primary `remote_content` and
`read_policy`. Each entry binds its own trust-store path, store identity, scope,
origin fingerprint, authority revision and admission policy. Policies share the
primary private read boundary; duplicate policy identities and mismatched scopes
are rejected. Resolving contexts checks each selected store separately and never
uses the local catalog trust store as incoming content authority. Changing the
admission set is a lifecycle configuration change, not live resource tuning.
Configurations without extra admissions retain their generation 3, 4 or 5
serialization; generation 1 and 2 readers remain supported without rewriting
stored bytes. Configuration alone neither enrolls a peer nor imports a key.

Catalog scans evaluate all configured admissions against one bounded source
inventory and share the same operation budget. A carrier must match exactly one
admission; overlapping successful policies fail before a catalog commit. Cached
metadata is reused only for the exact policy that authenticated it. All decisions
are committed together, preserving independently signed branches, duplicate
carrier locations and per-origin trust evidence.

Ordinary direct and catalog-reference reads select the matching configured
reader before payload release. Workspace separation applies to every selected
trust path. Paging retains the catalog head and the complete authority set;
a stale admission or changed runtime binding invalidates the read. No publisher
private key is required on the receiver, and neither ciphertext hints nor the
local catalog signer can select or create incoming authority.

The trusted container release primitive accepts explicit `DependencyReceipt`
records for independently owned base evidence. It acquires at most sixteen
participating stores in canonical path order and holds their writer transactions
through release. Each dependency requires its original metadata and selected
message receipts, current store revision/epoch/scope, current non-revoked
signers, matching layer receipts and a valid admission lifetime. Receipts are
never copied into the head's store. Dependencies are bounded to sixty-four
records and a shared million selected receipts, including the head. Contention
fails without automatic replay; expiry during the release callback rolls back
the published output and the head receipt. Calls without dependencies retain the
existing single-store transaction path.

Configured delta reads select each exact base from the frozen, authenticated
carrier inventory. `DeltaBaseSelection` carries its independently admitted reader;
legacy resolver mappings continue to use the caller's original authority. A
chain may vary its origin signer and trust store, but its scope, purpose and
recipient audience must match. Each base is reauthenticated under its own policy,
using the same byte/work/time budget, descriptor lifetime and chain-depth limit.

Reconstruction groups selected-message receipts by their original stores and
passes foreign dependency evidence to the ordered release guard. It retains each
dependency's admission policy through paging; a changed or revoked base authority
invalidates later pages even when the head's authority remains current. Content
lifetime checks inside release use captured policies without opening nested
writer transactions against the already-locked stores.

Snapshot preparation retains each reconstructed dependency's policy and trust
store through its publication checks. It rechecks those original authorities,
content lifetimes and held source generations before sealing a new full version
under the separately supplied publisher authority. Revocation of a base authority
during preparation aborts publication and rolls back staged output.

Delivery selection reauthenticates retained base carriers with their admitted
readers and keeps those authorities attached to subsequent generation and policy
checks. Exact approved ciphertext sizes and current destination observations
determine whether the full alternative or the delta plus its dependencies costs
less. An authority change during those observations fails selection rather than
silently falling back to a different representation.

For mixed-origin publication, `publish_prepared_job` accepts an explicit
`dependency_readers` mapping keyed by each dependency's exact logical commitment.
The mapping must cover the dependency set exactly. Every reader must require
controller approval and preserve the head's scope, audience and purpose. Its
policy, local trust path and current binding become part of the encrypted job's
restart identity; changing them cannot resume the old job. Each dependency uses
its own admission during transfer, and all configured dependency authorities are
rechecked throughout the job, including resumes after recorded completion.
Uncertain dependency uploads remain observation-only on restart. The default
single-policy call retains its existing journal binding. Reading authority never
substitutes for the caller's separate current destination-write authorization.

`run_snapshot_job` can receive an explicit `output_reader` for a new snapshot
signed by a different publisher. Reconstruction keeps the original source reader;
delivery uses the output reader. The new admission must preserve the source's
scope, audience and purpose, match the supplied publication origin and controller,
and require worker snapshot proofs when an explicit worker is used. Its policy,
local trust path and current binding are included in the durable lifecycle
identity and rechecked through preparation, approval and delivery. Changed output
admission cannot resume an existing job. Omission preserves the original
single-reader lifecycle binding.

Automatic consolidation selects exactly one primary or additional configured
admission matching the authorized output publisher and prospective content.
Missing or ambiguous matches fail before job creation or signing. The selected
policy must preserve source scope, audience and purpose, and worker mode requires
worker proofs on that output policy even when it is an additional admission.
The runtime retains its stable configuration binding for output checks; it does
not derive receiving authority from the content or copy another origin's key.
When source and output admission match, existing single-reader job identities
remain unchanged. Ad-hoc, client and worker modes share this selection path.

Independently signed branches from a common base remain separate current heads
until an explicitly authored new version names both parents. Arrival order and
duplicate carriers do not select a winner. Reopened catalogs expose the same
version graph, and a new full merge preserves ordinary authorized reads of the
base and both branches without rewriting their encrypted objects. The caller
chooses and authorizes the merged content; reconciliation does not perform a
content merge or grant publication authority.
Support for separate-origin reads does not grant
publication authority or authorize cross-scope reconstruction or disclosure.

These library primitives do not enroll identities, add sources, schedule scans,
publish objects, execute content, or change consumer disclosure grants.

### Agent Q and authorized file reading

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
