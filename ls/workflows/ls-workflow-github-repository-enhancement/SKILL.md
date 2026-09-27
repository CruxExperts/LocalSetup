---
name: ls-workflow-github-repository-enhancement
description: Audit, plan, apply, and verify documented GitHub repository settings through LocalSetup's fixed CLI workflow.
metadata:
  version: "1.0"
---

# GitHub repository enhancement

Use this workflow for a structured, evidence-backed review of a GitHub
repository and its selected local checkout. It returns seven group objects and
one observation for every control in LocalSetup's fixed registry. The registry
defines exact coverage for this workflow; it does not claim to include every
setting or control available across GitHub. Observations distinguish remote,
inherited, local, unavailable, unknown, not-applicable, and UI-handoff
evidence. An audit does not authorize changes.

## Fixed command and target identity

Select one GitHub host and one repository explicitly:

`--mode` is required and accepts exactly one of `audit`, `plan`, `apply`, or
`verify`:

```text
localsetup github-repo --repository OWNER/REPO --hostname HOST --checkout PATH --mode MODE
```

`--repository OWNER/REPO` selects the remote target. Always supply
`--hostname` (for example, `github.com` or the organization's GitHub Enterprise
host). The existing global `--repo` option continues to select LocalSetup's
source checkout; it never means the GitHub repository being changed. Do not
substitute a clone path, remote URL, or local directory for `--repository`.
`--checkout PATH` selects the local Git checkout used for evidence and defaults
to `.`. Use the same checkout for audit/plan and later apply/verify. This is
separate from the global `--repo` source-checkout option.

The plan binds the normalized host and repository slug to GitHub's immutable
repository ID and the authenticated actor. The slug is a lookup key, not the
durable identity. If a repository is renamed, transferred, replaced, or the
authenticated actor changes, stop and create a new plan. Do not carry an old
plan across hosts or repository IDs.

## Four modes

- `audit` performs reads and reports observed values, capability and
  permission limits, inherited policy, and tracked-file findings. It makes no
  changes.
- `plan` compares observed state with explicit repository policy and emits a
  deterministic, reviewable plan. Supply the policy with `--policy POLICY.json`;
  the command writes `plan.json` and `plan.md`, then returns a JSON summary
  containing both paths, operation IDs, and the SHA-256 plan digest. By default,
  both files are stored under LocalSetup's per-user private state root at
  `github-repository-operations/<target-key>/plans/<plan-digest>/`. The target
  key binds the normalized hostname and immutable repository ID. Use the
  optional `--output-directory DIR` to choose another private directory.
  When using the default plan path, the LocalSetup state root must be owned by
  the current user with mode `0700`; an unsafe state root or parent fails
  closed. Apply also requires that secure state root for its target journal.
  An explicit output directory changes where plan files go, not the state-root
  requirement for later apply. The selected output directory must be
  user-owned with mode `0700`; if missing, LocalSetup creates it at `0700`.
  Path components cannot be symlinks. Ancestors must be root- or user-owned
  and not group/world writable, except root-owned sticky directories such as
  `/tmp`. Plan files are created exclusively at mode `0600` without following
  symlinks; existing files must be user-owned mode-`0600`, single-link regular
  files.
  It must not invent desired values from the current state or a generic
  best-practice list. Pass the reported `plan.json` path to apply or verify and
  retain the reported SHA-256 digest for exact review and authorization.
  The schema-v4 plan digest binds remote observations, verification
  requirements, and the local checkout
  snapshot: root binding, HEAD, branch or detached state, staged/worktree/
  untracked counts and a digest of Git status bytes, configured upstream and
  normalized origin/upstream target matches, structural content observations,
  and hashes of inspected files. It contains no raw remote URL, credential,
  absolute checkout path, raw status path, or inspected file content. The
  status digest covers Git's porcelain status bytes; it is not a digest of all
  worktree content. Local content evidence records presence and structure, not
  quality. The `audit_coverage` receipt states whether each registered control
  has exactly one valid observation and reports missing, duplicate, invalid,
  or incomplete control IDs. A reason-backed unknown, unavailable, inherited,
  local, UI-only, or not-applicable observation is still assessed evidence;
  it does not by itself mean that a requested policy is satisfied. Apply
  refuses incomplete coverage or local checkout/evidence drift. Verify returns
  `incomplete` for incomplete coverage or local drift.
  Report-only findings still distinguish inventory, authorization, and
  requested-policy gaps. Unsupported controls remain report-only unless a
  documented typed operation exists.
  Each typed operation is shown in JSON and Markdown with a canonical
  interface descriptor: selected transport, fixed command or HTTP method and
  endpoint template, binding to `plan.target`, required command flags, and the
  reason an API method was selected. The descriptor is part of the operation
  identity and plan digest, so review the displayed interface with each
  operation before authorization.
