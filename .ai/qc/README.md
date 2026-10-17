# GitHub QC Patrol

This directory defines the checked-in contract for repository-local QC patrols. Runtime output belongs in workflow artifacts under `qc-out/`; do not commit generated patrol state.

## Operating Posture

- Scheduled patrols open factual, duplicate-aware issues for deterministic findings; AI adjudications remain artifact-only.
- PR checks surface as normal GitHub Actions checks plus step summaries and artifacts.
- Fork PRs run deterministic checks only and do not receive LLM secrets.
- Autofix is manual-only and limited to allowlisted deterministic repairs.
- LLM requests use bounded chunks, redacted inputs, and strict JSON response schemas.
- Adaptive patrol AI is opt-in. The default scheduled mode records deterministic results and pending packets only.

## Workflow Surface

- `qc-ci.yml` runs deterministic inventory, self-validation, and focused tests on PRs, pushes to `main`, merge queue, and manual dispatch.
- `qc-pr-review.yml` runs trusted base-branch tooling against a separate subject checkout. Same-repo PRs use LLM secrets only when `QC_PR_LLM_ENABLED` is explicitly `true` (case-insensitive); otherwise review is deterministic. Fork PRs run without LLM secrets.
- `qc-patrol.yml` runs weekly/manual repository patrols, writes inventory and drift artifacts, and reports deterministic findings through duplicate-aware issues.
- `qc-docs-drift.yml` runs scheduled/manual canonical documentation-alignment handoff and may report factual expected/observed values.
- `qc-release.yml` is manual release-readiness validation only. It builds and verifies temporary artifacts without publishing.
- `qc-autofix.yml` is manual-only and writes no code in v1; it only emits an autofix plan for explicit allowlist IDs.

## Adaptive Patrol Artifacts

`patrol` writes runtime artifacts under `qc-out/`:

- `inventory.json`: `qc.inventory.v2`, including tracked file hashes and classified docs, workflows, generated artifacts, packages, skills, workflow packages, private paths, registry/catalog metadata, and version truth.
- `drift-packets.json`: `qc.drift-packets.v1`, including structural drift, bounded markdown version-reference packets, and changed-file hash evidence with short redacted excerpts for tracked skills, workflow packages, and GitHub workflows.
- `packet-dispositions.json`: `qc.packet-dispositions.v1`, mapping each stable `packet_id` to `completed` or `attempted`. An attempted packet may have an uncertain provider outcome and is never automatically dispatched again.
- `ai-adjudications.json`: `qc.ai-adjudication.v1`, with bounded model results, explicit errors, and the packet call count.
- `pending-packets.json`: `qc.pending-packets.v1`, retaining only unattempted packets deferred by the per-run call limit or an unavailable model route.
- `rule-suggestions.json`: `qc.rule-suggestions.v1`, retained for artifact compatibility; patrol responses do not propose rules.
- `ledger.json`: `qc.ledger.v2`, including inventory/drift summaries, deterministic findings, AI findings, rule suggestions, issue results, artifacts, and AI mode.
- `baseline-status.json`: separate lookup status for the most recent successful inventory baseline and the latest completed run's packet disposition/queue state.

The workflow selects inventory only from the most recent successful patrol run. It separately reads disposition state and the deferred queue from the latest completed run, including failed runs, so successful model calls and uncertain attempts survive a later run failure. Completed and attempted IDs are filtered from regenerated packets. The attempt marker and remaining queue are persisted before each provider dispatch; an error after dispatch does not requeue that packet, while later unattempted packets remain deferred.

With no previous patrol run, the workflow performs deterministic bootstrap and writes an empty disposition receipt without model calls. Packet mode requires both a successful inventory baseline and a valid latest-completed disposition/queue receipt. Missing, unavailable, or corrupt legacy state fails closed and is not silently reset. After reviewing the lost history, set `QC_PATROL_AI_MODE=off` and manually dispatch the workflow with `initialize_packet_dispositions=true`; the next run can use packet mode once a successful inventory baseline exists.

Tracked-file hash changes are evidence of local change only. They are informational packets and do not prove stale upstream provenance or create issues. No canonical verifier currently checks external upstream source revisions. `validate-catalog` checks local catalog structure, while the existing docs-alignment check can report factual expected and observed generated-document values.

The docs handoff creates at most one bounded issue from parseable canonical alignment-check findings. It includes the check, affected paths, factual messages, and bounded expected/observed values; it does not produce model-written recommendations. If the check fails without parseable finding details, it records the unavailable result and fails without opening a stale-provenance issue.

## AI Mode

`patrol --ai-mode off` is the default. It writes deterministic artifacts and keeps unattempted packets pending without sending model requests.

