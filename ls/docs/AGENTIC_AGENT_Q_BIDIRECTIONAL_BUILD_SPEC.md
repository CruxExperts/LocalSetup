---
status: ACTIVE
version: 4.25
owner_skill: ls-agentq-transport
implemented: "mail preencrypted armored send; ship-mail-strict; mail pull direct manifest; ship-file-drop-multi to_agent_ids; manifest.schema to_agent_ids; StubDrive/Telegram + ADAPTER_REGISTRY; claim lockfile + poll --use-lockfile; ADMIN_GUIDE Part 18; plus prior: ready-sha256; mail-move-retry; archive-prune; queue-pending; strict gpg file_drop; ship-bundle; tests"
deferred: "ls/tools/agentq_transport_client/docs/DEFERRED.md"
remaining_build: "Part 19"
---

# Agent Q bidirectional transport build order

**Single document.** Read in order. Each block is executable without waiting on a later section. This file is the **implementation and backlog build contract** for the Agent Q transport client. **Canonical protocol behavior lives in** [AGENTIC_AGENT_TO_AGENT_PROTOCOL.md](AGENTIC_AGENT_TO_AGENT_PROTOCOL.md) (ACTIVE); PRD shape and queue layout live in [PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md](PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md) and [AGENTIC_AGENT_Q_PATTERN.md](AGENTIC_AGENT_Q_PATTERN.md).

---

## Part 0 - Purpose and assumptions

**Purpose:** Agent Q bidirectional handoff over **pluggable transports** (mail, shared drive, Google Drive, Dropbox, network share, future Telegram/IM). **OpenPGP sign-then-encrypt** makes payloads authentic and confidential even when the pipe is public.

**Assumptions:**

- **Mail is one transport.** Any channel that moves bytes is an adapter; **validation is transport-independent.**
- **OpenPGP** provides who signed, integrity, confidentiality. **YAML registry** lists allowed peers and key paths; tooling validates before promote.
- **File-drop:** sync folder as root is enough for v1; no Drive API required initially.
- **Symmetric deploy:** same framework, registry shape, and crypto pipeline on A and B.

**Principle:** Transport **moves sealed blobs** only. Framework **verify then decrypt then checksum** then staging then promote. Batch skill stays **filesystem-only**; never opens IMAP or cloud APIs directly.

---
## Shared OpenPGP contract boundary

`ls.core.openpgp` defines reusable fingerprint, participant-policy, key-profile,
capability, trust, and envelope-header contracts for consumers. Its public
envelope header contains only `format` and `schema_version`; signer and recipient
identities, document paths, hashes, timestamps, and provenance belong inside
authenticated encrypted content. On open, check the observed signer against
local trust and the signed manifest's intended recipient set against policy;
hidden recipient packet IDs disclose a count, not recipient fingerprints.
The RSA-4096 profile expires two calendar years after creation, clamping
February 29 to February 28 when the target year is not a leap year. Local trust
is authoritative; remote certificate discovery can report a mismatch but cannot
enroll or replace trust. Shared contracts and key inspection are non-mutating:
they do not encrypt or migrate existing Agent Q payloads. Key generation is a
separate, explicit consumer operation.

`discover_github_certificate(username=..., expected_fingerprint=...,
api_entry_id=...)` is an optional advisory comparison against a configured
GitHub user's public certificates. It makes one anonymous HTTPS request to
GitHub, inspects the returned `raw_key` certificates with the shared inspector,
and compares complete primary fingerprints. It returns `match`, `mismatch`, or
`unavailable`; malformed, incomplete, paginated, or failed responses are
unavailable. A mismatch starts investigation and never changes a local pin or
activates a key. Installation and ordinary local verification do not invoke
network discovery automatically. Network reads use socket timeouts and a shared
five-second cooperative budget also checked around certificate inspection.
System DNS and an in-flight bounded GnuPG inspection can outlast that budget;
callers needing a strict wall-clock limit must supervise the operation.

