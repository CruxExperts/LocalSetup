# Agent Q transport client user guide

The client ships an inner JSON manifest through a signed and encrypted binary
OpenPGP envelope. File drops and mail carry the same opaque bytes. The recipient
selects the expected peer from its private registry before decryption and admits
only a fully verified manifest to the queue.

## Setup

Use the repository's locked Python environment and GnuPG. Prepare the private
version 2 registry from [the field example](../../../config/agent_trust_registry.example.yaml).
An operator must already have enrolled the exact owner and publisher certificates
in persistent authority stores. Select an isolated local GnuPG home with the
local protected signing key. Import the peer's public certificate from the
pinned authority store with `key-import`; it is needed to encrypt to that peer.
Choose a supported ENV or Envman `secret_ref` rather than a raw passphrase.
Run `registry-validate` on the private registry before a send or poll. The
example's fingerprints and paths are placeholders.

## Current CLI

From the repository root, invoke `uv run --locked python
ls/tools/agentq_transport_client/agentq_cli.py`. `--help` and each subcommand's
`--help` show the exact current arguments. A typical file exchange is:

```bash
uv run --locked python ls/tools/agentq_transport_client/agentq_cli.py registry-validate /private/agent-a/registry.yaml
uv run --locked python ls/tools/agentq_transport_client/agentq_cli.py key-import --registry /private/agent-a/registry.yaml --peer agent-b
uv run --locked python ls/tools/agentq_transport_client/agentq_cli.py ship-file-drop --manifest manifest.json --registry /private/agent-a/registry.yaml --peer agent-b --out /private/agent-a/drop/to-b
uv run --locked python ls/tools/agentq_transport_client/agentq_cli.py file-drop-poll --registry /private/agent-b/registry.yaml --peer agent-a --queue /private/agent-b/queue
```

The sender's manifest is JSON with `manifest_version`, `from_agent_id`, and an
exact `to_agent_ids` array. `prd_body` and `prd_filename` are optional. Include
`ack_required` only when a reply is needed. The pre-ship gate runs configured
argv-only checks before encryption; a failed check stops shipping. An explicitly
selected `--skip-pre-ship` skips those checks for the requested send and does not
change cryptographic or transport checks. `ship-file-drop-multi` seals once to
the manifest's exact recipient set and returns one shareable opaque object.

File output uses a random 40-hex stem, `.agentq.lspgp` ciphertext and a sibling
`.ready` marker written last. The filename reveals no sender or recipient.
Polling claims only allowed inbound roots. The verified manifest is staged and
atomically promoted to `in/<transport-id>/` after the accepted ciphertext receipt
is recorded. Legacy `.agentq.asc` and unsigned content remain in place with a
migration-required result. A duplicate receipt is skipped; `--force` never
bypasses signature or authority checks.

## Mail carrier

Use `ship-mail` with the manifest, registry, exact `--peer`, account, sender and
recipient addresses, policy and account configuration. It sends a generic
subject/body and one opaque octet-stream attachment of at most 4 MiB. `mail-pull`
selects the peer before fetch, bounds the complete message to 6 MiB, verifies
the same envelope and then moves an accepted message to the selected processed
mailbox. If a move fails, `mail-move-retry` retries the recorded account,
mailbox and UID after the mail provider permits it. A rejected message is not
promoted or moved. The carrier necessarily sees routing addresses.

## Other commands

- `key-gen` creates a protected local key in the selected isolated GnuPG home;
  `key-export` exports its public certificate, `key-fingerprint` inspects an
  armored public certificate. Key generation alone does not enroll authority.
- `ship-bundle` packages a bounded directory within the same signed envelope;
  `queue-pending` lists or moves verified queue work according to acknowledgement
  flags. `archive-prune` and `prune-processed` support selected retention tasks.
- `stamp-prd` adds the current framework version and optional hash to a PRD.

## Verify

Run `uv run --locked pytest -q ls/tools/agentq_transport_client/tests/` for the
package checks. A deployment test should exercise its actual selected registry,
GnuPG home, file or mail carrier and authenticated queue result. Keep test keys
disposable. See the [administrator guide](ADMIN_GUIDE.md) and
[protocol](../../../docs/AGENTIC_AGENT_TO_AGENT_PROTOCOL.md).
