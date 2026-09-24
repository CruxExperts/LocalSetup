# Agent Q transport verification

Use the [current client guide](USER_GUIDE.md),
[administrator guide](ADMIN_GUIDE.md) and
[shared OpenPGP contract](../../../docs/OPENPGP_RUNTIME.md) for the executable
workflow. The old optional strict-GPG and PGPy modes have been replaced by one
mandatory signed and encrypted binary envelope.

| Behavior | Evidence to collect for a selected consumer |
|---|---|
| Private configuration | Registry version 2 selects the exact peer, full pins, local keyring, protected secret reference, trust stores and allowed carrier. |
| File drop | One opaque `.agentq.lspgp` ciphertext and ready marker; accepted receipt precedes queue promotion. |
| Mail | One bounded opaque attachment; rejected content stays out of the queue; a failed processed move is retried against the same account, mailbox, UID and ciphertext digest. |
| Authority | Current local recipient and selected peer remain authorized before opening and before receipt or promotion. |
| Failure cases | Tampering, wrong participants, revoked authority, legacy formats and duplicate receipts cannot promote new work. |
| Package tests | `uv run --locked pytest -q ls/tools/agentq_transport_client/tests/` runs the offline fixtures. |

An actual deployment test uses the selected registry, isolated GnuPG home and
file or mail carrier. An installed-file inventory only reports availability;
it does not establish cryptographic or carrier behavior. Run a disposable-key
round trip before claiming that a consumer is qualified.
