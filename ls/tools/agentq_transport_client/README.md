# Agent Q transport client

The existing Agent Q client sends one shared signed and encrypted binary
OpenPGP envelope over a permitted file drop or mail carrier. Its manifest,
including the sender and recipient IDs, stays inside the envelope. The
recipient selects the expected peer from its private registry before opening,
checks the full signing fingerprint and exact recipients against its persistent
LocalSetup authority store, then records a receipt before queue promotion.

Use the [private registry v2 example](../../config/agent_trust_registry.example.yaml)
as a field guide. Set real full fingerprints, isolated GnuPG homes, authority
store paths, scopes, recipient IDs and transport allowlists through an
already authenticated operator channel. The example placeholders do not enroll
trust. Existing version 1 registries and unsigned or PGPy objects require
separate manual migration; keep those legacy files intact. Receipt-bound
historical reading applies only to new-format ciphertext accepted by the
shared verifier.

`agentq_cli.py` exposes `registry-validate`, `key-gen`, `key-fingerprint`,
`key-import`, `key-export`, `ship-file-drop`, `ship-file-drop-multi`,
`ingest-blob`, `file-drop-poll`, `ship-mail`, `mail-pull`, `mail-move-retry`,
`queue-pending` and maintenance commands. Use each command's `--help` for its
current arguments. File drops use a random opaque `.agentq.lspgp` stem and a
ready marker created after the ciphertext. Mail uses one opaque binary
attachment; routing addresses remain visible to the carrier. Both paths share
the same verification and queue admission code.

The [user guide](docs/USER_GUIDE.md), [admin guide](docs/ADMIN_GUIDE.md),
[protocol](../../docs/AGENTIC_AGENT_TO_AGENT_PROTOCOL.md) and
[LocalSetup lifecycle workflow](../../workflows/ls-workflow-openpgp-lifecycle/SKILL.md)
cover setup, transport and key rotation. The package tests live under
`ls/tools/agentq_transport_client/tests/` and must be run explicitly alongside
framework tests.
