---
status: ACTIVE
version: 4.45
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
- `triage` labels issues and PRs from metadata only. It must not check out or run untrusted pull request code.
- `triage` also bootstraps the maintainer label set used by issue forms and Dependabot. Run it manually once with `workflow_dispatch` before enabling Dependabot on a fresh repository.

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

## Recommended Branch Ruleset For `main`

Configure the active `main` ruleset to:

- Require pull requests before merge.
- Require approving reviews only when the repository owner selects that policy; do not invent a maintainer-approval gate when live rules do not require it.
- Require resolved conversations.
- Require status checks to pass before merge.
- Require the `pr-validation` jobs listed above.
- Block deletions and non-fast-forward updates.
- Keep force pushes disabled.
- Enable merge queue only after `merge_group` checks are green on this repository.

## Merge Policy

Prefer squash merges for ordinary PRs so each merged change has one Conventional Commit subject that the release tooling can classify. Use a merge commit for release PRs that contain a version-sync/generated-doc commit; provenance regeneration follows the merged PR's second parent to preserve the source commit recorded in generated artifacts. Never squash or rebase those release PRs. Delete branches on merge when safe.

## Security Settings

Enable these in GitHub security settings where available:

- Dependabot security updates.
- Secret scanning.
- Push protection.
- CodeQL default setup for code scanning.
- Private vulnerability reporting.

Security-sensitive reports should follow [`../../SECURITY.md`](../../SECURITY.md).

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
