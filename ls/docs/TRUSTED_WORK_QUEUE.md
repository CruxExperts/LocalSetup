---
status: ACTIVE
version: 4.44
owner_skill: ls-architecture
---

# Trusted work queue: full repository snapshots

This document specifies a dedicated trusted remote-work queue. The released package contains phase 1's portable snapshot contract and phase 2's local shared-folder deposit and claim transport. It is independent of Agent Q and has no model, provider, chat, RPC, callback, or network implementation. Future deterministic materialization, isolation, execution, and returned-result handling are outside this package and belong to an explicitly separate harness-owned contract.

## Phase 1 scope

The controller packages an existing repository directory as a complete snapshot. The package retains the full directory tree, including dirty and untracked work, ignored context, caches, configuration, self-contained Git metadata, hidden paths, and ordinary files. It does not run a Git cleanup, reset, checkout, filtering pass, extension allowlist, size cap, dependency installation, or secret scan.

The archive is a gzip-compressed POSIX/PAX tar stream with exactly one top-level directory. Its top-level name is the source directory basename and must match the safe-name rule `[A-Za-z0-9._-]+`, excluding `.` and `..`. The archive preserves regular files, directories, and permission bits. Every archive member whose basename is `.git` must be a directory at any depth; Gitfiles such as linked-worktree or submodule pointers are rejected because they reference external Git metadata and cannot form a self-contained archive. Symbolic links and hard links are rejected, along with FIFOs, sockets, device nodes, and other non-portable special filesystem entries, before the archive is committed. The output archive must be outside the source tree.

The archive is written to a temporary file on the destination/drop filesystem. Compressed bytes are hashed as they stream and the temporary file is flushed and fsynced. It is then atomically hard-linked into a previously unused final path, never replaced. The adjacent sidecar uses the same no-clobber publication and is written last as the ready marker. Creation refuses an existing archive or sidecar, including a concurrent publication collision, so callers must choose immutable unique packet paths. Neither archive contents nor a file list is loaded into the manifest or emitted in normal CLI output.

The implementation reads discoverable Git `HEAD` before and after streaming. A changed resolved revision aborts before publication, preventing the manifest from claiming Git provenance that does not match the archive operation.

No Agent Q protocol or Agent Q attachment is used. The snapshot package is deliberately model-agnostic and does not select teams, models, providers, routes, or an executor.

## Sidecar manifest

For archive `repo.tar.gz`, the required adjacent sidecar is `repo.tar.gz.manifest.json`. The JSON object contains these fields and no file list or file contents:

| Field | Meaning |
| --- | --- |
| `format_version` | Integer `1` for this contract. |
| `source_root_name` | The validated, safe top-level archive directory name. |
| `archive_sha256` | Lowercase SHA-256 of the complete compressed archive bytes, hashed during streaming. No creation timestamp is added by the archive writer. |
| `total_bytes` | Compressed archive byte count. |
| `git_head` | Full Git object id when discoverable from the accepted source `.git` directory; otherwise `null`. This is provenance only and does not represent ignored or local filesystem context. |
| `job_id` | Lowercase SHA-256 of canonical JSON containing only `format_version`, `source_root_name`, `archive_sha256`, `total_bytes`, and `git_head`. It is deterministic and has no creation time, path, model, or PRD content input. |
| `job_identity` | Reserved nullable opaque queue/job identity. Phase 1 does not parse it or use it in `job_id`. |
| `prd_identity` | Reserved nullable opaque PRD identity. Phase 1 does not parse PRD content or use it in `job_id`. |
| `master_remote` | Reserved nullable read-only master/provenance identity. Phase 1 never contacts it. |
| `master_ref` | Reserved nullable immutable master revision/ref used by a later controller, not by snapshot creation. |
| `source_fork` | Reserved nullable source/fork provenance identity. |

