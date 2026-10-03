---
status: ACTIVE
version: 4.45
owner_skill: ls-framework-compliance
---

# Command Reference

Use this page when you need copy-pasteable LocalSetup commands. For narrative install guidance, start with [Quickstart](QUICKSTART.md) or [Multi-platform install](MULTI_PLATFORM_INSTALL.md).

## Bootstrap Installer

The public Bash wrapper is the entry point for Linux, macOS, and WSL2. Windows support is WSL2-only.

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s --
```

Run the same wrapper in automation mode:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --non-interactive --yes
```

Install from a local checkout:

```bash
./install --directory .
```

Attach selected platform adapters to the target repo:

```bash
./install --directory . --tools codex,kilo
./install --directory /path/to/localsetup --target-directory /path/to/project --tools cursor
```

`--tools` is the compatibility alias for `--platforms`. If both are omitted on a source-only install, LocalSetup refreshes the managed package library only and does not create repo adapter paths. If a repo target is explicit, selector-free `plan`, `install --apply`, and `update` use auto mode:

```bash
localsetup plan --target-directory .
localsetup install --target-directory . --apply
localsetup update --target-directory .
```

Auto mode infers existing LocalSetup state, applies only unambiguous safe repairs, or installs the `normal` global baseline for a brand-new repo without adapter paths.

## Installer Options

| Option | Meaning |
|---|---|
| `--directory PATH` | LocalSetup source checkout containing `ls/`. Without an explicit checkout, the raw bootstrap creates or refreshes `~/.local/share/localsetup/source`. |
| `--target-directory PATH` | Repo or directory where selected adapter paths and `.localsetup/lock.json` are written; without selector flags on `plan`, `install --apply`, and `update`, enables auto mode. |
| `--home PATH` | Home directory for the managed source and package library. Defaults to `$HOME`. |
| `--yes` | Accepted legacy flag. For automation, combine with `--non-interactive`. |
| `--non-interactive` | Automation mode. Requires `--yes` and preserves machine-readable output. |
| `--tools LIST` | Comma-separated platform ids. Alias for `--platforms`. |
| `--platforms LIST` | Platform adapter ids. Explicit values override auto mode; errors list registered selector ids. |
| `--codex-agent-conflict POLICY` | For `plan`, `install`, or `update`, `error` (default) refuses changed Codex agent files; `preserve` skips differing readable regular files for this operation and reports them separately. |
| `--preset NAME` | Selection preset: `core`, `normal`, `suggested`, `all`, or `custom`. |
| `--packs LIST` | Comma-separated skill and workflow packs. |
| `--skills LIST` | Comma-separated individual skill ids. |
| `--workflows LIST` | Comma-separated workflow package ids or workflow aliases. |
| `--skill-classes LIST` | Comma-separated taxonomy classes. |
| `--skill-tags LIST` | Comma-separated taxonomy tags. |
| `--exclude-skills LIST` | Skills to remove unless required by a selected workflow. |
| `--global-*` | Selection options for the managed package-library baseline, including `--global-workflows`. |
| `--repo-*` | Selection options for packages exposed through repo adapter paths, including `--repo-workflows`. |
| `--mode symlink\|portable` | Adapter write mode. Symlink is the default; portable copies selected packages. |
| `--sync-env` | Sync the uv-managed source checkout environment from `pyproject.toml` and `uv.lock`. |
| `--install-deps` | Deprecated alias for `--sync-env`. |
| `--install-uv` / `--no-install-uv` | Allow or forbid uv bootstrap when sync is requested. |
| `--offline` | Run uv sync in offline/cache-only mode. |
| `--no-register-shell` | Skip `~/.local/bin/localsetup` registration. |
| `--color auto\|always\|never` | Wizard color mode. |
| `--no-color` | Alias for `--color never`. |
| `--glyphs auto\|ascii\|unicode` | Wizard glyph mode. |
| `--help` / `-h` | Print installer help. |

## Managed CLI

After registration, the managed shim records the framework source and `localsetup` targets the nearest Git worktree root from the current directory unless `--target-directory` is supplied. For an installed wheel, source selection keeps this precedence: explicit `--source-root`, explicit `--repo`, source from the enabled managed shim, then a valid source recorded by the selected home's managed shim. The selected home follows existing CLI/config precedence: explicit `--home`, configured `home` when not overridden, then the enabled shim's `LOCALSETUP_HOME` or the user default. The recorded source is read without executing the shim and must contain `ls/tools/localsetup.py` and a parseable `pyproject.toml` naming the `localsetup` project. This checks the expected source layout; it is not a trust boundary against another process running as the same user.