- `apply` accepts the saved plan at `--plan PLAN.json` only when its exact
  digest is supplied with `--authorize-plan DIGEST`. Select each approved
  typed operation by its exact
  `--operation OP_ID`; repeat that option for additional approved operation
  IDs. The digest and selected IDs authorize only that plan subset. Never
  authorize by wildcard, group, or a prose summary. Read and review the plan
  before invoking apply.
- `verify` reads GitHub and the selected checkout again, then compares them
  with the plan. It requires `--plan PLAN.json`; when verifying policy-scoped
  signatures, supply each trusted public key with repeatable
  `--trusted-public-key FILE`. It returns fresh registered
  control observations, the exact coverage receipt, local evidence, operation
  read-backs, and categorized findings. It reports `verified` only when
  coverage is complete, the bound checkout and inspected content are
  unchanged, operation read-backs pass, and requested policy and required
  handoffs are complete. Incomplete coverage or local drift returns
  `incomplete`.

## Policy-scoped signature and release verification

The policy remains schema v2. Its optional `verification` object declares
which signing and release evidence is required; it does not make an audit of
all repository releases or settings. `verification.signatures` contains
`commit_oid`, `tag_name`, and `expected_primary_fingerprints`. It selects one
full Git commit object ID, one annotated tag, and the exact trusted OpenPGP
primary-key fingerprints allowed to sign both objects. Verification uses the
selected checkout's local Git objects and the caller-supplied public keys.

`verification.release` contains `release_id`, `tag_name`, `source_ref`,
`source_commit`, `signer_workflow`, `predicate_type`, and `artifacts`. Each
artifact has `asset_id`, `name`, `path`, and `expected_sha256`. The source ref
must be exactly `refs/tags/<tag_name>`; its commit and tag must match the
selected source identities. Each artifact binds a specific asset ID and name
in that release to a safe checkout-relative file and its expected SHA-256.
Verification checks the selected local bytes and the exact release, tag,
source, workflow, predicate, and asset identities required by policy. A local
hash match by itself does not prove that a release published those bytes.

For example, the optional policy shape is:

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
      "signer_workflow": "OWNER/REPO/.github/workflows/release.yml@refs/tags/v1.2.3",
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

The fingerprint, object ID, asset ID, file path, and digest above are
placeholders; replace them with reviewed identities and values. Pass trusted
public-key files only to `verify`, using `--trusted-public-key FILE` once per
key. Key contents and paths are verification inputs: they are never saved in
the plan or printed in plans or reports. Plans and reports also omit absolute
checkout paths and raw GitHub CLI output.

Saved plans now use schema v4; regenerate any schema-v2 or schema-v3 saved
plan from its reviewed policy before apply or verify. The policy stays at
schema v2. Without
verification requirements, release readiness is `not_assessed`; repository
settings results do not supply missing release proof. When requirements are
present, unavailable or old GitHub CLI installations that lack the needed
`gh release verify-asset` or `gh attestation verify` commands leave verification
unavailable/incomplete. The workflow does not install or upgrade `gh`
automatically. `apply` completion describes only the explicitly selected
settings operations and their read-backs; it does not claim release readiness.

## Mutation interface selection

Use native `gh repo edit` when its documented command and flags express the
complete typed operation. Before dispatch, LocalSetup checks bounded
`gh repo edit --help` output for every required flag. An unavailable command,
missing flag, unsupported feature, or inconclusive help check fails closed;
the apply does not silently switch transports or retry through another
interface. The selected command, target binding, and required flags appear in
the operation descriptor.

Use the explicitly described REST method and endpoint through `gh api` only
when no native command exactly expresses the complete operation; the
descriptor records the API selection reason. Ruleset mutations use REST
through `gh api`: `gh ruleset` documents listing, checking, and viewing, but
not ruleset writes. A failed or ambiguous mutation is reconciled by read-only
queries; never change transport or replay it automatically.

