---
status: ACTIVE
version: 4.89
owner_skill: ls-localsetup-framework-changes
---

# Repository Maintenance

## Test resource safety

Source pytest runs automatically enter a verified Linux cgroup v2 boundary
before starting workers. Maintainer validation requires a systemd user manager;
macOS framework usage is unchanged, but run this source suite on a qualified
Linux host or CI runner. Missing resource controls stop the run rather than
silently running without protection.

The `testing` section in `localsetup.json` owns the settings; the old
`[tool.localsetup.testing]` table remains a compatible fallback. `memory_percent`
defaults to 50 and may be lowered. An explicit `memory_limit_mib` requests an
absolute budget, always capped by `physical_memory_percent` (95 by default,
never above 95), currently available RAM minus host reserve, and tighter native
limits. Reserve the larger of `host_reserve_percent` (5 by default) and
`host_reserve_mib` (512 by default). These settings leave adjustable headroom for
other applications; they cannot guarantee those applications will not page.
`worker_memory_mib` defaults to 1024 and limits worker concurrency using that
budget. It is a scheduling allowance, not a per-process memory guarantee.
The whole test tree shares the hard memory limit, a 90% pressure threshold,
zero swap allowance, a CPU quota based on the default one-third CPU formula,
and `tasks_max` (1024 by default, including threads). An enclosing CPU quota
can reduce the test quota further. Numeric worker overrides cannot raise the
kernel CPU quota. A two-hour scope deadline bounds abandoned runs.

The launcher reads back the actual kernel limits before dispatch. Group OOM
termination stops the test tree on memory exhaustion; interruption and ordinary
exit stop the uniquely named scope, including detached fixture processes.
A per-user lock prevents independent local runs from overlapping. An active or
stopping scope left after a controller crash blocks another run until its owner
reconciles it. Never delete the lock or retry to bypass that check. Inspect the
reported unit with `systemctl --user status UNIT`; stop that exact owned test
unit with `systemctl --user stop UNIT` when its cancellation is authorized.

Both hosted pytest entrypoints prepare the ephemeral runner's user manager and
use this same guard. Each matrix shard keeps one worker. xdist child workers
must retain disabled distribution: re-enabling it recursively creates worker
pools. The regression check covers that boundary. Resource exhaustion fails
validation and must not be reported as a passing suite.

## Adaptive workers and engineering provenance

Local pytest uses one queue slot per active worker. The controller owns unstarted
work and rebalances it across remaining slots when a worker retires. A new worker
receives a balanced share of pending work. Running and acknowledged tests keep
their ownership. xdist's one-item lookahead means a retiring worker drains its
already dispatched pair before exit; no active test is interrupted by an ordinary
downshift. Collection must agree before any assignment or pressure retirement.

Before filling queues, the shared ordering component mixes case identities using
a recorded random seed and a stable hash order, then deals work evenly across
slots. Related files no longer form contiguous batches by default. Local runs
choose a fresh seed; `--test-seed N`, `LOCALSETUP_TEST_SEED`, or `shuffle_seed`
reproduces a run. GitHub matrix jobs derive one seed from `GITHUB_RUN_ID`; a failed
job rerun retains that seed so its coverage still matches already successful
jobs. GitHub documents that [run IDs persist across reruns](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).
Other CI matrices should supply one shared seed; without one, sharded mode
uses a documented seed of zero to preserve coverage. `shuffle: false` or
`--no-test-shuffle` preserves execution order within the selected partition.
Random mixing reduces clustering risk but cannot guarantee balanced memory use;
the resource guard and adaptive pressure policy remain necessary.

The deterministic policy samples each second. Pressure at or above 80% for five
continuous seconds lowers the target by one. Pressure below 70% for 30 continuous
seconds permits one additional worker. Normal transitions have a 15-second
cooldown; each increase resets recovery dwell. Leaving a threshold band resets
its dwell. These separate thresholds and timers prevent oscillation. Configure
them under `testing.adaptive`, with recovery < target < critical <= 95.

At 95%, the target immediately drops to the configured minimum. If critical
pressure lasts five seconds, an independent outer watchdog stops only the owned
test scope, first with TERM and then systemd's ten-second KILL fallback. The result
is failed/interrupted with incomplete coverage, never a passing partial suite.
The hard cgroup memory limit remains a final fallback throughout execution.

Pressure measures charged memory minus reclaimable inactive file cache, against
the smaller of the hard allowance and currently usable physical/parent-cgroup
headroom. Total charged memory still counts toward the kernel limit. The usable
window shrinks as other workloads consume RAM; no cache flush or host-wide process
kill is performed. Initial worker capacity also reserves `worker_memory_mib` per
worker within the target pressure fraction. The configured minimum cannot exceed
the physically feasible maximum. Fixed maximum-one execution bypasses adaptive
sampling and queue calculations; a pool dynamically reduced to one continues
sampling so it can recover. Hard resource controls apply in both cases.

The reusable boundaries are `test_pressure` (policy and measurement),
`test_scheduling` (queue ownership), `test_scheduling_runtime` (qualified xdist
adapter), `test_supervision` (independent emergency cleanup), and `test_plugin`
(pytest integration). Use `localsetup test-run` from another application rather
than copying these modules; see the [command reference](COMMAND_REFERENCE.md).
The plugin qualifies xdist 3.8.0 and rejects incompatible adaptive interfaces.

