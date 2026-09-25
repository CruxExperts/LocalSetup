---
status: ACTIVE
version: 5.7
owner_skill: ls-agentq-transport
audience: humans, agents
---

# Agent Q scenarios: repos, agents, local, remote

**Purpose:** Describe how **file_drop** (and when relevant **mail**) works across common deployments: same machine vs remote, same repo vs different repos, and how agents A/B align on paths and keys. Written for operators and for agents that must choose commands without guessing. **This document and [AGENTIC_AGENT_TO_AGENT_PROTOCOL.md](AGENTIC_AGENT_TO_AGENT_PROTOCOL.md) define transport and registry behavior; queue layout and batch processing live in [AGENTIC_AGENT_Q_PATTERN.md](AGENTIC_AGENT_Q_PATTERN.md).**

**Prerequisites:** [current protocol](AGENTIC_AGENT_TO_AGENT_PROTOCOL.md),
[shared OpenPGP contract](OPENPGP_RUNTIME.md), and the
[client guide](../tools/agentq_transport_client/docs/USER_GUIDE.md). The
[build specification](AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md) retains
historical command examples; use the current CLI help for exact options.

---

## 1. Concepts (minimal)

| Concept | Meaning |
|--------|--------|
| **Agent** | Logical identity (`agent-a`, `agent-b`) with OpenPGP keys and registry entry. Not the same as "Cursor session" or "repo clone". |
| **Queue** | Per-deployment filesystem tree (`in/`, `inbox/`, ledger). Usually under `.agent/queue` in that repo. **Not shared** between repos unless you point both at the same `queue_path`. |
| **file_drop root** | Shared **directory**. Writer drops sealed blob + ready marker; reader polls or ingests from that directory. **Must be the same absolute path** (or equivalent) on both sides when on the same machine. |
| **Sealed blob** | One signed and encrypted binary OpenPGP envelope. Ingest requires the exact configured signer and recipient authority before queue promotion. |
| **Registry** | Private version 2 YAML selects the local keyring, protected secret reference, persistent trust stores, peer fingerprint, and allowed roots or accounts. |

**Invariant:** Transport moves **bytes** only. **Validation** (registry, decrypt, manifest) is the same no matter if the folder is local, NFS, or sync-cloned.

---

## 2. Scenario matrix

| Scenario | Queue location | Drop folder | Typical transport | Notes |
|----------|----------------|-------------|-------------------|--------|
| A and B, **same machine**, **different repos** | Each repo has its own `.agent/queue` | **One shared path outside both repos** (e.g. `~/agentq-drop/to-b`) | file_drop | Both registry YAMLs reference the same absolute path for inbound/outbound. |
| A and B, **same machine**, **same repo** (e.g. worktrees) | Can share one queue or separate | Shared path or subdirs per direction | file_drop | Same as above if two agents still use distinct keys and registry entries. |
| A **local**, B **remote** | Each side its own queue | Sync folder (Dropbox/Drive/rsync) or **mail** | file_drop or mail | file_drop needs a folder both can see (sync or mount). If no shared FS, use mail. |
| A and B, **both remote**, no shared FS | Each queue local to that host | N/A for file_drop without sync | **mail** (or sync + file_drop) | file_drop requires a **common filesystem namespace** at some layer. |
| CI / headless B | B's queue on runner | Artifact upload dir or mail | file_drop to artifact dir or mail | B runs `ingest-blob` or `file-drop-poll` in CI with ephemeral key from secret store. |

---

## 3. Same machine, different repos (detailed)

**Goal:** Repo1 (agent A) ships a PRD to repo2 (agent B) without committing secrets.

**Setup:**

1. **Create a drop directory** not inside either repo, e.g. `/home/you/agentq/to-b` or `~/agentq/to-b`.
2. **Keys:** Each side keeps its private key in its selected isolated GnuPG home, imports the peer's pinned public certificate, and enrolls local authority explicitly.
3. **Registry on A's side:** Select B's full fingerprint, scope and outbound root in A's private version 2 registry.
4. **Registry on B's side:** Select A's full fingerprint, exact inbound recipient set and the corresponding inbound root. The root resolves to the same shared directory.

**A ships (from repo1):**

```bash
cd /path/to/repo1
python ls/tools/agentq_transport_client/agentq_cli.py ship-file-drop \
  --manifest /private/agent-a/manifest.json \
  --registry /private/agent-a/registry.yaml --peer agent-b \
  --out /private/agentq/to-b --queue /private/agent-a/queue
```

The manifest names `from_agent_id` and the exact `to_agent_ids`. The client
seals once and writes a random opaque `.agentq.lspgp` file with `.ready` last.

**B ingests (from repo2):**

One-shot:

```bash
cd /path/to/repo2
python ls/tools/agentq_transport_client/agentq_cli.py ingest-blob \
  /private/agentq/to-b/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.agentq.lspgp \
  --registry /private/agent-b/registry.yaml --peer agent-a \
  --queue /private/agent-b/queue
```

Or poll (cron/systemd) scanning B's inbound roots from registry:

```bash
python ls/tools/agentq_transport_client/agentq_cli.py file-drop-poll \
  --registry /private/agent-b/registry.yaml --peer agent-a \
  --queue /private/agent-b/queue
```

**What happens:** B's queue gains `in/<id>/` with PRD and optional attachments; A's blob moves under `processed/` under the drop dir. Ledger on **B's** queue records idempotency. A's repo is unchanged except ship_log if `--queue` was set.

**Common mistakes:**

- Using a **repo-relative** path on A that resolves differently on B (e.g. `./drop`): use **absolute** paths in registry.
- Forgetting the **ready marker**: ship-file-drop writes `.ready` after ciphertext; polling ignores incomplete pairs.
- Selecting the wrong local keyring or stale authority: verification fails before promotion.

---

## 4. Same machine, same repo (two agents)

If both agents are **roles** in the same clone (e.g. human + automated builder), you can:

- Use **two subdirs** under one drop base: `.../to-builder/`, `.../to-human/`.
- Or one queue with **flat** layout and manual `in/` drops (no adapter).

Each side still uses its own registry, keyring and authority store. Ship direction
comes from the selected `--peer`, exact manifest recipients and allowed root.

---

## 5. Local vs remote (no shared directory)

**file_drop** requires the sealed file to **exist on disk** where B can read it. If B is on another host with no mount/sync:

- Use **mail:** `ship-mail` from A; B runs `mail-pull` with the selected account and private registry.
- Or use a **sync folder** (Drive/Dropbox client) so both hosts see the same path eventually; then file_drop poll on B.

**Latency:** file_drop over sync is eventually consistent; ready marker + optional `sha256` first line in `.ready` reduces truncated-ingest risk.

---

## 6. Remote B with sync (step-by-step)

1. A and B agree on a **sync-relative** path that maps to the same logical folder on both machines after sync (e.g. `~/Dropbox/agentq/incoming`).
2. A `ship-file-drop --out ~/Dropbox/agentq/incoming`.
3. After sync completes, B runs `file-drop-poll` with `--root ~/Dropbox/agentq/incoming` (or registry inbound roots pointing there).
4. If sync creates conflict copies, **ignore_globs** in queue config should include `*conflicted copy*`.

---

## 7. Mail-only path (reference)

When file_drop is not available:

- A: `ship-mail` with the manifest, registry, selected peer, account and routing addresses.
- B: `mail-pull` with the queue, account, registry and selected peer.
- Post-ingest move to Processed avoids UNSEEN replay; use `mail-move-retry` if policy blocked the first move.

See client **ADMIN_GUIDE** and mail skill for policy tokens.

---

## 8. Decision guide for agents

Use this flow to pick transport:

1. **Can B read the same directory as A writes?** (same host path, NFS, or sync)
   - Yes -> **file_drop**: `ship-file-drop` + `ingest-blob` or `file-drop-poll`.
   - No -> **mail** (or add sync first).

2. **Who is the exact peer and recipient set?**
   - Select `--peer` from trusted local configuration, then use the registry's
     full pins and the manifest's exact recipients. All automated adapters use
     the same mandatory signed and encrypted binary envelope.

3. **Multiple recipients?**
   - Manifest `to_agent_ids` + `ship-file-drop-multi` + a pinned certificate
     for each recipient; one ciphertext can be delivered to all selected peers.

4. **Ack workflow?**
   - Manifest `ack_required`; use `queue-pending` to move `in/*` to `pending/` after promote.

---

## 9. File reference map

| Need | Doc or path |
|------|----------------|
| Registry shape | `ls/config/agent_trust_registry.example.yaml` |
| Queue config | `ls/config/agent_queue.example.yaml` |
| CLI commands | `ls/tools/agentq_transport_client/docs/USER_GUIDE.md` |
| Admin / policy | `ls/tools/agentq_transport_client/docs/ADMIN_GUIDE.md` |
| Protocol | `ls/docs/AGENTIC_AGENT_TO_AGENT_PROTOCOL.md` |
| Build order | `ls/docs/AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md` |

---

## 10. Glossary

| Term | Definition |
|------|------------|
| **Stem** | Random opaque filename base for `.agentq.lspgp` and `.ready`. |
| **Promote** | Atomic move from staging to `in/<transport_id>/`. |
| **Ledger** | Append-only JSONL idempotency log under queue `inbox/` and `out/`. |
| **Verified envelope** | Shared binary signed and encrypted format; ingest verifies complete integrity, exact participants and current authority before acceptance. |
