---
status: ACTIVE
version: 4.44
owner_skill: ls-architecture
---

# Framework library architecture

## Decision

LocalSetup framework capabilities belong in narrow, importable `ls/core/<domain>/` packages. Public `ls/tools/` files are thin direct-execution wrappers; the top-level `localsetup` CLI composes approved domains but does not contain their policy or filesystem logic.

The trusted review queue remains intentionally isolated in `ls/tools/trusted_work_queue/` for the released snapshot and shared-folder transport phases. It is not a packaged LocalSetup public API, a `localsetup` subcommand, or a harness extension. Promote it only when its phase-1/phase-2 contract or a second transport needs a reusable library boundary. Future deterministic materialization, isolation, and returned-result handling are external harness-owned concerns, not additional LocalSetup filesystem APIs.

## Boundaries

| Layer | Owns | Must not own |
| --- | --- | --- |
| `ls/core/<domain>/` | Typed domain models, deterministic rules, filesystem/network adapters, validation, and machine-readable results | CLI parsing, terminal rendering, agent/model selection, remote UI control |
| `ls/tools/<tool>.py` | Repository-root resolution, import setup, and `main()` delegation | Domain policy or compatibility logic |
| `ls/core/cli*.py` | Stable command routing, shared target selection, exit status, and output-mode integration | Queue transport mechanics or worker execution |
| Target-node helper | Authenticated node-local orchestration of approved LocalSetup APIs | Browser-facing control, terminal emulation, direct user sockets |
| Dashboard | Human-facing capability requests and bounded telemetry | Herdr sockets, tmux, WezTerm, queue filesystem mutation, or OmniRoute credentials |

The queue, dashboard/Herdr control plane, Agent Q, A2A, and OmniRoute inference routing remain separate systems. A queue packet contains immutable snapshot and opaque PRD bytes; it does not select models, teams, providers, prompts, or executor commands.

## Queue promotion gate

Do not move the current package merely for folder consistency. Promote it in the same accepted change that needs its public contract, using this shape:

```text
ls/core/trusted_work_queue/
  __init__.py       # lightweight exported types and errors
  models.py         # SnapshotMetadata, QueuePacket, QueueClaim
  snapshot.py       # full-tree archive creation and validation
  shared_folder.py  # local same-filesystem immutable transport
  transport.py      # small typed transport protocol after a second adapter exists
  cli.py            # queue-domain parsing and safe JSON/text rendering
ls/tools/trusted_work_queue.py  # direct-execution wrapper only
```

The current `ls/tools/trusted_work_queue/` package may be removed during that clean cutover; do not retain a second implementation or an import alias. The migration must update every documented invocation and test import together.

An S3-compatible adapter is the first plausible reason for `transport.py`. It must implement the same immutable deposit, ready-marker, claim, and provenance contracts. It must not cause a new dependency to be added until the dependency security review and explicit package approval gates have passed.

## Harness-extension gate

The existing `localsetup harness` surface is specific to Codex heartbeat and repository finalization. Do not make the trusted queue a harness topic and do not create a generic extension registry for one feature.

Introduce a typed `ls/core/harness_extensions/` registry only when two independently useful harness domains require lifecycle registration. Its minimal contract should expose an immutable identifier, `plan`, `status`, and explicit mutating operations with typed request/result values. The registry loads built-in extensions only; it does not import arbitrary user modules, execute remote code, or become a plugin channel.

## Evolution and validation

1. Keep snapshot and shared-folder transport isolated while their contracts settle.
2. When both shared-folder and a second transport share a stable phase-1/phase-2 contract, promote the domain into `ls/core/trusted_work_queue/` in one cutover.
3. Add a LocalSetup CLI surface only after the promoted library has focused unit tests and a safe, bounded output contract.
4. Consider a harness registry only after a second lifecycle extension demonstrates the shared need.

Every promotion must retain the focused snapshot and shared-folder transport tests; add thin-wrapper black-box tests and update generated documentation. Python framework changes must also satisfy [PYTHON_ARCHITECTURE_STANDARD.md](PYTHON_ARCHITECTURE_STANDARD.md), including its package responsibilities and architecture check.

## Current direct-execution surface

Until promotion, invoke the queue only as a repository-local module with `ls/tools` on `PYTHONPATH`:

```text
PYTHONPATH=ls/tools python -m trusted_work_queue.cli <command> ...
```

This is deliberately not an installed package or a `localsetup` command today. It keeps the current transport phase isolated from the framework's package and CLI compatibility surface.