Policy schema v2 supports the desired Boolean field
`repository.web_commit_signoff_required`. `true` requires contributors to sign
off on commits made through GitHub's web interface; `false` removes that
requirement. The registered control is
`collaboration.web_commit_signoff`. The official [`gh repo edit` options](https://cli.github.com/manual/gh_repo_edit)
do not document an exact native flag for this field. Its typed operation
therefore uses `PATCH /repos/{owner}/{repo}` through `gh api`, setting
`web_commit_signoff_required` in the request body and recording the REST
selection reason in the interface descriptor. GitHub documents this Boolean
in [Update a repository](https://docs.github.com/en/rest/repos/repos#update-a-repository).

All remote write operations are enabled only for `github.com` while the
GitHub Enterprise Server API-version matrix remains unverified. Audits and
reads may run against other GitHub hosts, but requested drift there is returned
as incomplete/report-only with a specific compatibility reason. Read support
does not imply write support.

The ordinary plan excludes destructive or access-changing operations,
including repository visibility changes, default-branch replacement,
collaborator changes, secret replacement, deleting rulesets, and reducing
protections. Default-branch replacement is a supported typed operation only
in its own policy and plan, after the requested branch and its head are
observed; other excluded changes also need a separate reviewed policy and
plan with their own risk, recovery, digest, and exact operation authorization.
IDs from the ordinary plan cannot authorize them. Public/private visibility can be planned
on `github.com` only as the sole setting in its own plan. Split or reject any
policy that mixes visibility with other operations before apply. Requested
visibility on other hosts remains report-only because the Enterprise Server
support matrix is unverified. `internal` remains unresolved because the
documented CLI and REST visibility surfaces differ. Other controls without a
supported typed operation remain report-only.

Each mutation refreshes the target and its preconditions immediately before
writing. Already-correct values are no-ops. Plans are idempotent: unchanged
policy and observations produce the same plan; repeating an accepted apply
does not repeat completed mutations. There is no arbitrary endpoint, shell
command, GraphQL document, or free-form write input in an operation.

The workflow uses a per-user LocalSetup state root, with a target lock and
durable journal keyed by normalized host and immutable repository ID. The lock
serializes operations for the same repository without blocking other targets.
After an interrupted or ambiguous write, the journal remains pending until a
read-only reconciliation establishes remote state. Reconcile first; never
automatically replay an uncertain write. Only a new, explicitly reviewed
invocation can authorize a later mutation.

## Seven groups and registered controls

Audit and verify return these seven group keys and one row per registered
control. The fixed registry is the exact coverage contract for this workflow,
not an exhaustive inventory of GitHub's settings surface. The coverage receipt
includes `status`, expected and observed counts, and lists of missing,
duplicate, invalid, and incomplete control IDs. Coverage is incomplete if any
registered control is absent, duplicated, malformed, or explicitly
incompletely observed. A valid reason-backed `unknown` or `unavailable` result
is recorded as assessed evidence, not as a policy success. Rows also identify
applicability, authority, capability, and observation state; inherited values
are not silently represented as repository-owned. Authentication failure
stops the command. Do not infer that an inaccessible field is inherited or
that an unknown value is disabled.

1. **Identity and discovery:** repository description, homepage, visibility,
   default branch, template status, LocalSetup-selected repository feature
   flags, and topics. The audit marks visibility and default-branch replacement
   as high-impact boundaries. Public/private visibility can be planned only as
   an isolated operation with its own exact authorization; `internal` remains
   unresolved because the documented CLI and REST surfaces differ.
2. **Collaboration:** selected repository feature flags and merge settings:
   merge methods, auto-merge, delete-branch-on-merge, squash/merge commit
   title or message defaults, and web commit signoff
   (`collaboration.web_commit_signoff`, controlled by
   `repository.web_commit_signoff_required`). Named report-only categories
   are collaborator access, issue forms/pull-request templates/funding links,
   and organization Projects policy.
3. **Git governance:** repository and parent ruleset summaries containing ID,
   name, target, source type/source, enforcement, conditions, rule count, and
   bypass-actor count. Named report-only categories are organization or
   Enterprise rulesets, signed-commit/tag verification and release-tag policy
   that require local Git evidence, and required-status-check health.
   Repository-targeted ruleset writes do not override inherited rules.
   Local Git history, signatures, recovery, and tag procedures remain owned by
   [ls-git-workflows](../../skills/ls-git-workflows/SKILL.md).
4. **Actions and deployment:** selected Actions policy fields (`enabled`,
   `allowed_actions`, SHA-pinning requirement), workflow token permissions,
   selected-action allowlist fields only when selected mode is active, and
   Pages build type/source, custom domain, HTTPS, status, and public state.
   Named report-only categories are organization Actions policy and environment,
   deployment, or workflow-health observations.
5. **Security and supply chain:** available security-and-analysis status
   values, Dependabot-alert and automated-security-fix state, and aggregate
   open Dependabot/code-scanning alert counts. Reports redact alert details
   and identifiers. Named report-only categories are private alert details,
   plan-gated feature availability, and malware protection/attestations for
   which this runtime has no repository-setting interface.
6. **Releases:** immutable-release state and an inventory summary containing
   release counts plus the latest published release's ID, tag, draft/prerelease
   flags, immutable flag, asset count, asset-digest count, and publication
   time. Named report-only categories are signature/attestation verification
   and release-tag/artifact recovery procedures. Tag and release mutation stay
   with the signed local release workflow.
7. **Repository content:** registered local structural checks cover README
   presentation and badges, installation/support/contribution/security
   routes, changelog/version signals, Dependabot configuration, community
   files, site/Open Graph metadata inputs, accessibility inputs, and footer /
   attribution inputs. Each record reports tracked candidate presence and
   hashes only inspected files; content and absolute paths are omitted, and
   quality review is explicitly not assessed. The remote social-preview
   control reads the GraphQL `usesCustomOpenGraphImage` Boolean. A fresh true
   value can satisfy a requested present setting when its selected local file
   remains valid and hash-bound. A fresh false or unavailable value leaves
   the Settings UI handoff as a requested-policy finding. In either case, the
   API and local file evidence cannot establish that the uploaded pixels match
   the selected file. Upload and removal remain Settings UI actions, not
   remote mutations.

The local checkout snapshot binds its root without returning an absolute path,
and records HEAD, branch/detached state, dirty counts and a deterministic
digest of porcelain status bytes, configured upstream, and normalized
origin/upstream match to the requested `OWNER/REPO`. Local evidence hashes only
the bounded allowlist of tracked candidate files it inspected, plus a
requested social-preview file when present. Local evidence is structural: it
does not assess the quality or correctness of content. The selected checkout
is re-read for apply and verify; any change from the plan fails closed.

Paginated REST lists currently used for rulesets, Dependabot alerts,
code-scanning alerts, and releases request 100 items per page and stop on a
short page; the adapter reports an error rather than claiming completeness if
the 1,000-page bound is reached. Topics and the social-preview Boolean are
single-resource reads. This workflow does not paginate arbitrary GitHub lists
or GraphQL connections. Permission, inheritance, plan, and endpoint limits
appear only where the adapter can observe them or in the named report-only
reason; an unknown is not evidence of support or absence.

## Social-preview handoff

Schema-v2 policy distinguishes no requested action from an explicit absent
image. To bind a local file for upload review, use:

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
and omit `asset_path`. A `present` action requires a checkout-relative safe
path. The file must resolve within the selected checkout as a regular,
non-symlink file, be under 1 MB, and have PNG, JPEG, or GIF magic bytes. The
plan records its checkout-relative path, SHA-256, size, and detected format,
not its contents or absolute path. Apply and verify recheck the same path and
hash; local drift fails closed. An absent action records a removal handoff.
An omitted action remains distinct from removal.

The reviewed GitHub documentation exposes social-preview upload through the
repository Settings UI. No upload API was found in the REST/GraphQL references
reviewed on 2026-09-26; absence from those references is not proof that no API
exists. Never guess an endpoint or report upload complete from an image file
being present locally.

When policy requests a social-preview state and the observed Boolean does not
match, plan output provides a Settings URL and a UI handoff. For a desired
custom image, follow the documented upload steps below. Local validation and
hash binding prove which safe local file was reviewed; they do not prove what
GitHub stores. The CLI cannot compare remote pixels to the local digest:

1. Open the Settings page for the selected repository:
   `https://HOST/OWNER/REPO/settings`.
2. Go to **Social preview** → **Edit**. For a desired custom image, choose
   **Upload an image** and select the exact image bound by the reviewed plan.
   If the desired state is no custom image, choose **Remove image**.
3. Complete GitHub's upload flow, then run `verify` again. A fresh true
   `usesCustomOpenGraphImage` read-back can complete the requested present
   setting while its local asset remains valid and hash-bound. If the Boolean
   is false or unavailable, keep the handoff as a requested-policy finding.
   A Boolean match does not establish the remote image's pixel identity; state
   this limitation even when the selected setting is complete. For a requested
   absent image, a fresh false value satisfies only that selected setting.

GitHub documents PNG, JPG, or GIF uploads under 1 MB and recommends 1280 ×
640 pixels. The workflow does not create or choose an image on the user's
behalf.

## Composition and ownership

Follow [ls-github-publishing-workflow](../../skills/ls-github-publishing-workflow/SKILL.md)
for publication scope, public/private boundaries, and the local signed-release
process. Compose [ls-safety-and-backup](../../skills/ls-safety-and-backup/SKILL.md),
[ls-documentation-alignment](../../skills/ls-documentation-alignment/SKILL.md),
[ls-docs-organization](../../skills/ls-docs-organization/SKILL.md),
[ls-test-runner](../../skills/ls-test-runner/SKILL.md),
[ls-framework-compliance](../../skills/ls-framework-compliance/SKILL.md),
[ls-git-workflows](../../skills/ls-git-workflows/SKILL.md), and
[ls-automatic-versioning](../../skills/ls-automatic-versioning/SKILL.md) for
their respective owners. Use [Repository Maintenance](../../docs/REPO_MAINTENANCE.md)
and the [Command Reference](../../docs/COMMAND_REFERENCE.md) for this
repository's specific checks and command forms. A successful API write does
not satisfy the repository's Git, release, documentation, or framework gates.

## Source facts and limits

GitHub documentation is rolling. The following interface and permission facts
were checked against official GitHub documentation and CLI manuals on
**2026-09-26**; the REST examples used API version `2026-03-10`. Recheck these
sources before changing adapter behavior or making a current-support claim:

- Repository settings, topics, visibility, and CLI surfaces (repository
  setting writes require `Administration:write`):
  [REST repositories](https://docs.github.com/en/rest/repos/repos),
  [GraphQL repositories](https://docs.github.com/en/graphql/reference/repos),
  [`gh repo edit`](https://cli.github.com/manual/gh_repo_edit),
  [`gh repo view`](https://cli.github.com/manual/gh_repo_view), and
  [`gh api`](https://cli.github.com/manual/gh_api).
- Rulesets and inheritance:
  [repository rules](https://docs.github.com/en/rest/repos/rules),
  [organization rules](https://docs.github.com/en/rest/orgs/rules), and
  [`gh ruleset`](https://cli.github.com/manual/gh_ruleset).
- Collaboration and Actions:
  [collaborators](https://docs.github.com/en/rest/collaborators/collaborators),
  [Actions permissions](https://docs.github.com/en/rest/actions/permissions),
  [Actions policies](https://docs.github.com/en/rest/actions/policies).
- Pages and security:
  [Pages](https://docs.github.com/en/rest/pages/pages),
  [Dependabot alerts](https://docs.github.com/en/rest/dependabot/alerts),
  [secret scanning](https://docs.github.com/en/rest/secret-scanning/secret-scanning),
  [code scanning](https://docs.github.com/en/rest/code-scanning/code-scanning).
- Releases, attestations, and tracked content:
  [releases](https://docs.github.com/en/rest/releases/releases),
  [release assets](https://docs.github.com/en/rest/releases/assets),
  [artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations),
  [repository contents](https://docs.github.com/en/rest/repos/contents).
- Pagination and social preview:
  [REST pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api),
  [GraphQL pagination](https://docs.github.com/en/graphql/guides/using-pagination-in-the-graphql-api),
  [customizing a repository social preview](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).

Known limits from that review: REST's repository-update visibility schema
lists public/private while `gh repo edit` documents internal visibility; some
GraphQL mutation permission mappings are not specified; the immutable-release
plan matrix and exact GitHub Enterprise Server compatibility are not
established. Preserve these unknowns as unknowns. Do not generalize GitHub.com
support to Enterprise Server or interpret undocumented support as a safe write
path.