Each run reports its owned scope, effective kernel budget, requested memory,
policy digest, pressure transitions and completion count. Retain those records
with the exact source commit, selected configuration, command and exit status as
engineering provenance. Focused tests cover debounce, pressure limits, collection
replacement, emergency cleanup and queue conservation; the native probe verifies
real worker retirement, replacement, exact test coverage and exited process IDs.
Reusers must validate their own workload, runtime and containment environment.

### Live telemetry data contract

The supervisor publishes a versioned, atomic JSON snapshot independently of the
terminal. Its [schema](../config/test-telemetry.schema.json) covers run and process
identity, sequence and timestamps, lifecycle and cleanup, configured/requested and
effective worker limits, queue counters, seed, scaling reasons, memory and kernel
task counts. The pytest controller supplies a separate run-bound progress input;
workers and nested pytest processes cannot replace the parent's counters. The
outer watchdog retains independent memory measurement so a blocked pytest
controller cannot hide critical pressure. These local records are observational
data, not signed grants or an isolation boundary against other same-UID code.

```bash
localsetup test-workers --status --json
localsetup test-workers --watch
localsetup test-workers --watch --json
```

The viewer only reads the snapshot. JSON watch mode emits one record per new
sequence; the terminal view renders the same fields. Stopping a viewer never
signals the tests. A missing, stale or different-project snapshot is explicit.
The per-user runtime snapshot survives completion until the next protected run
or reboot. Retain a CLI JSON export alongside exact source/configuration evidence
when durable run provenance is needed. No terminal scraping is required.

`active` workers are processes in the primary pytest pool. Kernel `tasks` includes
all processes and threads in the guarded scope; do not add those two counts
together. The compact display labels this `OS thr` (including each process's main
thread); it rises and falls as tests launch and reap subprocesses. `done`
counts finished test cases, including failed or skipped cases, rather than only
passes. `dispatched` includes xdist's prefetched item and is not a running-test
count. Adaptive counters satisfy total = completed + unresolved + pending +
dispatched. Serial/native schedulers explicitly report unavailable queue details
as null. Resources and progress have separate observation timestamps; a final
snapshot retains the last measurement, while cleanup is independently confirmed.
Unknown collection totals remain null. Fixed one-worker runs still publish
telemetry but never construct an adaptive pressure controller.

Snapshots follow the pressure sample interval (bounded by the telemetry interval).
`telemetry_interval_seconds`, default 5, controls periodic human-readable output.
Each periodic update starts and ends with a newline so pytest activity dots
continue below the telemetry instead of sharing its line.
Pytest uses one serialized terminal writer for progress and two fixed 80-column
telemetry rows. Percentages occupy the right edge; dot chunks fill the remaining
columns. The outer watchdog continues sampling and writing the data interface
independently, without competing for pytest's terminal output. Serial runs use
the same display owner. Structured diagnostic JSON retains its exact format.
The `[LS]` prefix uses the selected theme's accent (blue in the shipped default)
on color-capable terminals. Muted labels and
bright values distinguish fields: `wrk` is active/maximum workers, `tgt` is the
desired worker count, and `done` is completed/total tests. `RAM` shows working
memory followed by charged/hard-limit memory in GiB. `NO_COLOR` takes precedence
over `FORCE_COLOR`; redirected output is plain unless color is explicitly forced.
Managed commands inherit presentation context from their owning shell session;
each output refresh reads its current theme and `colors_enabled` preference.
Outside managed sessions, the existing `agent-shell` runtime profile supplies
the theme, without creating preferences. This context affects display only,
never execution authority. Invalid presentation context falls back to plain text.
The version beside `[LS]` comes from `framework_version()` and is recorded as
`framework_version` in each new run's snapshot. Older snapshots show `v?` rather
than borrowing the viewer's version and misidentifying the measured run.
The JSON interface also includes the effective memory window, reserved host
headroom, reclaimable cache estimate, hard limit and pressure percentage even when
a narrow terminal cannot display every field.

This checklist records the GitHub settings and repo-maintenance conventions that are not fully represented by tracked files. Keep tracked automation in `.github/`; apply remote settings in GitHub after validating the workflow names emitted by Actions.

## Structured failures and reviewed feedback

LocalSetup records operational exceptions as bounded private events, with the
operation, error code, effect certainty, redacted traceback, source commit,
source/target paths, and capture/publication policy. Capture is best effort and
does not replace the original failure. Inspect summaries with
`localsetup health events --limit 20`; use its `next_cursor` with `--after` for
the next page. `localsetup health events --event EVENT_ID` returns one bounded
private event, including its traceback and digest. Private paths in that view
are debugging context and are not publication-ready.

Run `localsetup health request draft --event EVENT_ID` to prepare a deterministic
request describing the problem, desired outcome, impact, evidence, and
constraints. The draft excludes raw traces and applies best-effort redaction of
common private-path patterns and email addresses. This cannot identify every
sensitive path, hostname, or identity; review the exact prepared payload before
authorizing publication. Optional prose uses
`localsetup health request observe --event EVENT_ID --event-digest DIGEST --producer NAME --text-file FILE`;
regenerate the draft afterward. Observations are attributable and tied to the
event digest, so changed evidence cannot silently reuse an earlier observation.

The install configuration's optional `feedback` object owns `repository`
(`OWNER/REPO`), `labels`, `capture_enabled`, and `auto_draft`. Select it with
`localsetup health --config CONFIG request prepare --event EVENT_ID`. The
read-only preparation checks repository identity and open/closed duplicates,
then returns the exact title, body, destination, action, and plan digest for
review. It uses `GH_TOKEN` or `GITHUB_TOKEN` for GitHub reads. Inventory and
request budgets stop incomplete duplicate checks; they never imply no match.

