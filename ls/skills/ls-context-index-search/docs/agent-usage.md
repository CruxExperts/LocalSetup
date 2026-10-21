# Agent Usage

Use `context-index` before broad recursive reads when the capability is enabled. Verify stale or high-impact results against the files before changing code or making exact claims.

## Preflight

```bash
localsetup context-index agent-preflight --scope repo
```

Important fields:

- `vector_available`: whether vectors exist for the scope.
- `freshness.safe_to_use_index`: whether the indexed files are current.
- `freshness.agent_guidance.read_direct_paths`: files to read directly.
- `worklist`: pending extract/chunk/embed/tombstone counts.

## Search

```bash
localsetup context-index search "how are workflows registered" --scope repo --top-k 10
localsetup context-index search "skill validation smoke commands" --scope framework --mode hybrid --top-k 10
```

Result rows include rank, score, vector score, lexical score, scope, path, line range, snippet, chunk ID, source hash, chunk hash, and stale status.

Before trusting a query response, confirm its selected scope/context and freshness fields, then retain the paths, line ranges, chunk IDs, and hashes needed to reproduce important results. Inventory exclusions prevent known sensitive paths from entering the index; search does not add a separate redaction pass or a JSONL event for every query.

## Lookup

```bash
localsetup context-index lookup --chunk-id UUID
```

Use lookup when a search result is important enough to inspect in full. For code edits, still open the source file by path and line range.

## Freshness And Worklists

```bash
localsetup context-index freshness --scope repo
localsetup context-index stale-files --scope repo
localsetup context-index worklist --scope repo
localsetup context-index stats --scope repo
```

If a file is listed in `read_direct_paths`, read it directly before using index results for that file.

Choose maintenance from the observed state:

| Observed state | Action |
| --- | --- |
| No pending or stale work | No-op |
| Changed or not-indexed paths | `refresh` or `worker run` |
| Work should be queued for a worker | `worker nudge` (queues only) |
| Embedding profile/model/dimension changed | Reviewed `vector-rebuild plan/apply` |
| Index is contaminated or a clean reindex is required | Reviewed `rebuild plan/apply` |
| Deleted/tombstoned sources remain | Reviewed `prune plan/apply` |

After maintenance, rerun `agent-preflight`, `freshness`, or `worklist` and verify the expected transition. Preserve the input state, selected action, command JSON, and verification JSON; runtime operation events do not replace that decision record.

## Ingest And Refresh

```bash
localsetup context-index ingest --scope repo
localsetup context-index refresh --scope repo
localsetup context-index worker nudge --scope repo
localsetup context-index worker run --scope repo
```

`refresh` and `worker run` use changed-only ingest. `worker nudge` is safe for heartbeat because it only queues work when a deterministic worklist exists.

## Reset And Rebuild

```bash
localsetup context-index rebuild plan --scope repo
```

Review the returned `ok`, `context_key`, `mode` (`context_full` for rebuild), and `would_delete` fields. Copy its `plan_id`, then apply that exact reviewed plan:

```bash
PLAN_ID='<plan_id copied from the reviewed JSON>'
localsetup context-index rebuild apply --scope repo --plan "$PLAN_ID"
```

Use rebuild when the index is contaminated, when embedding settings changed, or when maintainers request a clean reindex. The database is disposable derived state.

For vector-only refreshes:

```bash
localsetup context-index vector-rebuild plan --scope repo
# Review that plan JSON, then copy its plan_id below.
PLAN_ID='<plan_id copied from the reviewed JSON>'
localsetup context-index vector-rebuild apply --scope repo --plan "$PLAN_ID"
```

For conservative cleanup of deleted/tombstoned indexed sources:

```bash
localsetup context-index prune plan --scope repo
# Review that plan JSON, then copy its plan_id below.
PLAN_ID='<plan_id copied from the reviewed JSON>'
localsetup context-index prune apply --scope repo --plan "$PLAN_ID"
```

## Agent Rules

- Prefer `repo` for repo work and `framework` for LocalSetup framework questions.
- Never ask the index for secret values.
- Mark stale results as context hints only.
- Prefer `ACTIVE` docs where status is available.
- Use direct file reads for exact edits and citations.

## MCP Config

```bash
localsetup context-index mcp config --scope repo
```

This emits a read-only stdio server command target for MCP-capable clients. Live serving is optional and requires enabling the MCP SDK wrapper; the deterministic CLI remains the supported baseline.