Wheel `doctor` and `doctor repair` require an explicit or registered source. If none is usable, they exit with an actionable `--source-root` message before writing health state or attempting repair. Other wheel commands keep the installed package-resource fallback. Source-checkout invocations keep the checkout as their default source.

```bash
localsetup install --tools codex --yes
localsetup verify --tools codex --level filesystem
localsetup adapters check --tools codex
localsetup doctor
localsetup diff --tools codex
localsetup rollback
```

From this source checkout, run the CLI through uv:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
uv run --locked python ls/tools/localsetup.py --source-root . docs-align check --ci
```

### Client state

Resolve the registry-owned repo/global state root without writing:

```bash
localsetup state path --client codex/codex-cli
```

Add only that exact repo state child to Git's resolved local exclude:

```bash
localsetup state path --client codex/codex-cli --apply-exclude
```

Allocate and then verify a no-overwrite restart artifact:

```bash
localsetup state allocate --client codex/codex-cli \
  --agent controller --purpose release-checkpoint --extension md \
  --kind restart-artifact --schema restart-v1 --producer controller \
  --content-file checkpoint.md
localsetup state verify --client codex/codex-cli --artifact <artifact-filename>
```

Use `--scope repo|global` only to require a specific supported scope; explicit global scope does not inspect the caller's working directory, while the default `auto` uses the containing Git worktree or the verified global root. Outputs contain relative state/artifact identifiers rather than absolute machine paths. Verification mismatches exit `1`; invalid requests and operational failures exit `2` with a sorted, sanitized JSON error envelope. See [Deterministic client state](CLIENT_STATE.md) for metadata, collision, stale-binding, and unsupported-state behavior.

## CLI Commands

The top-level CLI currently exposes these commands:

```text
plan, install, verify, rollback, update, adapters, configure, doctor, state,
migrate, context, convert, catalog, diff, skill, workflow, why, graph,
candidate-skill, adopt, detach, sbom, scan-migration, audit-global-first,
validate-catalog, generate-docs, provenance, harness, docs-align, context-index, hook-gate,
github-repo,
envman, version-plan, version-sync, release-docs, release-push, self-refresh, install-hooks,
register-shell, wizard, package, verify-release, agent, llm
```

`candidate-skill validate --candidate <path> --json` and `candidate-skill proposal --candidate <path> --output -` inspect repo-scoped candidate skills without promoting them into managed packages or adapter directories.

`wizard --repo-profile universal-agent-repo --target-directory <path> --dry-run --report <path>` plans the lean universal agent repository shape without entering the interactive installer. Re-run with `--apply` to create the missing shape files. Existing files with different content are blockers; LocalSetup does not overwrite them.

Most commands emit JSON by default. Commands with explicit human-readable modes, such as `context --markdown`, document that mode in their own help.

### Envman toolchain

Envman is an optional external toolchain. LocalSetup does not install or update it during ordinary commands. Status is read-only and redacted; the doctor probe is opt-in:

```bash
localsetup envman status
localsetup doctor --envman
localsetup envman update --check
```

Only the explicit install and mutating update commands invoke Envman's latest bootstrap:

```bash
localsetup envman install
localsetup envman update
```

The bootstrap uses uv with CPython 3.12 and supports Linux x86_64 with uv 0.11 or newer. To request Envman's native skill projection, supply exactly one current LocalSetup platform id and one scope:

```bash
localsetup envman install --install-skill --skill-scope global --skill-target codex
localsetup envman update --install-skill --skill-scope repository --skill-target codex --target-directory /path/to/repository
```

Repository scope requires the explicit Git root as `--target-directory`; global scope does not accept that option. LocalSetup does not invoke `envman init`. A failed or timed-out mutation has an unknown result and is not automatically retried. See [the integration contract](ENVMAN_INTEGRATION_CONTRACT.md) for the exact redaction, validation, and rollback boundaries.

### GitHub repository enhancement

Use the CLI-first [GitHub repository enhancement workflow](../workflows/ls-workflow-github-repository-enhancement/SKILL.md)
to inspect a GitHub-hosted repository's settings. Its target syntax is:

```text
localsetup github-repo --repository OWNER/REPO --hostname HOST --checkout PATH --mode MODE
```

`MODE` is exactly one of `audit`, `plan`, `apply`, or `verify`. Both target
selectors are explicit: `--repository OWNER/REPO` and `--hostname HOST` identify
the remote, while the global `--repo` retains its existing meaning as the
LocalSetup source-checkout selector. `--checkout PATH` selects the local Git
checkout used for repository evidence and defaults to `.`. Use the same
checkout for audit/plan and apply/verify; it does not change the global
`--repo` meaning.

```bash
localsetup github-repo --repository OWNER/REPO --hostname github.com --checkout . --mode audit
localsetup github-repo --repository OWNER/REPO --hostname github.com --checkout . --mode plan \
  --policy POLICY.json