Only after explicit approval of that destination and payload, invoke
`localsetup health --config CONFIG request submit --event EVENT_ID --approve-plan PLAN_DIGEST`.
It rechecks the bindings and serializes the target write. After an uncertain
response, it reconciles by reads without replaying the write. Configuration,
capture, drafting, and preparation never grant publication authority. This flow
supports configured repositories on `github.com`; it does not claim GitHub
Enterprise write support. Use the same configuration and workflow in another
repository through the existing GitHub maintenance adoption recipe.

## GitHub Repository Enhancement

For a GitHub-hosted repository, use the CLI-first
[GitHub repository enhancement workflow](../workflows/ls-wf-github-repository-settings-audit/SKILL.md)
to audit seven groups and every control in the fixed registry: identity and
discovery, collaboration, Git governance, Actions and deployment, security
and supply chain, releases, and repository content. The registry is the exact
coverage contract for this workflow, not an exhaustive inventory of every
possible GitHub control. A coverage receipt records status, expected and
observed counts, and missing, duplicate, invalid, or incomplete control IDs.
Each control observation carries applicability, authority, capability,
observation state, and safe evidence or a reason. Reason-backed unknown,
unavailable, inherited, local, UI-only, and not-applicable states are assessed
evidence; they do not imply a requested policy is satisfied. Report-only
results distinguish inherited, permission-limited, plan-gated, tracked-file,
observational, and UI-only areas. Unsupported controls remain report-only
unless a documented typed mutation exists.
Remote write operations are enabled only for `github.com` while the GitHub
Enterprise Server API-version matrix remains unverified. Audits and reads may
run on other GitHub hosts; requested drift there remains incomplete/report-only
with a compatibility reason.