`job_identity`, `prd_identity`, `master_remote`, `master_ref`, and `source_fork` are intentionally opaque strings when populated by a later controller. They are not interpreted as instructions, URLs to fetch, model selectors, or queue commands. The complete UTF-8 sidecar, including those opaque values, must not exceed 16 KiB; validation rejects larger manifests before JSON decoding. For an accepted `.git` directory, its existing remotes and refs remain inside the full archive exactly as source files; snapshot creation performs no fetch or host contact. Gitfiles that point to linked-worktree or submodule metadata are rejected rather than archiving a host-local pointer.

The phase-1 API returns archive and sidecar paths plus the parsed metadata object. Validation recomputes the archive size and SHA-256, checks the adjacent sidecar and deterministic `job_id`, checks that every archive member is contained beneath `source_root_name`, rejects absolute/traversal/backslash/control-name members, requires the root directory entry, and rejects special tar members. It does not extract anything.

Archive validation bounds member bookkeeping at 100,000 entries and rejects member 100,001 before retaining its metadata. This bound applies to validation only; it does not interpret repository code or promise any downstream candidate operation.

## Exact CLI contract

The module is invoked with the repository `ls/tools` directory on `PYTHONPATH`:

```text
python -m trusted_work_queue.cli snapshot-create SOURCE_DIR ARCHIVE_PATH
python -m trusted_work_queue.cli snapshot-validate ARCHIVE_PATH
python -m trusted_work_queue.cli shared-deposit QUEUE_ROOT ARCHIVE_PATH PRD_PATH --replication-count N
python -m trusted_work_queue.cli shared-list QUEUE_ROOT
python -m trusted_work_queue.cli shared-claim QUEUE_ROOT
```

`SOURCE_DIR` must already exist and be a directory. `ARCHIVE_PATH` must be a unique output filename/path outside the source tree; no extension is required, although `.tar.gz` is conventional. Creation refuses to replace either that archive or its adjacent `.manifest.json` sidecar, writes the sidecar only after publishing the archive, and prints only safe metadata JSON on stdout. Validation requires that sidecar and prints the metadata with `"valid": true` on stdout. Expected input, filesystem, archive, or validation failures print a short diagnostic to stderr and return exit status `1`; successful commands return `0`; argparse usage failures return its normal status `2`.

## Python API contract

`trusted_work_queue.snapshot` exposes:

```python
create_snapshot(source_dir, archive_path) -> SnapshotResult
validate_snapshot(archive_path, manifest_path=None) -> SnapshotMetadata
manifest_path_for(archive_path) -> pathlib.Path
job_id_for_metadata(*, format_version, source_root_name, archive_sha256,
                    total_bytes, git_head) -> str
```

`SnapshotResult.archive_path`, `SnapshotResult.manifest_path`, and `SnapshotResult.metadata` identify the committed outputs. `SnapshotMetadata.as_dict()` is the sidecar schema above. `SnapshotError` reports creation failures and `SnapshotValidationError` reports validation failures. `manifest_path` is optional only for an explicit pairing check; it must resolve to the archive's adjacent sidecar.

## Future harness-owned boundary

This release stops after phase 2. The LocalSetup filesystem package does not materialize candidates, isolate or execute repository copies, dispatch agents or models, validate returned results, delete claims, or contact network endpoints. Any future deterministic materialization, candidate isolation, execution, or returned-result handling must be specified and implemented by an explicitly separate harness-owned contract. That contract is not a LocalSetup command, import, export, packet-schema extension, or promise that `replication_count` creates candidates.

## Phase 2: shared-folder packet transport

`trusted_work_queue.shared_folder` defines a source-owned, same-filesystem transport rooted at a user-selected local directory:

```text
QUEUE_ROOT/
  .queue-operation.lock
  incoming/
    <snapshot-job-id>/
      snapshot.tar.gz
      snapshot.tar.gz.manifest.json
      prd.bin
      packet.json
  claims/
    <snapshot-job-id>.claim
    <snapshot-job-id>/
      snapshot.tar.gz
      snapshot.tar.gz.manifest.json
      prd.bin
      packet.json
```
`deposit_packet(queue_root, snapshot_archive, prd_path, replication_count=N)` first validates the source snapshot, streams the archive, adjacent sidecar, and PRD bytes into a unique hidden staging directory under `incoming/`, fsyncs each member, validates the staged snapshot again, then hard-links `packet.json` as the no-clobber ready marker and atomically renames the fully durable directory to `incoming/<job-id>/`. Hidden staging directories are excluded from discovery and cannot be listed or claimed. A failed staging operation removes only its unique staging directory, so it cannot reserve the final job name or block a valid retry. `prd.bin` is verbatim opaque bytes: this transport computes its SHA-256 and byte count but does not decode or interpret it. The ready marker contains only version, snapshot job/digest/size, PRD digest/size, replication count, and canonical UTC enqueue time.

Every packet uses the snapshot's deterministic `job_id`; a second deposit of the same snapshot job refuses to overwrite an incoming packet, retained claim directory, or claim reservation marker. Packet files are owner-only and ready markers are the only readiness signal. Incomplete directories have no marker and are never returned by `list_ready_packets`.

`claim_oldest_packet(queue_root)` validates every ready packet, orders them by `(enqueued_at, job_id)`, and reserves the oldest with an exclusive `claims/<job-id>.claim` marker before atomically moving its packet directory from `incoming/` to `claims/`. It never claims a newer packet when the oldest is malformed, disappears, or has an existing reservation. The claim deliberately retains the complete packet; no source input is deleted by this phase.

Deposits and claims use cooperative same-job serialization through the persistent owner-only `.queue-operation.lock` at the queue root. The lock is acquired with a descriptor-held exclusive `flock`; descriptor/process lifecycle releases it safely after a crash, without stale lock-file cleanup. A deposit holds the lock only for its final incoming/claims existence checks, native no-replace publication, and incoming-directory fsync, preventing a same-job duplicate from replacing or racing a retained packet. A claim holds the same lock across ready-packet list/select, claim-marker creation, the move into `claims/`, and the required directory fsyncs. The threaded regression `test_same_job_deposit_and_claim_are_serialized_at_final_publication` verifies that a competing claim waits for a same-job no-clobber deposit failure and then retains exactly one claim.

`replication_count` is validated and retained in `packet.json` as transport metadata only. This release does not interpret it as a candidate count and makes no materialization, isolation, or execution promise. The queue root remains a directional local storage surface with no network client, agent dispatch, model selection, downstream execution, credential handling, or automatic stale-claim cleanup. An external controller may inspect queue depth or size separately, but senders do not need read access to consumer-side payload storage.

## Intended fixture coverage

`ls/tests/test_trusted_work_queue_snapshot.py` contains standard-library `unittest` fixtures collected by the normal LocalSetup pytest suite and intended to cover:

- hidden files and `.git` inclusion;
- symbolic-link and hard-link rejection plus permission-bit retention;
- output-inside-source rejection;
- unsafe source-root basename rejection;
- source FIFO rejection and archive special-member rejection;
- archive tamper detection by sidecar SHA-256/byte-count mismatch;
- safe single-root member containment, including traversal members;
- no-clobber collisions for archive and sidecar publication; and
- parent-directory fsync failure cleanup with no ready marker.

`ls/tests/test_trusted_work_queue_shared_folder.py` additionally covers:

- streamed verbatim PRD bytes with no PRD content in ready-marker output;
- no-clobber duplicate packet refusal;
- staging cleanup and retry after an interrupted copy;
- no-replace behavior when a competing final packet appears;
- deterministic `(enqueued_at, job_id)` ordering and retention of claimed packet contents;
- malformed oldest packet blocking newer claims; and
- cooperative same-job deposit/claim serialization at final publication; and
- shared-folder CLI deposit/list/claim metadata output; and
- argparse rejection of commands outside the released phase-1/phase-2 surface.

Run the focused snapshot and shared-folder fixture modules before accepting changes to these phase-1/phase-2 contracts.