localsetup github-repo --repository OWNER/REPO --hostname github.com --checkout . --mode apply \
  --plan PLAN.json --authorize-plan DIGEST \
  --operation OP_ID --operation ANOTHER_OP_ID
localsetup github-repo --repository OWNER/REPO --hostname github.com --checkout . --mode verify \
  --plan PLAN.json \
  --trusted-public-key KEYS/maintainer.asc \
  --trusted-public-key KEYS/release.asc
```

Plan mode requires the explicit desired-state `POLICY.json` and produces a
`plan.json` file, a human-readable `plan.md`, and a JSON summary on stdout with
both paths, the SHA-256 digest, and operation IDs. By default, the plan files
are written under LocalSetup's per-user private state root at
`github-repository-operations/<target-key>/plans/<plan-digest>/`; the target
key binds normalized hostname and immutable repository ID. The default state
root must be owned by the current user with mode `0700`. Its path components
must not be symlinks; ancestors must be root- or user-owned and not
group/world writable, except root-owned sticky directories such as `/tmp`. An
unsafe state root fails closed for default plan output, and apply always needs
that secure root for its target journal. An optional `--output-directory DIR`
selects another directory, which must be user-owned with mode `0700` and is
created with that mode if missing. Its path components cannot be symlinks and
its ancestors follow the same ownership and sticky-directory rule. Plan files
are created exclusively at mode `0600` without following symlinks; existing
files must be user-owned mode-`0600`, single-link regular files. Use the reported
`plan.json` path for apply and verify; verify mode requires `--plan PLAN.json`.
Saved plans use schema v4; regenerate earlier schema-v2 or schema-v3 saved
plans from their reviewed policy before applying or verifying. The repository
policy schema remains v2.

Review the exact saved plan JSON, its reported SHA-256 digest, and every
operation ID before apply. `--authorize-plan DIGEST` plus the repeated
`--operation` options authorize only those IDs from that exact plan. Do not use
a wildcard or apply the unreviewed remainder. The plan also binds normalized
host, immutable GitHub repository ID, and authenticated actor; a changed
identity requires a new audit and plan. Destructive or access-changing changes
are excluded from the ordinary plan and need a separate reviewed plan and
exact authorization. Controls without supported typed operations remain
report-only. On `github.com`, public/private visibility must be the sole
setting in its own plan; split or reject a policy that mixes it with other
changes before apply. Visibility on other hosts remains report-only while
Enterprise Server support is unverified. The documented CLI and REST handling
of `internal` is unresolved.

Each typed operation in plan JSON and Markdown displays a canonical interface
descriptor: transport, the fixed command or HTTP method and endpoint template,
the `plan.target` binding, required command flags, and the reason for selecting
an API interface. The descriptor is part of the operation identity and plan
digest. LocalSetup prefers native `gh repo edit` only when its available
command and flags express the complete operation. Before dispatch it checks
bounded `gh repo edit --help` output for every required flag; a missing command,
flag, or unsupported feature fails closed. It does not silently fall back to
`gh api`, switch transports after failure, or replay a failed command. The
plan-selected REST path uses `gh api` only when no native command exactly
expresses that operation, and records why. Ruleset mutations use REST through
`gh api` because `gh ruleset` documents list/check/view operations, not writes.

#### Collaboration web commit signoff

Policy schema v2 supports the desired Boolean
`repository.web_commit_signoff_required` for the registered
`collaboration.web_commit_signoff` control. For example, set
`{"schema_version":2,"repository":{"web_commit_signoff_required":true}}`
to require contributors to sign off on commits made through GitHub's web
interface; set the field to `false` to remove that requirement. The official
[`gh repo edit` options](https://cli.github.com/manual/gh_repo_edit) do not
document an exact native flag for this setting. The typed operation uses
`PATCH /repos/{owner}/{repo}` through `gh api`, supplies
`web_commit_signoff_required` in the request body, and records this REST
selection reason in its interface descriptor. See GitHub's [Update a
repository API](https://docs.github.com/en/rest/repos/repos#update-a-repository).

All remote write operations are enabled only for `github.com` while the
GitHub Enterprise Server API-version matrix remains unverified. Audits and
reads may run on other GitHub hosts, but requested drift there remains
incomplete/report-only with a specific compatibility reason.

Audit returns the seven group objects plus one observation for every
control in the fixed registry. That registry defines exact coverage for this
workflow, not an exhaustive audit of every possible GitHub control. The
`audit_coverage` receipt includes status, expected/observed counts, and lists
of missing, duplicate, invalid, or incompletely observed control IDs. A
reason-backed unknown, unavailable, inherited, local, UI-only, or
not-applicable observation is assessed evidence; it does not mean the
requested policy is satisfied. Apply refuses incomplete coverage, and verify
returns `incomplete` when coverage is incomplete. Unsupported controls remain
report-only unless a documented typed operation exists.

The plan digest also binds the selected local checkout: a root binding, HEAD,
branch or detached state, staged/worktree/untracked counts and a deterministic
digest of Git porcelain status bytes, configured upstream, and normalized
origin/upstream matches to `OWNER/REPO`. It hashes only tracked candidate files
inspected for structural evidence and any requested social-preview asset. The
result omits raw remote URLs, credentials, absolute checkout paths, raw status
paths, and file contents. The status digest covers status bytes, not every
worktree file. Local checks report structural presence and explicitly do not
assess content quality. Apply rechecks the checkout and local evidence before
each write and refuses local drift; verify reports `incomplete` on local
drift. Use the same `--checkout` path for every mode. Paginated REST lists
request 100 items per page and stop on a short page; the adapter errors at its
1,000-page bound. Security output redacts alert details and exposes aggregate
counts. The per-user LocalSetup state journal and target lock serialize
operations for one host and immutable repository ID. If a mutation response
is lost or ambiguous, use read-only reconciliation and never replay the write
automatically. A repeated apply is a no-op for values already at the desired
state.

Plan and verification output distinguishes `inventory` caveats,
`authorization` findings about ambient token-permission visibility, and
`requested_policy` gaps. Verification reports `verified` only when operation
read-backs pass, requested policy and required handoffs are complete, exact
control coverage is complete, and the checkout and its inspected evidence
still match the plan. An assessed unknown or unavailable result can have
complete evidence coverage while leaving a requested policy unresolved.

### Policy-scoped signature and release verification

Policy schema v2 can optionally select signing and release evidence. The
`verification.signatures` object contains exactly `commit_oid`, `tag_name`,
and `expected_primary_fingerprints`. The `verification.release` object contains
`release_id`, `tag_name`, `source_ref`, `source_commit`, `signer_workflow`,
`predicate_type`, and `artifacts`. Each artifact supplies `asset_id`, `name`,
checkout-relative `path`, and `expected_sha256`:

```json
{
  "schema_version": 2,
  "verification": {
    "signatures": {
      "commit_oid": "<full-lowercase-git-object-id>",
      "tag_name": "v1.2.3",
      "expected_primary_fingerprints": ["<trusted-primary-fingerprint>"]
    },
    "release": {
      "release_id": 123456,
      "tag_name": "v1.2.3",
      "source_ref": "refs/tags/v1.2.3",
      "source_commit": "<full-lowercase-git-object-id>",
      "signer_workflow": "OWNER/REPO/.github/workflows/release.yml",
      "predicate_type": "https://slsa.dev/provenance/v1",
      "artifacts": [
        {
          "asset_id": 234567,
          "name": "release.tar.gz",
          "path": "dist/release.tar.gz",
          "expected_sha256": "<64-lowercase-hex-digits>"
        }
      ]
    }
  }
}
```

Replace placeholders with reviewed identities and values. The tag source ref
is exactly `refs/tags/<tag_name>`; the source commit, tag, and release asset
must match the selected policy identities. Verification binds the local file's
exact bytes to its expected SHA-256 and that digest to the selected release
asset ID and name, while provenance checks bind the repository, source,
signer workflow, and predicate. A local checksum match alone does not prove
that GitHub released those bytes.

Supply OpenPGP public-key files only at verify time, repeating
`--trusted-public-key FILE` once per key. These are verify-only inputs. Key
contents and key-file paths are not stored in saved plans or printed in plans
or reports; outputs also omit absolute checkout paths and raw GitHub CLI
output. When policy requires release checks, a missing or older `gh` without
`gh release verify-asset` or `gh attestation verify` leaves the proof
unavailable/incomplete. LocalSetup does not install or upgrade `gh`
automatically. If policy declares no verification requirements, release
readiness is `not_assessed`. An apply status of `complete` describes only the
explicitly selected settings operations and their read-backs; it does not
establish release readiness.

Tracked repository content is inspected structurally from a fixed bounded
allowlist: README and badges, installation/support/contribution/security
routes, changelog/version signals, Dependabot configuration, community files,
site/Open Graph metadata inputs, accessibility inputs, and footer/attribution
inputs. Per-file SHA-256 and size records cover only inspected files; no file
contents or absolute paths are returned. This evidence does not review quality.

Schema-v2 policy distinguishes omitting `social_preview` (no requested action)
from an explicit removal. Use this shape to bind a file for a requested upload:

```json
{
  "repository_content": {
    "social_preview": {
      "action": "present",
      "asset_path": "assets/social-preview.png"
    }
  }
}
```

For removal, use `{"repository_content":{"social_preview":{"action":"absent"}}}`
and omit `asset_path`. A present asset path must be safe and relative to the
selected checkout; validation requires a contained regular non-symlink file
under 1 MB whose bytes have PNG, JPEG, or GIF magic. The plan binds its
checkout-relative path, SHA-256, size, and detected format, and apply/verify
recheck the same file and hash. The remote API confirms only whether a custom
social image is set, not that its pixels match the bound file. Complete the
Settings UI upload/removal handoff, then verify. A fresh custom-image Boolean
that matches the requested presence/absence state can satisfy that selected
setting if the local present-image asset remains valid and hash-bound. A false
or unavailable Boolean keeps the UI handoff as a requested-policy finding.
Even when the setting is complete, the exact remote pixels remain unverified.

The selected registry, named report-only categories, documented permission
limits, known API unknowns, and sources accessed 2026-09-26 are in the workflow
reference above. For a manual social-preview upload, open the target's Settings page at
`https://HOST/OWNER/REPO/settings`, then use **Social preview** → **Edit** →
**Upload an image**. Run verification after the user completes the upload.
For a requested absent image, use GitHub's documented Settings removal action
and then verify.
The API verifies only whether a custom social image is set; it does not check
the uploaded pixels or local image hash. Keep a mismatched or unavailable
handoff incomplete, and state that a completed Boolean match does not prove
exact image identity.
Tracked files and release tags remain on the local signed Git and release
paths.