Trusted core callers select a `SecretReference(provider="env" | "envman",
name="VARIABLE_NAME")` and call `SecretResolver.resolve(reference)` for private
key or passphrase material. The value stays in caller process memory; never
serialize it in a command line, result, diagnostic, or workflow output. Envman
uses `get --json --reveal NAME` with bounded output and time. Resolution never
falls back to a different provider or caches a prior value. This API does not
grant Agent Q transport code a new secret-export or automatic trust path.

`inspect_key(certificate=...)` reads one public certificate in a disposable
GnuPG home; the alternative `keyring_home=..., fingerprint=...` takes an
explicit full-fingerprint snapshot of a bounded `pubring.kbx` only. Keyboxd
and legacy keyrings are rejected rather than read ambiently. Inspection
reports primary and subkey direct usage separately from GnuPG's aggregate
uppercase capability flags, size, expiry, and revoked/disabled state,
but does not confer trust. `enroll_key` requires the expected full primary
fingerprint, selected signing/encryption capabilities, an explicit key
profile and current `LocalTrust`; it rechecks the exact expiry time at
enrollment and returns a new immutable trust set. The consumer must persist
that set in its chosen protected store. No key is generated or activated
by installation or inspection.

`generate_key()` in `ls.core.openpgp` is called only by a trusted consumer. It
creates an RSA-4096 signing primary and one RSA-4096 encryption subkey in the
caller-selected new or empty protected GnuPG home. Parent directories must be
symlink-free and non-replaceable by another Unix identity (a root-owned sticky
temporary parent is allowed). It resolves the passphrase through an explicit
`SecretReference` and `SecretResolver`, applies the
two-calendar-year profile to both components (including the leap-day clamp
above), and returns only the public certificate and full fingerprints. It
does not enroll trust or export private-key material. Generation currently
accepts only `RSA_4096_TWO_YEAR_PROFILE`; installation and inspection never
call it.

`create_protected_backup()` accepts the explicit owner keyring, full owner and
recovery fingerprints, a separate public-only recovery certificate keyring,
the selected profile/capabilities, and an owner passphrase reference. It
streams the protected owner's secret-key export directly into GnuPG
encryption, writing only a mode-0600 ciphertext backup in a private directory;
no unencrypted secret-key package is written to disk. The recovery private
key stays independently held and is not needed on the backup creator.
`restore_protected_backup()` uses that separate protected recovery keyring
and passphrase reference to decrypt/import into a new or empty isolated home,
then reinspects the full owner fingerprint, signing/encryption capabilities,
and expiry before returning. A failed restore clears the selected new home.
Restoration also requires GnuPG to report locally available owner private
components for the primary and needed subkeys; a public-only certificate,
dummy/shadow secret stub, or token-bound reference is not a cold recovery.
The APIs stop only GnuPG agents they start in selected homes. A pre-existing
agent remains running and may retain a previously cached passphrase; callers
must not concurrently use the same selected home and must handle any
pre-existing agent according to their own operational policy.
Neither API enrolls trust, activates a key, transports the backup, or repairs
lost historical ciphertext. Agent Q's older `key-gen` path is not migrated by
these core APIs; the explicit transport cutover must remove its unprotected
private-key export before it can claim the shared lifecycle contract.

`seal_envelope()` builds the bounded binary envelope using an explicit private
GnuPG home, full signing and recipient fingerprints, and a provider-resolved
passphrase passed by descriptor. It binds the format-only public header, sorted
recipient set, signing fingerprint, payload length, and SHA-256 digest inside
the signed, encrypted manifest. The ciphertext carries hidden recipient key IDs
and exactly one public-key packet per configured recipient; packet inspection
cannot independently attribute those packets to recipient identities. The
separate `parse_envelope()` and `parse_inner_message()` functions only parse
framing and authenticated-content shape; they do not verify a signature or
authorize plaintext release. Trusted consumers call `open_envelope()` with an
explicit private GnuPG home, exact `EnvelopePolicy`, and pinned `LocalTrust`.
The opener requires one valid GnuPG signature and integrity-protected
decryption, checks the full primary signer fingerprint, compares the signed
inner manifest with the public header and exact configured participant sets,
and only then returns original document bytes. Hidden packet IDs still provide
no cryptographic proof of the recipient identities. Existing Agent Q transport
paths remain unchanged pending the explicit migration.

