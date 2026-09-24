# Agent Q transport client troubleshooting

## CLI or version unavailable

Use the repository's locked Python environment, or the installed LocalSetup
runtime. `stamp-prd` needs its framework dependencies. `version` reads the
selected LocalSetup installation or source root; verify that source before
assuming `0.0.0` is a release version.

## Registry requires migration

The normal client accepts version 2 only. Keep the old registry and carrier
objects intact, copy the [v2 example](../../../config/agent_trust_registry.example.yaml)
to a private path, select actual full fingerprints and authority stores, and run
`registry-validate`. Short IDs, legacy `public_key_path` lists and unsigned
`.agentq.asc` files do not enroll current trust or enter normal ingest.

## Seal or open fails

Confirm the selected GnuPG home holds the local protected signing/decryption key
and imported public certificates for every selected recipient. `key-import`
reads the pinned peer certificate from its authority store; `key-fingerprint`
inspects an armored public certificate. Check the private secret reference and
GnuPG agent availability without printing secret values. Verify the selected
peer ID, full fingerprint, current scope/revision and exact recipients. A key
that was rotated or revoked is denied for new traffic even if an old secret
remains in a home. Fix trust through the lifecycle workflow, not `--force`.

## File drop or mail is not processed

For file drops, check allowed roots, complete opaque `.agentq.lspgp` bytes and
its sibling `.ready` marker. A conflict copy or temporary file is not a verified
carrier object. For mail, check the allowlisted account/address, one complete
binary attachment, the 4 MiB ciphertext and 6 MiB message limits, and the
selected mail provider's policy. Rejects remain unpromoted. A successful ingest
with a failed mail move creates a pending move bound to account, mailbox, UID
and ciphertext digest. Retry re-fetches the attachment and compares its exact
digest before movement; use the required confirmation token. A duplicate accepted message is
terminal and should move to processed storage, rather than repeating decryption.

## Queue and iteration

The replay ledger uses the exact ciphertext receipt and transport ID. Preserve
it with the queue. `queue-pending` reports accepted work and acknowledgements.
If an iteration's archive is missing or attach-back exceeds a carrier limit, use
the protocol's manual handoff or link path. Run the focused package tests and a
real selected carrier exercise before diagnosing an installed consumer from a
mock fixture alone.