Select the remote explicitly with
`localsetup github-repo --repository OWNER/REPO --hostname HOST --checkout PATH --mode MODE`,
where `MODE` is `audit`, `plan`, `apply`, or `verify`. `--checkout PATH`
selects the local Git checkout and defaults to `.`; it is distinct from global
`--repo`, which still selects LocalSetup's source checkout. Use the same
checkout path for audit/plan and apply/verify.
Audit, plan, and verify leave Dependabot, code-scanning, and secret-scanning
alert counts unread unless the current command explicitly includes
`--include-security-alerts`. Without it, the three counts are null with reason
`security_alert_reads_not_selected`; feature settings remain covered and health
is `not_assessed`. The option requests aggregate counts only and is rejected
for apply. A saved plan or earlier count does not authorize a fresh read.
Plan mode requires `--policy POLICY.json`; verify mode requires
`--plan PLAN.json`. The ordinary plan excludes visibility changes,
default-branch replacement, collaborator or secret changes, deleting rulesets,
and reducing protections. Default-branch replacement is a supported typed
operation only in its own policy and plan, after the target branch and head
are observed. Other excluded changes need their own reviewed policy, plan,
digest, and exact operation authorization. On `github.com`, public/private visibility
must be the sole setting in its own plan; split or reject a policy that mixes
it with other changes before apply. Visibility on other hosts is report-only
while Enterprise Server support is unverified. The documented CLI and REST
handling of `internal` is unresolved.
Global `--repo` retains its LocalSetup source-root meaning. Apply requires the
reviewed plan's exact SHA-256 digest and each approved operation ID; the plan
binds host, immutable GitHub repository ID, and authenticated actor. A
per-user target lock and journal serialize writes for one remote identity.
After an uncertain response, reconcile by read only and do not replay the
operation automatically. See the
[Command Reference](COMMAND_REFERENCE.md#github-repository-enhancement) for
command examples and the workflow package for official source links, limits,
pagination, redaction, and the precise social-preview upload handoff.

Saved plans use schema v4; regenerate any earlier schema-v2 or schema-v3 saved
plan from its reviewed policy before apply or verify. Policy remains schema v2.
The schema-v4 plan binds checkout root identity, HEAD, branch/detached
state, staged/worktree/untracked counts, a digest of porcelain status bytes,
configured upstream, and normalized origin/upstream matches to the requested
`OWNER/REPO`. The digest also covers structural observations and SHA-256/size
for only the bounded tracked candidate files inspected; no raw remote URL,
credential, absolute checkout path, raw status path, or content is returned.
This evidence reports file presence/structure, not quality. Apply refuses an
unsupported checkout, incomplete registry coverage, or local drift and checks
the local binding before each remote write. Verify returns `incomplete` for
incomplete coverage or local drift, as well as failed read-backs or unmet
requested policy. A complete coverage receipt can contain reason-backed
unknown or unavailable observations while policy remains unresolved.

Policy may optionally request signature and release proof through
`verification.signatures.{commit_oid,tag_name,expected_primary_fingerprints}`
and `verification.release.{release_id,tag_name,source_ref,source_commit,signer_workflow,predicate_type,artifacts}`.
Each release artifact binds `asset_id`, `name`, checkout-relative `path`, and
`expected_sha256`. Verify checks the selected commit and annotated tag against
the declared primary fingerprints, then binds the exact local artifact bytes
and digest to the specified release asset and the required source, signer
workflow, and predicate identities. Trust keys are verify-only inputs supplied
with repeatable `--trusted-public-key FILE`; key material and key paths are
not saved or printed. Reports omit absolute checkout paths and raw `gh`
output. If the policy requires release proof and the installed `gh` lacks
`release verify-asset` or `attestation verify`, report verification as
unavailable/incomplete; do not install or upgrade `gh` automatically. When no
verification requirements are selected, release readiness is
`not_assessed`. An `apply` completion describes only selected settings
operations and their read-backs, not release readiness.

Each typed operation displays a canonical interface descriptor in plan JSON
and Markdown. It identifies the transport, fixed command or HTTP
method/endpoint template, `plan.target` binding, required flags, and API
selection reason; this descriptor is bound into the operation identity and
plan digest. Prefer native `gh repo edit` when exact supported flags express
the complete operation. Before dispatch, bounded `gh repo edit --help` output
must confirm every required flag. Missing commands, flags, or unsupported
features fail closed; do not silently change transport after a failure. REST
via `gh api` is selected only where the native command cannot exactly express
the complete operation, with its reason in the descriptor. Ruleset mutations
are REST-only because `gh ruleset` exposes read/list/check/view commands, not
write commands.

Policy schema v2 supports the desired Boolean
`repository.web_commit_signoff_required` for the registered
`collaboration.web_commit_signoff` control. `true` requires signoff on commits
made through GitHub's web interface; `false` removes that requirement. The
official [`gh repo edit` options](https://cli.github.com/manual/gh_repo_edit)
do not document an exact native flag for this setting, so its typed operation
uses `PATCH /repos/{owner}/{repo}` through `gh api` and records the REST
selection reason in its interface descriptor. See the GitHub [Update a
repository API](https://docs.github.com/en/rest/repos/repos#update-a-repository).

Policy may omit `repository_content.social_preview` to request no action. To
request a custom image, use schema-v2
`{"repository_content":{"social_preview":{"action":"present","asset_path":"assets/social-preview.png"}}}`.
For removal, use `{"repository_content":{"social_preview":{"action":"absent"}}}`
and omit `asset_path`. The present asset must be a contained checkout-relative
regular non-symlink file under 1 MB with PNG, JPEG, or GIF magic bytes. Its
relative path, SHA-256, size, and format are bound into the plan and rechecked
before apply/verify. The GitHub API confirms only the custom-image Boolean.
When a fresh Boolean matches requested presence/absence and any selected
present-image file remains valid and hash-bound, that selected setting can
complete. A false or unavailable value keeps the Settings UI handoff as a
requested-policy finding. A true value does not establish that remote pixels
match the selected local image; report that limitation even when the setting
is complete.

## Required Local Gates

Dependency updates must refresh the shipped SDK dependency exports and receipt
with `ls/tools/generate_sdk_dependency_locks.py`, then pass its `--check` mode.
This includes lockfile-only updates: the runtime wheel rejects a receipt that
does not match the accepted manifest, resolution, and exported bytes. Validate
the affected consumers before release, including image tooling for development
Pillow updates. See [managed dependency lock exports](SDK_FORK.md#managed-dependency-lock-exports).

When refreshing full-commit GitHub Actions pins, update the corresponding exact-pin
assertions in `test_qc_patrol.py` alongside the workflows. The setup action version
and its selected uv version are separate inputs; changing the action pin does not
implicitly change the uv runtime used by validation and publication.

Run these before a maintainer release or broad automation change:

```bash
git status --short --branch
uv lock --check
uv sync --locked --all-groups
git fetch --tags origin
uv run --locked python ls/tools/localsetup.py --source-root . publish-preflight --base origin/main --head HEAD
# Review and commit a prepared_not_ready direct version-sync candidate, then create and validate its generated-doc receipt.
uv run --locked python ls/tools/localsetup.py --source-root . version-plan
./ls/tools/verify_context
./ls/tools/verify_rules
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/localsetup.py --source-root . validate-package-surface
uv run --locked python ls/tools/localsetup.py --source-root . scan-migration
uv run --locked python ls/tools/localsetup.py --source-root . audit-global-first
uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
uv run --locked python ls/skills/ls-localsetup-framework-audit/scripts/run_framework_audit.py --output /tmp/ls-framework-audit.md
uv run --locked pytest ls/tests -q
uv run --locked ./ls/tests/automated_test.sh
git diff --check
```

For daily maintenance and ordinary framework edits, run focused tests and matching LocalSetup validators first. Use the full Python suite above as final consolidation for broad automation changes, release or publish readiness, dependency changes, or explicit maintainer requests. Pytest reads the common `testing` policy automatically; inspect its resolved budget and hosted partitions with `localsetup test-workers --json`. [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md) owns the calculation and override precedence.

## Unit-Test Concurrency Policy

Native terminal tests must establish activity phases from captured acknowledgements
and quiet-state observations. Parallel scheduling can legitimately change which
producer most recently resumed output; launch order and short sleeps do not prove
that precondition. Keep exact geometry assertions after establishing the phase,
and include the observed layout in failure diagnostics.
The real controller-admission test reports only stable error codes when its
initial metadata opening fails, preserving authorization rejection without
including secrets, payloads, or GnuPG output.

Shipped test filenames follow the same public artifact boundary as runtime files.
Avoid reserved private-path markers in their names; use descriptive names such as
`test_sudo_credential_input.py` for synthetic credential tests. Keep the artifact
scanner's private-path rejection intact rather than adding test exceptions.

Synthetic runtime inventory tests request `synthetic_runtime_interpreter` from
`ls/tests/conftest.py` to supply owned, permission-controlled bytes without executing
them or changing the runner's Python. Installed-artifact qualification uses the
actual interpreter and production integrity checks. Adapter tests that assume
default OpenCode discovery request `default_opencode_environment`; override tests
set their inputs explicitly after that fixture. Keep production permission and
configuration rejection checks active on every runner.
Tool-free OmniRoute observation fixtures pass `--api-key-env ""` when they expect
anonymous requests, so local credentials cannot enter mocked transport assertions.
Bootstrap fixtures that replace `uv run python` must resolve it to the test
runner's `sys.executable`; an activated virtual environment or a `python` command
on PATH is not required by the test runner.
Copied bootstrap source fixtures include both `VERSION` and `pyproject.toml` so
version discovery uses that source. Bootstrap and hook subprocesses use the test
runner's interpreter directory on PATH; checker tests invoke `sys.executable`.
Heartbeat cron tests verify the bound interpreter, isolated Python flags, source
and target paths; an unrelated `localsetup` on PATH must not replace that command.
Tests creating temporary GPG homes register exact-home agent cleanup before the
first key operation, including when fixture setup or an assertion fails.
On a GPG fixture failure, the original nonzero exception retains one bounded
note with the operation phase, return code, and allowlisted machine status;
it excludes raw GPG output, key material, credentials, and temporary paths.
Headless tmux observer fixtures set `TERM=xterm-256color` for their synthetic
terminal children. They must not depend on an interactive caller's terminal type;
CI may leave `TERM` unset or set it to `dumb`. Failed attachment assertions include
bounded output from the fixture's child so terminal errors remain diagnosable.
Retain diagnostic bytes in the existing terminal reader; a second read after
the child exits can raise an I/O error and hide the attachment failure.
Native popup PTY fixtures also set an explicit window size and supported `TERM`
before starting their child. Wait for the rendered prompt before sending keys;
on early closure, report the child's exit status and bounded captured output,
then reap the child and close the fixture's PTY descriptors.
Native canvas follow-mode fixtures wait for the older operation's captured
readiness marker and advanced log cursor before launching active work. A later
first output is a reactivation event that may select the older pane.
Client-state process allocation fixtures use spawned workers with a bounded
startup barrier before the common five-second allocation observation; they
still exercise the real blocking file lock and reap their owned children.
Interpreter startup and state resolution are readiness work, especially under
one CPU, and do not consume the allocation observation window.

The default aggregate budget is `max(1, floor(available logical CPU cores / 3))`. This repository's common `testing` section owns the requested maximum: `"auto"` uses that default, and a positive integer may raise it up to the available logical CPU count. Physical memory can lower the effective maximum. Concurrent test processes share the resolved budget rather than each claiming it.

## Managed Adapter Refresh

Run this after changing shipped skills, workflow packages, platform adapters, or repo agent context when this machine should immediately use the current checkout:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . self-refresh --dependency-mode prompt-only
```

The command installs every configured pack from this checkout into the managed LocalSetup library. Adapter refresh uses validated recorded clients, paths, scopes, modes, and package exposure; new catalog clients sharing a directory do not become owners. A legacy shared path without recorded ownership requires explicit `--platforms` or ownership reconciliation before refresh. Explicit legacy selection uses the normal preservation and native prerequisite checks.

For validated modern receipts, pack and global package overrides change the shared library selection while target exposure remains recorded. Changing recorded clients, target package selectors, scope, or adapter mode requires an explicit install or the owning migration command; self-refresh refuses these combinations before dependency work. It is maintenance tooling for local machine state, not a release or publish step.

Without an explicit platform override, a fresh target with no recorded installation or adapter surface receives a library-only refresh; self-refresh does not select clients for it.

## Maintainer Codex Adapter Reconciliation

This source checkout may expose every public LocalSetup skill and workflow package through its repo-local `.agents/skills` adapter while keeping the global/default skill stance curated. Use this only for the LocalSetup maintainer repo, not as a normal consumer-repo default.

Dry-run first:

```bash
UV_CACHE_DIR=/tmp/localsetup-uv-cache uv run --locked python ls/tools/localsetup.py --source-root . install --target-directory . --platforms codex --global-packs bootstrap core dev frontend architecture ops publishing omniroute --global-skills ls-firecrawl-scrape-search ls-cloudflare-dns-records --global-exclude-skills ls-superpowers-skill-mapping --repo-preset all --mode symlink --json
```

Apply the same plan only after the dry-run has no warnings; the apply-time preflight remains authoritative and will still stop before mutation on same-name adapter collisions:

```bash
UV_CACHE_DIR=/tmp/localsetup-uv-cache uv run --locked python ls/tools/localsetup.py --source-root . install --target-directory . --platforms codex --global-packs bootstrap core dev frontend architecture ops publishing omniroute --global-skills ls-firecrawl-scrape-search ls-cloudflare-dns-records --global-exclude-skills ls-superpowers-skill-mapping --repo-preset all --mode symlink --apply --json
```

Same-name selected package collisions still block before mutation. If a repo-local adapter entry intentionally shadows a selected LocalSetup package, resolve that one entry deliberately before rerunning the native installer; do not clear the adapter directory or broaden the global Codex adapter to make the apply pass.

Verify the reconciled shape:

```bash
UV_CACHE_DIR=/tmp/localsetup-uv-cache uv run --locked python ls/tools/localsetup.py --source-root . verify --target-directory . --platforms codex --level filesystem --json
UV_CACHE_DIR=/tmp/localsetup-uv-cache uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
```

## GitHub Actions

- `pr-validation` is the required PR and merge-queue validation workflow.
- `generated docs and version sync` checks canonical version arithmetic before merge. `docs-sync` is called once as the prerequisite `documentation / generated docs drift` and owns generated-document drift, release prose, and documentation alignment; the version job does not regenerate the same files again.
- `framework validation py3.12` is the aggregate result of the 12 configured isolated Python 3.12 shards, matching the supported Python floor. The prerequisite job asks `test-workers` for a hosted override equal to the runner's available logical CPU count; the resolved `max_parallel` limit admits that many shard jobs, up to the shard count. Each isolated job also requests a worker per available logical CPU on its own runner and passes that request into the resource guard, so its CPU quota matches the worker pool unless an enclosing CPU or memory limit is tighter. Local test runs retain their configured worker policy. Inexpensive version, audit, smoke, deterministic QC, catalog, branding, and architecture checks precede the shards. Each shard has a 60-minute deadline and stops on its first failure; all shards must pass.
  The shards use `ubuntu-24.04` and verify tmux 3.4 before running tests, matching
  the native family layout and retained-history qualification. A runner-label
  migration must not silently change that contract. Qualify a newer tmux version
  before moving these tests to an image that supplies it; preserve the runtime's
  rejection of unqualified versions.
- `shell smoke and framework audit` runs the shell wrapper, framework audit, and whitespace diff check.
- Native family runners mark their workload descendants collected, then wait for
  the existing output-drain and finalize receipts before exiting. The capture
  collector writes its drain receipt first and can hand the exact completed,
  collected, dead pane to the shared finalizer. The pane-died hook uses that same
  finalizer if it arrives later. Both routes verify native identity, serialize
  cleanup, and keep uncertain removals readback-only. Missing receipts or identity
  leave cleanup pending. The collector's waiting and reconciliation share one
  two-second retry deadline; each hook invocation retains its existing retry
  budget. Individual native calls may exceed that budget under the existing native
  timeout. Final telemetry publication follows a settled cleanup decision under
  the session lock, keeping this final read out of runner exit; reconciliation may
  still publish telemetry during other lifecycle changes. The native family test
  keeps its passive eight-second cleanup assertion.
- `publish` is explicitly dispatched on `main` after source checks and a verified signed tag. It prepares a validated release draft; complete its artifact inventory and notes before publication, following [VERSIONING.md](VERSIONING.md#github-release-workflow). It should not be a maintainer's first signal that version sync is missing.
- `triage` labels issues and PRs from metadata only. It must not check out or run untrusted pull request code. Its handoff summary uses fixed text, event kind, and item number to link maintainers to the [Codex GitHub Issue Goal Loop](CODEX_GITHUB_ISSUE_GOAL_LOOP.md); it does not include issue or PR title/body text.
- `triage` also bootstraps the maintainer label set used by issue forms and Dependabot. Run it manually once with `workflow_dispatch` before enabling Dependabot on a fresh repository.
- `dependency review` runs only on pull requests and merge groups, before framework prerequisites. It blocks high/critical dependency vulnerabilities across runtime, development, and unknown scopes; it does not impose a license policy or post comments. The required framework aggregate rejects failed, cancelled, missing, or unexpectedly skipped dependency results even when full-suite evidence is reused. Push/manual validation requires this conditional job to be skipped; do not require the conditional job universally in branch rules.
- `triage` creates only missing catalog labels and preserves maintainer colors/descriptions, existing status/type categories, and other labels. Read failures stop mutation; repeated intake does not reset accepted, blocked, or ready work.

### Validation reuse

PR jobs check out the candidate head explicitly. `ls/tools/ci_evidence.py` checks
successful Actions runs for the exact repository, commit, workflow, and required
jobs. When that same commit reaches `main`, the full suite reuses its completed
PR result. Missing evidence runs the suite normally. Publication requires the
completed validation, docs, and deterministic QC evidence and fails promptly if
it is unavailable; it does not launch another full suite. Manual workflow reruns
are available when the environment or evidence needs refreshing.

Use focused local checks before pushing, then let CI supply the authoritative
full-suite result. Avoid running a second local full suite for the same release.
Preserve successful jobs when retrying an understood failure. The final release
still checks the signed tag and commit, version, release prose, and built artifact.

## Maintenance Branch Ruleset For The Default Branch

The opt-in [GitHub maintenance recipe](../workflows/ls-wf-github-repository-settings-audit/references/github-maintenance.md)
defines a proposed profile, not a claim that remote settings are active. Pilot
and verify the candidate before enabling enforcement. Create/update only the
separately named `LocalSetup maintenance` branch ruleset through `github-repo`:

- Active enforcement, include `~DEFAULT_BRANCH`, empty exclude list, no bypass actors.
- Require GitHub-recognized signed commits; block deletion and force pushes.
- Require these observed GitHub Actions contexts, each with integration ID `15368`:
  - `framework validation py3.12`
  - `shell smoke and framework audit`
  - `generated docs and version sync`
  - `documentation / generated docs drift`
  - `quality / deterministic qc`
- Set `strict_required_status_checks: false` to allow reuse of the exact tested candidate.

Do not add a PR-only restriction, review count, code-owner requirement,
conversation requirement, merge queue, or automatic merge as part of this
profile. Preserve stronger existing or inherited controls and unrelated
`Default`/release-tag rulesets. For another repository, discover its own successful
contexts and application IDs rather than copying this pilot's bindings.

GitHub's [required-status-check rule](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset)
protects ref updates: first push the signed candidate to another ref for its PR
or manual validation, verify the exact SHA's required successful checks, and
separately verify its primary OpenPGP fingerprint against the repository's
authorized signing identity. GitHub's recognized-signature check does not
establish that local signer identity. This is not the separate push-ruleset feature.

## Merge Policy

Under the maintenance profile, finish and sign the candidate, validate and verify
that exact SHA, then fast-forward the protected target to the same commit. A
merge, squash, or rebase creates a different candidate: sign, validate, and verify
that commit before updating the target. Do not substitute a successful run from
the pre-integration SHA. Keep Conventional Commit classification and generated
provenance intact. For a release PR containing version-sync/generated-document
receipts, preserve its source ancestry; never squash or rebase those receipts.
No branch deletion or merge-method setting change is part of this profile.

Opaque provenance root IDs case-fold the GitHub host, owner, and repository in the
hash seed. Displayed origin URLs retain their configured spelling; other hosts keep
their case-sensitive identity.

### Local hooks and hosted controls

Preserve `.githooks/pre-commit`, `.githooks/pre-push`, and `ls/core/hooks.py`.
Pre-commit can generate and stage files; pre-push can create a version-sync
commit. They are not read-only remote validators and must not run in Copilot
sessions. Their hosted counterparts are generated docs/version checks,
documentation drift, package-boundary deterministic QC, framework audit, and
exact-commit validation. Do not duplicate the hooks in another workflow or add
AI calls, network review, or full suites to per-tool/commit hooks.

Finish source/version preparation before final hosted validation. Any generated
source/version change creates a new candidate SHA and invalidates earlier
candidate evidence. Prepare the typed settings plan only after the local source
checkpoint is stable; review its digest and exact operation IDs, obtain/reuse
authority for that payload, apply selected operations, and read them back. Lost
responses require read-only reconciliation, never blind replay.

## Security Settings

Enable these in GitHub security settings where available:

- Dependabot security updates.
- Secret scanning.
- Push protection.
- CodeQL default setup for code scanning.
- Private vulnerability reporting.

Security-sensitive reports should follow [`../../SECURITY.md`](../../SECURITY.md).

Use CodeQL **default setup**, detected normalized language selections, the default
query suite, and standard GitHub-hosted runners when no scanner exists. Preserve
an existing advanced setup. Configure through the explicit Settings handoff:
`github-repo` does not model CodeQL writes. Confirm both fresh configuration and
a completed successful analysis before claiming scanning; configured alone is
insufficient. Keep Autofix suggestions available for repository-agent review,
without assigning alerts to a cloud coding agent or automatically dismissing them.

## Dependabot

Tracked configuration lives at [`../../.github/dependabot.yml`](../../.github/dependabot.yml). It covers:

- GitHub Actions updates.
- Python dependency updates for the root uv project (`pyproject.toml` and `uv.lock`).

Keep Dependabot PRs small, grouped, and scheduled. Treat dependency updates that affect install, transport, security, or packaging as release-relevant. Pull requests from Dependabot run an extra uv lock/sync validation job, while the standard validation jobs continue to use frozen uv sync/run commands.

Workflow files own the reviewed action revisions. Regression checks require full
immutable hashes, consistent revisions across workflows and bounded cache cleanup;
they do not duplicate changing release hashes as test constants. Review new pins
against upstream identity and dependency advisories before accepting an update.

## Labels

Keep labels aligned with the triage workflow and issue forms:

- `status/needs-triage`, `status/blocked`, `status/accepted`, `status/ready`
- `type/bug`, `type/feature`, `type/maintenance`, `type/pr`, `type/support`
- `area/docs`, `area/installer`, `area/skills`, `area/release`, `area/automation`, `area/python`
- `priority/high`, `priority/normal`, `priority/low`
- `good first issue`, `help wanted`, `security`, `release`, `docs`, `needs-repro`, `dependencies`

The triage workflow bootstraps these labels. Run the workflow manually once before enabling Dependabot on a fresh repository, then review colors, descriptions, and taxonomy periodically.

## Issues, Discussions, And Projects

- Keep Issues enabled for actionable bugs, features, and maintenance reports.
- Keep Discussions enabled for usage questions and design conversation.
- Use Projects automation to move new issues and PRs into triage and closed or merged items to done.
- Keep blank issues disabled so reports use structured fields.

## Release Policy

`VERSION` is the source of truth. Use [`VERSIONING.md`](VERSIONING.md) for the policy and command flow. Fetch tags before diagnosing a version or release mismatch, because stale local tags can make an already-published GitHub release look missing. For a pending release batch, `version-plan` is expected to report `ok: false` until the version-sync commit exists.

From a clean worktree, prepare the local direct version-sync candidate with:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . publish-preflight --base origin/main --head HEAD
```

When it returns `prepared_not_ready`, inspect and commit that unstaged direct
candidate, then create and validate the post-version generated-document receipt.
Use `--fix` only when the tool should prepare and commit both release slices.

Then push through:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . release-push
```

Do not publish a release from a dirty worktree. If a tag already exists at a different commit, stop and resolve the remote release state before retrying.

## Optional PR model review

PR validation runs deterministic QC without a provider by default. To authorize
the optional model review, a repository owner sets the GitHub Actions repository
variable `QC_PR_LLM_ENABLED` to `true` (case-insensitive, following GitHub
expression semantics) and configures the
`QC_LLM_BASE_URL` and `QC_LLM_API_KEY` secrets. Existing secrets alone do not
enable requests. This opt-in can incur provider charges and disclose the review
input to that provider; apply the repository's disclosure authority first.

Only same-repository pull requests using trusted base tooling receive the
credentials when enabled. Forks, manual workflow runs and the subject-tooling
fallback remain provider-free. Removing the variable or setting it to a value
other than case-insensitive `true` disables model review on subsequent runs;
it does not cancel an active request.
Deterministic QC and the other required validation gates remain enabled.

### Native advisory Copilot profile

The optional maintenance profile uses native Copilot review, not the separate QC
provider. Keep `QC_PR_LLM_ENABLED=false` and retain deterministic review artifacts
and fork/no-secret routing. The substantive review owner remains `REVIEW.md`;
`.github/copilot-instructions.md` is a short advisory adapter.

Before activation, verify the actual subscription, credit/request billing mode,
remaining allowance, payer for external/bot reviews, additional-usage controls,
and inherited review rules. Authorize no additional paid usage or organization
billing migration; preserve account-wide settings and budgets. If allowance or
effective controls cannot be verified, keep automatic review inactive and request
individual reviews only when entitlement is confirmed. Native review consumes
allowance and Actions minutes and is not promised free.

Use repository Settings -> Copilot -> Code review: Lite effort; both approval
toggles off. A deliberate Balanced review is only for material security,
installer, or release-boundary risk, not a second routine pass. GitHub chooses
the native review model; there is no user-selectable review model.

In Settings -> Rulesets, use a separate `LocalSetup Copilot review` ruleset for
default-branch PRs, with automatic review enabled, new-push and draft reviews
off. Do not put this unmodeled rule in the typed maintenance ruleset. Inspect
independent user/organization/enterprise rules: a repository toggle cannot
cancel another source's draft/push review. Report conflicts rather than claiming
the economical profile is enforced.

Keep UI evidence in the existing private task ledger, alongside typed read-back
results; the profile is not activated until both are verified. Do not install
review-request Actions, MCP integrations, secrets, CLI hooks, or a cloud coding
agent. Do not invoke "Fix with Copilot." For an explicitly requested sanitized
Chat summary only, prefer GPT-6 Luna with low reasoning if verified in the actual
account picker; otherwise return the work to existing repository agents.

See the recipe's [official source links](../workflows/ls-wf-github-repository-settings-audit/references/github-maintenance.md#current-github-documentation-to-recheck)
and recheck current UI/billing behavior before activation.

## Hosted factual freshness patrol

Use the existing weekly `qc-patrol` GitHub-hosted workflow, not a workstation
scheduler or a second cloud agent. Inventory tracked skills, workflow packages,
their references/assets, and GitHub workflows against the last successful
baseline, including a previous run at the same SHA. File changes are evidence
packets, not proof that provenance is stale. Canonical catalog/documentation
validators own expected-versus-observed provenance checks; they do not guarantee
that every upstream source or external claim is current.

Confirmed outdated provenance uses the existing duplicate-aware issue owner:
affected path, check name, expected evidence, and observed evidence only. Do not
post model recommendations, implementation plans, sensitive findings, private
paths, or repeated comments for unchanged evidence. Preserve issue-only reporting;
repository agents perform any accepted repair through normal signed review.

The separate `provenance-refresh` workflow checks the one canonical Herdr
manifest at 06:00 `America/Chicago`, with manual dispatch retained. It reads
official release metadata and the manifest's bounded watched files; a newer
stable release is actionable only after its tag resolves to a full immutable
commit SHA. Changed watched-file hashes can be reported on their own, including
when another observation is unknown. Missing pins, malformed evidence, and
network failures do not create speculative issues. Hosted runners have no Herdr
binary or server, so those local and live-host checks remain unknown by design.
Its entry point, `tools/provenance_refresh.py`, is repository automation;
the entry point and scheduled workflow are excluded from the framework payload.

Confirmed drift creates a compact `provenance-refresh` issue with pinned and
observed evidence, immutable upstream references, local owner paths, and a
review prompt that grants no host-upgrade authority. Matching open evidence is
updated only when watched-file hashes or the stable release tag materially change; the
observed branch commit and run URL alone do not trigger repeat comments. An
identical closed issue suppresses a duplicate, while materially new evidence can
open a fresh issue. Missing observations alone do not count as new evidence;
deduplication retains the last confirmed release and watched-file observations
when a later check is partial. A confirmed matching release or file retires a
previous drift claim when other drift still warrants a handoff. Newly confirmed
matching paths with no prior drift stay quiet, as do unknown observations.
The job has `contents: read` and `issues: write` only
and does not change source files. QC Patrol remains unchanged and owns its
existing weekly factual freshness checks.

Model-assisted patrol uses the existing QC provider, separately from native
Copilot review. Bind an explicitly verified economical patrol model with low
reasoning where supported; do not inherit an opaque premium review route.
Limit analysis to new bounded factual evidence, cap calls and input/output
tokens, and prohibit retries or automatic escalation. Missing model, runtime,
allowance, or disclosure evidence keeps that activation pending; deterministic
checks still run. Keep `QC_PR_LLM_ENABLED=false` for the pilot. No new paid usage,
subscription changes, provider secret, or cloud coding assignment is authorized
by selecting the pack.