Routine owner transition uses `create_transition_proposal()` to inspect both
public certificates and bind their exact identities, capabilities, creation,
expiry, and full primary fingerprints to a canonical signed record. The
candidate signs the record first; `encrypt_transition_proposal()` then sends
that candidate proof only to the current owner through the verified envelope.
`approve_transition_proposal()` opens it with the old owner's private key and
adds the old owner's signature over the identical record and a separate
signature approving the exact candidate-signed package.
`verify_approved_transition()` independently verifies both keys, the approval,
the old owner's existing local trust, nonce and transition-ID replay histories,
and predecessor. When current owner and epoch are supplied, it also requires
the exact next epoch in that chain. Ordinary verification accepts a scheduled
record through its recorded overlap; explicit historical verification conveys
authenticity only. Activation must check persisted current authority and time
atomically. The actual signing component must satisfy the key profile and remain
valid through the overlap. The signed
record includes scope, reason, and revocation/rollback instructions. Verification
returns facts only: the proposed key is not enrolled or activated, and candidate
receipt alone is never authorization to rotate. Selected private homes use
bounded GnuPG agent startup for crypto; a pre-existing agent is preserved,
while an agent started for this transition is stopped. Concurrent use of the
same selected home is unsupported.

Routine publishing-key rotation follows
`create_publishing_transition_proposal()` →
`sign_publishing_transition_proposal()` → `approve_publishing_transition()`.
The proposed publisher proves control over the canonical certificate-bearing
record; the old publisher signs that exact candidate-signed package; the current
owner approves the complete two-proof package. Each party can use its own private
GnuPG home. `verify_approved_publishing_transition()` requires explicit current
owner and publisher fingerprints, next epoch, exact expected scope, local trust,
replay histories, and time. It verifies all three signatures and the actual
signing components through the overlap, then returns immutable facts without
activation. Publishing keys need compliant signing capability; encryption is
not required for the publishing role. Missing any routine proof requires the
separate recovery procedure, never a candidate-only authorization shortcut.


## Part 1 - Locked decisions (constraints)

| # | Topic | Decision |
|---|--------|----------|
| 1 | Topology | **D** - One protocol for same-machine, git-only sync, and fully remote. |
| 2 | Ingest | **A** - Everything on disk first. Transport only moves bytes into inbox/staging. |
| 3 | Ack | **B** - Only when `ack_required: true`. |
| 4 | Return path | **C** - `delivery` and `deliverable` per request; transport per request or config. |
| 5 | Crypto | **Mandatory OpenPGP sign-then-encrypt** for automated adapter ingest. Plaintext or encrypt-without-sign out of spec for adapter path. Signature = legitimacy gate. Optional PSK additional only. |
| 6 | Who pulls/sends | **A** - Python client + transport adapters; batch never opens transport. |
| 7 | Tooling | Mail stack = mail adapter backend only. |
| 8 | Transports | **Mail | file_drop | future IM**. Same pipeline per adapter. |

**Reverse-prompt milestones (manual):**