`localsetup adapters` preserves the legacy adapter status list output. Use `localsetup adapters check --tools codex` for a structured, report-only adapter compatibility payload with `ok`, `adapters`, `issues`, `warnings`, `repair_hints`, `summary`, and suggested existing commands. It exits `0` when the adapter check is OK and `1` when verifier issues are present.

## LSCli And Tool-Free Completion

`localsetup agent` forwards to `lscli`. Place `agent` immediately after
`localsetup`; use LSCli subcommand options for workspace, runtime and state
selection. The entry point does not translate framework installation selectors
into task authority.

```bash
lscli --help
lscli doctor --format json
lscli profiles --profiles /private/config/profiles.json --format json
localsetup agent run --help
localsetup llm complete --help
```

[LSCli operations](LSCLI.md) owns full setup/registration, run, control, context,
session, branch, recovery and compaction syntax, including stdin/JSONL formats,
exit codes and limits. Help and diagnostic/inventory commands do not initialize
providers or create missing configuration. Coding uses explicit grants and a
qualified protected runtime with actual sandbox/resource preflight; completion
has no tools or workspace access grants.

```bash
localsetup llm complete --profile example --request request.json --profiles /private/config/profiles.json --runtime-root /private/runtimes
```

Replace the paths/profile with reviewed inputs. Supplying the request explicitly
authorizes its disclosure to that selected provider. The
[request/result schema](LSCLI_RUNTIME.md#direct-completion-contract-foundation)
and [completion command](LSCLI.md#tool-free-completion-command) define the one-attempt
contract, declared capabilities, local validation and uncertainty/exit handling.

## Typed Heartbeat And Controller Accounting

```bash
localsetup harness codex-heartbeat plan
localsetup harness codex-heartbeat budget
localsetup harness codex-heartbeat accounting --help
```

[Harness automation](HARNESS_AUTOMATION.md) owns activation and transaction
behavior. The [typed LSCli profile](../skills/ls-codex-heartbeat/references/config.md#typed-lscli-profile)
uses an owned registration, explicit private profile/grants and protected coding
limits. Installation does not activate it, and generated cron commands remain
agent-free. [Controller accounting commands](../skills/ls-codex-heartbeat/references/config.md#controller-accounting-commands)
cover init/inspect/review, action-plan, later authorization and result reconciliation;
[reserved run syntax](../skills/ls-codex-heartbeat/references/config.md#running-a-reserved-action)
requires all four explicit controller options. Ordinary fresh-profile runs and
legacy queue budget reports do not implicitly enforce reserved task accounting.
A completed execution still needs controller disposition, and uncertain effects
require evidence-backed reconciliation rather than replay.

## Install Command Options

The Python CLI install command supports the same selection model as the wrapper:

```bash
localsetup install \
  --tools codex \
  --preset suggested \
  --skill-classes development \
  --skill-tags git \
  --skills ls-context \
  --exclude-skills ls-linux-patcher \
  --yes
```

Useful install options:

| Option | Meaning |
|---|---|
| `--config PATH` | Load install config. |
| `--target-directory PATH` | Override target directory. |
| `--json` | Make JSON output explicit. |
| `--report PATH` | Write an install report. |
| `--backup-dir PATH` | Use an explicit backup location. |
| `--trace-json PATH` | Append JSONL trace events. |
| `--policy-mode permissive\|standard\|strict\|ci` | Choose policy strictness. |
| `--dependency-mode managed-venv\|prompt-only\|user-pip\|uv-sync` | Choose dependency handling. |
| `--apply` | Apply a planned operation when the command supports plan/apply separation. |
| `--mode symlink\|portable` | Adapter write mode. |
| `--platforms ...` / `--tools ...` | Platform adapter ids. |

## Repair And Health Commands

`doctor repair` is report-only by default. It infers platforms, adapter mode, repo-visible skills, repo-visible workflows, custom repo skills, and stale framework state, then returns a JSON report with `repair_schema_version: 2`.

```bash
localsetup doctor repair --target-directory .
localsetup doctor repair --target-directory . --repair-mode migration-plan --agent-prompt
localsetup doctor repair --target-directory . --repair-mode migration-plan --emit-agent-prompt /tmp/localsetup-repair.md
localsetup doctor repair --target-directory . --repair-mode safe-repair --yes
localsetup doctor repair --target-directory . --repair-mode apply-with-backups --yes
```

Safe repair only mutates LocalSetup-owned state. It can back up and remove a legacy `ls/` tree only when the target tree is framework-shaped and matches the current source framework contents byte-for-byte. Clean tracked framework trees are backed up, untracked with `git rm -r --cached -- ls`, and then removed from the working tree. Protected source checkouts, symlinks, dirty trees, framework-shaped trees with extra or modified files, and custom `ls/` content are preserved and reported as decisions for migration planning.

Custom repo skills are repo-owned by default. Adapter directories such as `.agents/skills`, `.claude/skills`, `.cursor/skills`, `.kilo/skills`, and `.opencode/skills` are shared agent surfaces, not exclusive LocalSetup-owned directories. Historical `.codex/skills` is inspected only for the proof-gated Codex managed-entry transition. Mixed adapter directories preserve custom content. Same-name collisions and unproven historical links are reported as decisions or blockers. See [Adapter ownership](ADAPTER_OWNERSHIP.md).

Health commands surface blocked repairs and handoff prompts:

```bash
localsetup health --json
localsetup health repair-queue --json
localsetup health repair-queue --agent-prompts /tmp/localsetup-prompts
```

`.localsetup/lock.json` is intentional managed repo state and remains visible to Git. Runtime summaries and journals are locally excluded through `.git/info/exclude`: `.localsetup/health.json`, `.localsetup/AGENT_STATUS.md`, `.localsetup/install-journal/`, `.localsetup/backups/`, `.localsetup/state/`, and `.localsetup/context-index/`.

## Resolver And Validation Commands

Use resolver commands when scripts, docs, workflows, or agents need directly followable LocalSetup paths:

```bash
localsetup path --json
localsetup path source-root
localsetup path framework-root
localsetup path docs-root
localsetup path tools-root
localsetup path package-root
localsetup path package ls-context SKILL.md
localsetup path doc WORKFLOW_REGISTRY.md
localsetup path tool tmux_ops
```

`localsetup path --json` refreshes `paths.json` under the configured LocalSetup home. Named path commands print one absolute path.

Use package-surface validation after changing skills, workflows, resolver tokens, materialization rules, or deployed path contracts:

```bash
localsetup validate-package-surface
localsetup validate-catalog
```

Use `reprocess-paths` for whole-project path-contract reporting. Apply mode is intentionally disabled until allowlisted rewrites are implemented:

```bash
localsetup reprocess-paths
```

Use `test-workers` to compute the default full-suite pytest worker count:

```bash
localsetup test-workers
localsetup test-workers --json
localsetup test-workers --workers 4
```

The default and maximum allocation is `max(1, floor(available CPU cores / 3))`; available cores are those usable by the current test process. `test-workers` reports the total shared budget, so a controller or CI job MUST divide it before starting concurrent test processes. `LOCALSETUP_TEST_WORKERS` or `--workers` can request a lower value; non-integer overrides fail with an explicit configuration error.

## Maintainer Commands

Run these from the repository root when changing docs, catalogs, release metadata, skills, workflows, or platform manifests:

```bash
uv run --locked python ls/tools/docs_alignment.py --repo-root . inventory
uv run --locked python ls/tools/docs_alignment.py --repo-root . check --ci
uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/localsetup.py --source-root . validate-package-surface
uv run --locked ./ls/tests/automated_test.sh
workers="$(uv run --locked python ls/tools/localsetup.py --source-root . test-workers)"
uv run --locked pytest -n "$workers" ls/tests -q
git diff --check
```

Run focused pytest targets and matching LocalSetup validators before broad suites. Reserve the full Python suite for final consolidation on broad/shared runtime changes, release or publish work, dependency changes, or explicit maintainer requests. `test-workers` defaults to `max(1, floor(available CPU cores / 3))`; concurrent test processes must share one aggregate budget.

Use `release-push` only when the release wave explicitly includes publishing:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . release-push
```

For release preparation without pushing, run `publish-preflight --base origin/main --head HEAD` first from a clean worktree. It prepares the direct version-sync candidate unstaged and returns `prepared_not_ready` when the candidate needs review and a separate generated-document receipt. Add `--fix` only when the tool should prepare and commit the required version-sync/generated-document slices before the guarded push.

Release documentation has its own source-aware gate:

```bash
localsetup release-docs plan --verify-baseline
localsetup release-docs prepare --verify-baseline --candidate .agents/state/<task-slug>/candidate.json
localsetup release-docs apply --candidate .agents/state/<task-slug>/candidate.json
localsetup release-docs render
localsetup release-docs check
localsetup release-docs notes
```

`plan`, `check`, and `notes` read the candidate; `prepare` calls the configured
protected QC model and writes private proposal evidence; `apply` writes an accepted
hash-bound proposal; `render` regenerates managed sections and the current guide
from an existing versioned record. Preparation and application require a clean
checkout. Commit authored preparation before canonical version/generated sync.
No command in this family publishes a release. For final draft validation use
`release-docs check --draft-tag v<version> --expected-commit <sha>`; complete
artifact verification is still required. See [versioning](VERSIONING.md#github-release-workflow)
for automatic release, repair, and qualification modes.

Release commands select a valid `.localsetup-release.json` from the planned
committed HEAD. Its verified published anchor owns sequential arithmetic;
`--base` remains comparison metadata. An absent policy retains patch-default.
Loose policy changes and explicit version targets cannot override the committed
contract. Invalid configuration or historical sync prefixes stop before version
mutation; ordinary target drift can be prepared by the existing sync flow. See
[version policy and exact overrides](VERSIONING.md#explicit-sequential-policy).
