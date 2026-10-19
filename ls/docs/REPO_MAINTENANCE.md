---
status: ACTIVE
version: 4.53
owner_skill: ls-framework-compliance
---

# Repository Maintenance

This checklist records the GitHub settings and repo-maintenance conventions that are not fully represented by tracked files. Keep tracked automation in `.github/`; apply remote settings in GitHub after validating the workflow names emitted by Actions.

## GitHub Repository Enhancement

For a GitHub-hosted repository, use the CLI-first
[GitHub repository enhancement workflow](../workflows/ls-workflow-github-repository-enhancement/SKILL.md)
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
uv run --locked python ls/skills/ls-framework-audit/scripts/run_framework_audit.py --output /tmp/ls-framework-audit.md
workers="$(uv run --locked python ls/tools/localsetup.py --source-root . test-workers)"
uv run --locked pytest -n "$workers" ls/tests -q
uv run --locked ./ls/tests/automated_test.sh
git diff --check
```

For daily maintenance and ordinary framework edits, run focused tests and matching LocalSetup validators first. Use the full Python suite above as final consolidation for broad automation changes, release or publish readiness, dependency changes, or explicit maintainer requests. Resolve the permitted worker count with `localsetup test-workers`; [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md) owns its formula and aggregate-budget rule.

## Unit-Test Concurrency Policy

Synthetic runtime inventory tests request `synthetic_runtime_interpreter` from
`ls/tests/conftest.py` to supply owned, permission-controlled bytes without executing
them or changing the runner's Python. Installed-artifact qualification uses the
actual interpreter and production integrity checks. Adapter tests that assume
default OpenCode discovery request `default_opencode_environment`; override tests
set their inputs explicitly after that fixture. Keep production permission and
configuration rejection checks active on every runner.
Tool-free OmniRoute observation fixtures pass `--api-key-env ""` when they expect
anonymous requests, so local credentials cannot enter mocked transport assertions.

Unless a repository explicitly defines a stricter policy, every unit-test runner—regardless of language or framework—uses one aggregate budget: `max(1, floor(available CPU cores / 3))`. Round down before applying the minimum of one worker. Concurrent test processes share that budget rather than each claiming the full allowance.

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
UV_CACHE_DIR=/tmp/localsetup-uv-cache uv run --locked python ls/tools/localsetup.py --source-root . install --target-directory . --platforms codex --global-packs bootstrap core dev frontend architecture ops publishing omniroute --global-skills ls-firecrawl ls-cloudflare-dns --global-exclude-skills ls-superpowers --repo-preset all --mode symlink --json
```

Apply the same plan only after the dry-run has no warnings; the apply-time preflight remains authoritative and will still stop before mutation on same-name adapter collisions:

```bash
UV_CACHE_DIR=/tmp/localsetup-uv-cache uv run --locked python ls/tools/localsetup.py --source-root . install --target-directory . --platforms codex --global-packs bootstrap core dev frontend architecture ops publishing omniroute --global-skills ls-firecrawl ls-cloudflare-dns --global-exclude-skills ls-superpowers --repo-preset all --mode symlink --apply --json
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
- `framework validation py3.12` is the aggregate result of eight isolated Python 3.12 shards, matching the supported Python floor. Inexpensive version, audit, smoke, deterministic QC, catalog, branding, and architecture checks precede the shards. Each shard has a 60-minute deadline and stops on its first failure; all shards must pass.
- `shell smoke and framework audit` runs the shell wrapper, framework audit, and whitespace diff check.
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

The opt-in [GitHub maintenance recipe](../workflows/ls-workflow-github-repository-enhancement/references/github-maintenance.md)
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

See the recipe's [official source links](../workflows/ls-workflow-github-repository-enhancement/references/github-maintenance.md#current-github-documentation-to-recheck)
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

Confirmed drift creates a compact `provenance-refresh` issue with pinned and
observed evidence, immutable upstream references, local owner paths, and a
review prompt that grants no host-upgrade authority. Matching open evidence is
updated only when watched-file hashes or the stable release tag materially change; the
observed branch commit and run URL alone do not trigger repeat comments. An
identical closed issue suppresses a duplicate, while materially new evidence can
open a fresh issue. The job has `contents: read` and `issues: write` only and
does not change source files. QC Patrol remains unchanged and owns its existing
weekly factual freshness checks.

Model-assisted patrol uses the existing QC provider, separately from native
Copilot review. Bind an explicitly verified economical patrol model with low
reasoning where supported; do not inherit an opaque premium review route.
Limit analysis to new bounded factual evidence, cap calls and input/output
tokens, and prohibit retries or automatic escalation. Missing model, runtime,
allowance, or disclosure evidence keeps that activation pending; deterministic
checks still run. Keep `QC_PR_LLM_ENABLED=false` for the pilot. No new paid usage,
subscription changes, provider secret, or cloud coding assignment is authorized
by selecting the pack.