- **M1** Mail + file_drop adapters **in parallel** after crypto pipeline + registry exist.
- **M2** Registry **in-repo** (paths only to keys); keys **out of repo**; **key tooling** required (generate, export-pub, import-pub, fingerprint, doctor) as **Python module + thin CLI** (M3).
- **M4** file_drop: **extension filter + ready marker** before ingest.
- **M5** On verify/decrypt fail: **quarantine** + **`agentq ingest --force --blob`** with ledger `forced`, reason, operator, timestamp (A5).
- **M6** file_drop after success: move to **processed/** subtree + **prune** CLI.
- **M7** Mail after success: **mandatory** move to processed mailbox (parity with file_drop).

---

## Part 2 - Build order (do this sequence)

1. **Registry YAML** - Example `agent_trust_registry.yaml` + **startup validator fail-closed** (no empty allowlist; paths exist; OpenPGP parseable).
2. **Outer format** - One **canonical armored OpenPGP** blob for both mail body and file_drop file; single `verify_then_decrypt` / `encrypt_then_sign` module.
3. **Inner manifest** - `manifest_version` + **manifest.schema.json** with bounds (max attachments, path length, total bytes).
4. **Key tooling** - Module + CLI: generate, export-pub, import-pub, fingerprint, doctor; support **multiple keys per agent** for rotation (19.3).
5. **Mail adapter** - IMAP fetch / SMTP push via **policy_engine** facade only; mandatory post-ingest move to **LocalsetupAgentQ/Processed** (config override per account).
6. **file_drop adapter** - Scan **allowed_inbound_roots** only; **sealed_extension** default `.agentq.asc`; **ready marker** sibling `stem.ready`; **writer order** payload complete then ready last; **ignore_globs** `*conflicted copy*`, `*.tmp`, `~*`, `*.part`; after success move to **processed/<iso8601_utc>_<shortid>/** + prune CLI; **processing/** exclusive lock before verify.
7. **Client orchestration** - pull_new / push on adapter interface; staging `.staging/<uuid>/` then atomic promote; **ledger after promote and processed move** when possible; **idempotency** by UID or blob id.
8. **Quarantine + force ingest** - `.quarantine/<id>/` + force CLI with ledger audit line.
9. **Sidecar archive** - Ship bundle checksum manifest + outer sign-then-encrypt; recipient verify then decrypt then checksum files.
10. **Pre-ship gate** - Sandbox-tester / test-runner / PRD `pre_ship_checks` or documented skip.
11. **Tests** - Ephemeral keyring in tmp_path; fixture registry path; file_drop + mail fixtures.
12. **Docs** - USER_GUIDE, ADMIN_GUIDE, API_EXAMPLES, TROUBLESHOOTING; framework audit pass.
13. **Ship gate checklist** - Part 10.

---

## Part 3 - Queue layout and backwards compatibility

**Structured:**

| Folder | Role |
|--------|------|
| inbox | Incoming via adapter or manual drop (via staging). |
| in | `status: ready` or resume `in-progress`. |
| out | Sent sidecars (message_id, transport_ref, etc.). |
| pending | Awaiting ack or handoff. |
| archive | B retains per conversation_id; **never commit**; retention + prune. |

**Flat:** If only `.agent/queue/` with no subdirs, entire folder = **in** (legacy). **Adapter path never auto-ingests plaintext** from file_drop roots. **Human may still drop PRD into `in/`** for flat layout.

**Config keys:** `agent_trust_registry_path`, `transports_enabled`, `queue_path`, `layout: flat | structured`, `sealed_extension`, `ignore_globs`, `post_ingest_mailbox`, retention, archive_max_total_gb.

---

## Part 4 - Registry (normative schema)

```yaml
version: 1
local_agent_id: agent-a
agents:
  agent-b:
    display_name: "Agent B builder"
    public_key_path: /secure/keys/agent-b.pub.asc   # or public_keys list for rotation
    allowed_transports: [mail, file_drop]
    mail:
      accounts: [acct_b_mailbox]
    file_drop:
      allowed_inbound_roots:
        - /sync/agentq/incoming-from-b
      allowed_outbound_roots:
        - /sync/agentq/outgoing-to-b
```

**Rules:**

- Inbound promote only if signature verifies to a registry key mapped to **agent_id**; then inner **`from_agent_id` must equal** that agent_id or fail (binding).
- Only scan/write under listed roots. **Signing subkey** verification; doctor prints signing fingerprint for registry.
- **Why not From: header alone:** forgeable. Legitimate PRD = OpenPGP signature from registered key.

---

## Part 5 - Payload and manifest contracts

**Outer:** One armored OpenPGP sign-then-encrypt message (same bytes for mail and file_drop).

**Inner after decrypt:**

- **manifest_version** (required).
- **Idempotency key**; ledger stores transport id (IMAP UID, blob path hash, etc.).
- Attachments list with **sha256** per file; after extract, checksum must match or failed meta.
- Optional: `conversation_id`, `iteration`, `transport`, `drop_path` hints; must stay within registry bounds.

**Archive ship:** Sidecar YAML/JSON + checksums; outer layer same crypto; mismatch -> no promote.

**PRD front matter extensions:** `transport`, `drop_path` / `drop_uri`, `localsetup_framework_version` (from VERSION resolver), `conversation_id`, `iteration`, `ack_required`, `delivery`, `deliverable`, `pre_ship_checks`, supersede fields.

**Outcome extensions:** `message_id` or `transport_ref`, `manifest_filename`, `conversation_id`, `iteration_shipped`, `archive_path` / `archive_manifest`, `skip_pre_ship_checks` + reason if used.

---

## Part 6 - file_drop conventions (defaults)

| Topic | Convention |
|--------|------------|
| Sealed extension | Default **`.agentq.asc`**; config `sealed_extension`. |
| Ready marker | Sibling **`payloadstem.ready`** (e.g. `x.agentq.asc` + `x.agentq.ready`). Optional first line `sha256 <hex>` to detect truncated sync. |
| Writer order | Temp write -> rename to final -> **ready marker last**. |
| After ingest | Move payload + ready to **processed/<iso8601_utc>_<shortid>/**; **agentq-prune-processed --older-than**. |
| Concurrency | Move to **processing/<id>/** or file lock before verify; one winner. |
| Flood | Per-poll cap, max blob size before decrypt, backoff on repeated failures; log event `ingest_verify_fail` with path hash only. |

---

## Part 7 - Mail conventions

- Post-ingest **mandatory** move to **LocalsetupAgentQ/Processed** (or config `post_ingest_mailbox`) so UNSEEN cannot replay without ledger.
- Mutating IMAP only through policy_engine; CONFIRMATION_REQUIRED -> non-zero exit + stderr; ADMIN_GUIDE covers automation profile.

---

## Part 8 - Inbound flow (step by step)

1. Adapter lists candidates (UNSEEN or glob under allowed roots ignoring ignore_globs).
2. file_drop: exclusive claim blob (processing dir or lock).
3. **Verify** OpenPGP signature; map fingerprint to agent_id; reject if not allowed.
4. **Decrypt**; on fail -> quarantine + `.meta` code; continue with next.
5. **Validate inner manifest** schema + bounds; **checksum** files; inner from_agent_id must match signer binding.
6. Write to **inbox/.staging/<uuid>/**; atomic promote to **in/** (or inbox then promote step).
7. **Move source to processed** (file_drop processed subdir or mail folder).
8. **Append ingest ledger** (after promote + move when possible); UID/blob id idempotent.
9. PRD batch reads filesystem only.

**Force path:** Quarantined blob -> fix keys -> `agentq ingest --force --blob <path>` with reason + operator + timestamp in ledger.

---

## Part 9 - Outbound flow

1. Pre-ship gate passes or skip documented in outcome.
2. Build inner manifest + checksums; **encrypt_then_sign** with recipient pubkey from registry.
3. Mail: send via facade. file_drop: temp write -> rename -> **ready last** under allowed_outbound_roots.
4. Local archive + sidecar; prune if over quota; disk space check before large write.

---

## Part 10 - Ship gate (definition of done)

1. Code + config implement Parts 2-9; deferred items explicitly documented (e.g. multi-recipient phase 2).
2. AGENTIC_AGENT_TO_AGENT_PROTOCOL.md **ACTIVE** with front matter.
3. Full doc set: protocol + client USER_GUIDE, ADMIN_GUIDE, API_EXAMPLES, TROUBLESHOOTING.
4. Framework doc standards (GFM, script-and-docs-quality, no EM dash in user-facing text).
5. Framework audit / link check pass.
6. SKILLS.md + platform templates updated.
7. Client tests pass (ephemeral keyring only).
8. Registry + OpenPGP verify on every adapter ingest; no plaintext adapter ingest.

---

## Part 11 - Transport adapter interface

```text
pull_new() -> iterable of (raw_blob_bytes, metadata)
push(blob_bytes, metadata) -> transport_ref
```

Implementations: **mail**, **file_drop**, **manual** (CLI verify only), **im_file** future. All call shared crypto module only.

---

## Part 12 - Pre-ship gate (Agent B)

Before ship: sandbox-tester smoke where applicable; debug-pro on failure; test-runner if code PRD; PRD **pre_ship_checks** list run with exit codes in outcome; optional framework-audit. No ship without pass or **skip_pre_ship_checks** + reason.

---

## Part 13 - Hardening summary

- **Staging + ledger:** At-most-once per transport id; ledger JSONL; crash-safe re-run.
- **Registry:** Fail closed at load; schema validate.
- **Paths:** pathlib only; file_drop writes never shell-interpolate paths.
- **Manual vs adapter:** Adapter always crypto; manual in/ for flat queue without adapter.
- **Processed vs ledger order:** Promote -> move processed -> ledger; **pending_processed_move** if move fails.
- **CI:** pytest tmp_path keyring; AGENTQ_REGISTRY_PATH fixture; never ~/.gnupg.
- **Observability:** JSONL event types `ingest_verify_fail`, `ingest_decrypt_fail`, `ingest_checksum_fail`, `ingest_promote_ok`, `ingest_forced`, `ship_push_ok`, `ship_push_fail` with stable **code**; redact secrets.
- **Multi-recipient:** Phase 1 pairwise; Phase 2 deferred.

---

## Part 14 - Edge cases

| Case | Handling |
|------|----------|
| Signature verify fail | Quarantine / failed meta; no promote. |
| Checksum mismatch | Failed meta; tamper or truncate. |
| Partial file_drop file | Ready marker + optional sha256 line; writer order. |
| Missing registry peer | Reject; stderr agent_id. |
| Sync conflicts | ignore_globs. |
| Junk flood | Cap + max size + backoff. |
| Disk full | No partial archive ship; stderr. |

---

## Part 15 - Skills and framework touchpoints

- mail-protocol-control: sign-then-encrypt for Agent Q payloads; link to this spec.
- prd-batch / umbrella: transport-agnostic; promotion; version mismatch warning; pre-ship when shipping to agent.
- AGENTIC_AGENT_Q_PATTERN: queue layout, registry, file_drop.

---

## Part 16 - Files to create

| Item |
|------|
| agent_trust_registry.yaml (+ schema validator) |
| manifest.schema.json |
| Crypto pipeline module (encrypt_then_sign / verify_then_decrypt) |
| file_drop_adapter + mail adapter |
| Client package (or agentq_transport_client) |
| Key tooling module + CLI |
| Ingest ledger, quarantine layout, prune CLI |
| Tests with fixtures |
| .gitignore for archive |

---

## Part 17 - Out of scope / future

- Full Google Drive API (use sync folder).
- Telegram/IM adapter until scheduled; same pipeline when added.
- New crypto algorithm (OpenPGP only).
- Multi-recipient phase 2 until scheduled.

**Backlog detail:** Part 19 (after Part 18) lists remaining build items in execution-friendly order. Short list in DEFERRED.md stays in sync for the client package.

---

## Part 18 - Documentation deliverables

USER_GUIDE / ADMIN_GUIDE must cover: transport choice, registry edit, key pre-share and rotation, conflict filenames, insecure location rationale, force ingest audit, automation profile for mail.

---

## Part 19 - Remaining build list (backlog)

Ordered for minimal blockers. Rows 1-2, 3, 6-8 **implemented** (see front matter `implemented` and DEFERRED.md).

| # | Build item | Status | Notes |
|---|------------|--------|--------|
| 1 | Mail outer sign-then-encrypt | **Done** | `preencrypted_openpgp_armored` in mail_control `send_encrypted`; CLI **ship-mail-strict** |
| 2 | Mail pull strict ingest | **Done** | `mail_pull_and_promote` promotes envelope if `manifest_version` + `from_agent_id` (direct manifest after decrypt) |
| 3 | Multi-recipient phase 2 | **Done** | `to_agent_ids` + **ship-file-drop-multi** |
| 4 | Google Drive / Dropbox **API** | Deferred | Sync folder only via StubDriveAdapter |
| 5 | Telegram **API** | Deferred | StubTelegramAdapter raises; Part 17 |
| 6 | Adapter registry | **Done** | `ADAPTER_REGISTRY`, `get_adapter` |
| 7 | File lock before verify | **Done** | `claim_with_lockfile`; **file-drop-poll --use-lockfile** |
| 8 | Part 18 doc pass | **Done** | ADMIN_GUIDE expanded |

**Optional next:** gpg subprocess decrypt in mail skill when PGPy fails on gpg-signed packet; cloud OAuth adapters.