`patrol --ai-mode packets` sends bounded, individual factual packets through the existing protected completion runtime. It requires an explicit valid disposition receipt and a successful inventory baseline; a first run without prior history cannot call models. `QC_PATROL_MODEL` must name a concrete model explicitly; `Agent-QC`, `auto`, `default`, and `premium` are rejected, and the shared `QC_LLM_MODEL` is not inherited. At most four packet requests are attempted per run, each with an output-token cap of 1,000 and a 24,000-byte prompt limit. There are no retries or model escalation. Low reasoning effort is sent only when `low` appears in `QC_LLM_REASONING_EFFORTS`.

The prompt contract is `.ai/qc/prompts/patrol.md`, and each response must match `.ai/qc/schemas/ai-adjudication.schema.json`. Responses are limited to factual packet observations; actionable recommendations, rule proposals, and issue creation are prohibited. Results remain artifacts only: they do not create issues, mutate files, create pull requests, or bypass policy.

The packet-mode CLI validates its model, credentials, and verified managed runtime even when there are no packets; an unavailable route is recorded as an error and preserves the current unattempted queue. It never installs a runtime or switches providers/models.

## Issue Policy

The existing patrol issue writer reports deterministic findings only. It does not open issues for source hash changes, speculative model findings, or model recommendations. The docs handoff reports only canonical alignment-check facts. Existing identical findings are left unchanged without a new comment; changed facts under the same finding identity receive a factual update.

## Secrets And Vars

Secrets: `QC_LLM_BASE_URL`, `QC_LLM_API_KEY`, optional `QC_LLM_ORGANIZATION`, optional `QC_LLM_PROJECT`. The weekly workflow injects them only into the opt-in model-assisted step.

PR model-review opt-in: `QC_PR_LLM_ENABLED` is unset/off by default. See [repository maintenance](../../ls/docs/REPO_MAINTENANCE.md#optional-pr-model-review) for authorization and disabling behavior.

Shared QC transport variables include `QC_LLM_API_STYLE`, `QC_LLM_ENDPOINT_ALIAS`, `QC_LLM_REASONING_EFFORTS`, and the optional `QC_LLM_RUNTIME_ROOT` for an already verified managed runtime. Patrol uses `QC_PATROL_AI_MODE` (`off` by default) and an explicitly selected `QC_PATROL_MODEL`; it does not inherit `QC_LLM_MODEL`.

Endpoint URLs and secret headers must not be written to issues. Issue bodies contain only deterministic finding details.

## Repository Settings

- Enable GitHub Actions for the repository.
- Keep the default token posture restrictive; each QC workflow declares the job-level access it needs.
- Allow `issues: write` only for patrol workflows that create or update issue handoffs.
- Enable "Allow GitHub Actions to create and approve pull requests" only if a future autofix implementation actually creates PRs. The v1 `qc-autofix.yml` workflow remains manual-only and plan-only.
- Do not add `pull_request_target` to QC review workflows. Untrusted fork PRs must not receive LLM secrets or write credentials.

## Release Boundary

The QC workflows live in `.github`, which is otherwise part of the public LocalSetup artifact. Because the QC tooling lives at root `tools/qc_patrol/` and is not shipped with the framework package, the exact `qc-*.yml` workflow paths are listed in `ls/config/pack.yaml` under `public_private.private_paths`.

Runtime output belongs in `qc-out/` workflow artifacts. Do not commit local patrol ledgers, raw LLM responses, or generated runtime state.

## Protected completion compatibility

QC's `LLMClient.complete(...)` retains its string result, default review schema
and prompt redaction. It uses LocalSetup's protected completion worker and shared
provider transport. Provision a verified managed runtime before enabling QC model
work; the wrapper never installs one automatically. `QC_LLM_RUNTIME_ROOT` selects
an explicit root, otherwise the default LSCli runtime location is used. Missing
credentials fail before dispatch. Model failures raise sanitized errors without
provider bodies, endpoint URLs or credentials. Successful JSON is compactly
serialized; formatting is not preserved byte-for-byte.

Both API styles and base URLs that include the selected endpoint suffix remain
supported. Explicit organization/project values are retained in the final request
headers; ambient SDK settings are ignored. Optional `QC_LLM_REASONING_EFFORT` must
be listed in comma-separated `QC_LLM_REASONING_EFFORTS`. Optional temperature is
sent only with `QC_LLM_TEMPERATURE_SUPPORTED=true`; the legacy zero default is
omitted otherwise, while an unsupported nonzero value refuses. The schema-name
argument is retained. `QC_LLM_ALLOW_LOOPBACK_HTTP=true` is only for explicitly
selected literal loopback fixtures; other endpoints require HTTPS.

`QC_LLM_RETRY_COUNT` remains readable for existing configuration, but no value
enables retries: the accepted completion policy permits one attempt. Transport
uncertainty is not replayed. The configured timeout bounds the protected worker,
including validation. The wrapper creates no sessions, configurations or recurring
jobs and preserves the existing caller-owned redaction and error-artifact flow.
