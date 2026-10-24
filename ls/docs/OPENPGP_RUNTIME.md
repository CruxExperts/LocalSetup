---
status: ACTIVE
version: 4.67
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
no patch; automatic selection still needs to compare full encrypted transport
costs and dependency availability. Codec checksums do not supply trust.

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

The ad hoc, client-driven and designated-worker responsibility modes, durable
restart handling and distributed worker fencing remain integration work around
this common operation.

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
machine/boot, runtime configuration, identity and local catalog authority. Changes
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
