---
name: ls-workflow-openpgp-lifecycle
description: Adopt, generate, back up, rotate, revoke, or recover OpenPGP owner and publisher keys using LocalSetup's shared implementation and explicit local trust.
metadata:
  version: "1.0"
---

## Purpose

Manage an OpenPGP key throughout its life using `ls.core.openpgp`. Select the
operation the user needs; do not run every phase for every request. Installation
only makes this workflow available. It does not create keys, contact a provider,
resolve a passphrase, or enroll trust.

Use the existing [OpenPGP contract and lifecycle specification](../../docs/AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md)
and the public [Python API](../../core/openpgp/__init__.py). The functions below
are Python APIs, not invented CLI commands. Use the installed LocalSetup Python
runtime or the repository's locked uv environment. Keep private key homes and
trust databases outside versioned application installations and public workspaces.

## Select identity and authority

Identify the owner, publisher and intended full fingerprints through the user's
existing authenticated channel. Select a private GnuPG home, private trust-store
path, scope, capabilities and `KeyProfile`. A display name, email, short key ID,
remote URL or successful decryption does not establish identity.

Call `inspect_key()` for an existing public certificate or selected keyring, then
`enroll_key()` with the expected full fingerprint, capabilities and profile.
Inspect its returned evidence before retaining the new local pin. For persistent
owner/publisher authority, `initialize_trust_state()` creates the selected store
once; use `load_trust_state()` thereafter. Preserve its store ID and revision for
changes. Do not reinitialize a store to get around a failed transition.

If an official GitHub source was configured, `discover_github_certificate()` can
compare its public certificate with the local pin. Match, mismatch and unavailable
are advisory results. None enrolls or replaces trust automatically.

## Generate and protect a new key

If no suitable key exists, collect the explicit `KeyIdentity` and use
`generate_key(identity=..., profile=..., passphrase_reference=...,
secret_resolver=..., keyring_home=...)`. The default profile uses RSA4096 and a
two-calendar-year expiry profile. Select `SecretReference` with the supported
ENV or Envman provider; resolve only for the operation. Do not put raw passphrases
in command arguments, logs, manifests or chat. Keep the returned public certificate
and full fingerprint separate from secret material. Key generation alone does not
activate the key for any consumer.

## Back up and prove restoration

Use `create_protected_backup()` with the owner's full fingerprint, selected
capabilities/profile, protected owner passphrase reference and a separately held
recovery recipient. Its recovery public keyring must contain public material only.
Write the encrypted backup to a new private path. Keep its recovery private key
and passphrase independently available; do not add that key as a routine message
recipient merely because it protects backups.

Exercise `restore_protected_backup()` into an explicitly empty isolated home.
Check the returned full fingerprint, capabilities, profile and expiry against the
original. Report the actual restore outcome. Retain historical decryption keys
as long as old ciphertext must remain readable. Losing every recipient key and
usable backup makes that ciphertext unrecoverable.

## Rotate, retire or revoke

For routine owner rotation, compose `create_transition_proposal()`,
`encrypt_transition_proposal()`, `approve_transition_proposal()` and
`verify_approved_transition()`. Delivery is encrypted only to the old owner;
its approval binds the exact candidate package. For publisher rotation use
`create_publishing_transition_proposal()`, `sign_publishing_transition_proposal()`,
`approve_publishing_transition()` and `verify_approved_publishing_transition()`.
The current owner authorizes the publisher, and the old publisher proves consent.

Apply the serialized approved record through `apply_owner_transition()` or
`apply_publisher_transition()` with the expected store ID and revision. These
functions independently verify the record and update authority atomically.
Observe the new snapshot and effective time. Consumers use current authority for
new traffic; only the recorded immediate predecessor gets the bounded overlap.
A scheduled transition survives an offline interval. Retire keys after their
applicable overlap while retaining required historical decryption material.

For compromise, the trusted operator calls `revoke_authority()` for the affected
role or exact predecessor. Verify new traffic is denied for that fingerprint and
pending transitions are canceled. Do not bypass revocation with routine rotation.

## Recover independent authority

If the operational key is unavailable, restoring a backup and replacing authority
are separate operations. For replacement, create a short-lived
`create_recovery_challenge()` for the exact successor certificate and scope.
The candidate signs with `sign_recovery_challenge()` and submits possession through
`submit_candidate_proof()`.

Use either an already authenticated clean-system/provider-console operator calling
`authorize_recovery_locally()` with the exact challenge digest and revision, or a
signature from an independently pre-enrolled offline recovery key. The library
cannot authenticate the provider for you. Enroll recovery pins through the trusted
operator's `enroll_recovery_key()` or initial store setup, never from the challenge
itself. Operational or candidate signatures alone cannot authorize recovery.

Call `apply_recovery_transition()` and verify the successor is active, the old role
is revoked and replay is denied. There is no overlap for emergency recovery.
Each remote consumer needs its own authenticated enrollment or independent pinned
recovery verification; a local receipt is not portable authority.

## Verify the consumer outcome

Use the actual selected consumer to sign, encrypt, verify and reconstruct bounded
test bytes. Check tampering, wrong participants, stale state and revoked keys are
denied. Keep test key material disposable and separate from production. A GnuPG
installation or certificate listing alone is not a successful consumer test.

Only a trusted consumer that has fully verified an envelope may call
`record_accepted_content()`. `open_historical_envelope()` opens exact previously
accepted ciphertext with its persisted receipt, including authentic signatures
whose key later expired or was revoked. It does not admit new messages or promote
historical content into a queue. Finish with the full public fingerprints,
operation/state revision, useful test results and any genuine recovery limitation;
never include secrets or private key material in that report.
