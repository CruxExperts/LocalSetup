---
status: ACTIVE
version: 4.44
owner_skill: ls-automatic-versioning
---

# LocalSetup Versioning

LocalSetup uses the root `VERSION` file as the source of truth for the framework version. README files, generated facts, and release artifacts should display the same normalized semantic version.

## Current Version

- Source of truth: [`../../VERSION`](../../VERSION)
- Current value: `4.44.1`
- Generated facts: [`_generated/facts.json`](_generated/facts.json)

## 4.x major-line lock and one-time numbering reconciliation

The corrected 4.x release line is explicitly major-locked. While the lock is in
force, accepted releases may increment MINOR or PATCH only; no automatic or
implicit MAJOR increment is permitted. Reject any prospective MAJOR
classification, including an explicit `Release-Type: major`, while locked. Do
not silently downgrade a major classification to minor or patch.

This is a one-time arithmetic correction. The already-published v5.6.2 tag and
assets remain immutable; that release's content maps to corrected arithmetic
4.43.2 after the exact historical issue-100 MAJOR-to-MINOR numbering
reconciliation. Branding maps to 4.44.0, followed by canonical
repository-name policy at 4.44.1. This describes the historical reconciliation:
v4.44.1 is now the current published release and arithmetic anchor. The
v5.6.2 and v4.22.9 historical release evidence remains preserved.

The SDK paging compatibility break and the Agent Q v2/envelope compatibility
break remain. Renumbering does not restore source or protocol compatibility.
The v4.44.1 release guidance must disclose both breaks clearly and must not imply
that the numbering correction restored compatibility.

## Policy

- Keep `VERSION`, the root README version line, and generated facts in sync.
- Use Conventional Commits for readable release history. Routine development defaults to one patch bump per release batch, including commits whose subject starts with `feat:`.
- `Release-Type: major|minor|patch|none` is the only way to request a major bump, minor bump, explicit patch bump, or no release bump.
- Breaking markers (`!` or `BREAKING CHANGE:`) are diagnostic only until paired with an explicit `Release-Type:` trailer. `version-plan` fails with an actionable message when a breaking marker appears without that trailer.
- Merge commits, version-sync commits, and fully canceled unreleased reverts do not request a bump.
- `version-plan` derives the target from the base `VERSION` and the net unreleased bump. Every in-range `chore: sync release version X` commit must name that target; when any sync commit is present, the HEAD `VERSION` must also equal it, including no-bump batches.
- Version and documentation sync are automatic. Local hooks plan the bump from outgoing commits, update known version surfaces, regenerate docs artifacts, and create a version-sync commit before push.
- From a clean worktree, `publish-preflight` without `--fix` deliberately prepares direct version surfaces as an unstaged candidate. When it changes files, it returns `prepared_not_ready`; review and commit that candidate, then create and validate the separate generated-document receipt. It never stages or commits the candidate.
- `publish-preflight --fix` is the one-command alternative: it performs the clean preparation plus the version-sync and generated-document commits.
- Reverts of unreleased commits cancel the pending bump before push. Reverts of already-published commits are released as a monotonic patch version rather than decreasing `VERSION`.
- Skill versions are separate from the framework version and live in each skill's `SKILL.md` frontmatter under `metadata.version`.
- Workflow package catalog data is regenerated from `ls/workflows/*/workflow.yaml`; version-sync checks include workflow registry, quick reference, and generated workflow catalog surfaces.

## Explicit sequential policy

The canonical planner selects `sequential-logical-slices` automatically when the
planned HEAD contains a valid, committed `.localsetup-release.json`. This shared
selection applies to `version-plan`, `version-sync`, `publish-preflight`,
`release-push`, the pre-push hook and release CI. Without that file, the existing
`patch-default` policy remains. Python callers can also select sequential mode
explicitly for an unconfigured repository; an explicit argument cannot override
a conflicting committed policy. A loose, deleted or edited worktree policy does
not replace the selected commit's contract. Selection does not authorize publication.

The [release policy schema](../config/release-policy.schema.json) defines the
strict configuration. Schema 1 has `schema_version: 1`,
`policy: "sequential-logical-slices"`, an `anchor` with full lowercase commit
SHA, canonical `version` and matching `vMAJOR.MINOR.PATCH` tag, and an
`overrides` array. Schema 2 adds `major_line: 4` and a one-time
`reconciliation` object containing the original 4.x anchor, the immutable
pre-correction published anchor, the source/corrected cutoff versions, and exact
breaking-slice commit SHAs reclassified from MAJOR to MINOR. It preserves checks
of the original release sync history and uses the corrected cutoff for arithmetic
until a corrected 4.x tag is published; after that, arithmetic starts from the
new verified 4.x anchor. The historical v5.6.2 published anchor remains available
for release-document coverage. Runtime validation binds each tag to its commit,
checks ancestry and replayed syncs, and requires the cutoff VERSION and corrected
arithmetic to match. In schema 2, HEAD must also carry the corrected version
before the plan is ready, even when no new source slice adds a bump.

The file must be a regular Git blob of at most 64 KiB. Duplicate keys, unknown
fields, invalid types and duplicate override SHAs fail planning. The planner
checks committed versions and ancestry using local Git objects and refs;
maintainers must independently verify the tags were actually published. Planning
never fetches release information.

Each optional override has exactly `commit`, `slice` and `classification` fields.
It names one full unpublished source SHA, a lowercase slice ID of at most 128
characters, and `none|patch|minor|major`. Overrides can reconcile reviewed legacy
classification and grouping without changing commit messages or `raw_bump`.
They cannot name merges, generated receipts, reverts or already-published work,
or downgrade breaking changes. They apply consistently to the final fold and
historical sync prefixes; no subject similarity or global name deduplication is
used. Preserve source ancestry when integrating exact-SHA mappings.

The repository-only policy is excluded from release archives and must not be
copied into converted projects. After verifying a newly published tag, advance
the anchor to that exact published commit/version and remove overrides already
covered by it. Review and commit that policy change as ordinary source work;
never rewrite existing history or hand-edit generated versions to force a match.

A `feat:` source slice defaults to MINOR and resets PATCH; other source changes
default to PATCH, with `Release-Type: major|minor|patch|none` for explicit impact.
`Release-Slice: lowercase-id` groups unpublished members; an unlabelled source
uses its unique SHA. The first integrated member anchors the slice, and the
highest classification among its final members determines its one increment.
For example, patch foundation A, independent patch B and a later minor member
of A yield one minor increment for A followed by one patch for B.

Integration order follows first-parent history before newly introduced side
ancestry in Git parent order; timestamps do not order releases. Actual merge
commits and generated-only receipts do not increment the version. Receipt
exclusion validates changed paths and restricts mixed authored files to generated
facts blocks. A receipt-like subject alone grants no exclusion. Sync exclusions
compare canonical version/generated content rather than exempting arbitrary docs.

Breaking markers require an explicit `Release-Type: major` compatibility decision.
Minor, patch and none cannot conceal them. An active 4.x major-line lock rejects
major classifications; restore compatibility or stop release work until the lock
is explicitly amended. Duplicate/invalid metadata, ambiguous
reverts and partial logical-slice reverts fail for reviewed reconciliation. Exact
native Git revert SHAs cancel fully reverted unpublished slices only after a
single-parent raw path/blob/mode comparison proves the complete inverse. Mixed
changes, conflict-adjusted reverts and later changes to affected files require
explicit reconciliation; later unrelated files remain untouched. Published work
is reverted as a new maintenance outcome.

Each historical sync is checked against its own ancestor-prefix target, including
its committed VERSION. The latest sync and HEAD must match the final target.
Incorrect historical syncs make the plan nonrepairable and stop mutation before
version files change. Ordinary target drift remains repairable through canonical
sync tooling. With committed policy, `--base` is comparison metadata and arithmetic
always starts at the verified anchor. In explicit unconfigured sequential mode,
the selected base must be an ancestor. Upstream refs alone do not prove publication.

Sequential output retains the existing fields and adds `logical_slices` (slice,
anchor, source_shas, classification, before_version, after_version),
`excluded_commits`, `version_sync_checks`, `latest_sync_matches_target`,
`repairable`, `anchor`, `release_overrides`, `comparison_base` and
`comparison_base_resolution`. `base` and `base_resolution` identify the arithmetic
anchor; comparison metadata records the caller's independently selected ref.
Schema 2 also returns the verified `published_anchor`, `major_line`,
`line_reconciliation`, and any `major_line_violations`. `bump` is the highest
applied category for compatibility; `target_version` is the ordered fold, not
one application of that aggregate category.

## Local workflow

Install hooks once per clone:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . install-hooks
```

For normal release pushes, use:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . release-push
```

Before diagnosing a version or tag mismatch, fetch remote tags so local state
matches GitHub:

```bash
git fetch --tags origin
```

For an unpublished release batch, `version-plan` normally reports `ok: false`
until a version-sync commit exists. First prepare the direct, unstaged candidate
from a clean worktree:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . publish-preflight --base origin/main --head HEAD
```

If the result is `prepared_not_ready`, inspect and commit the direct
version-sync candidate, then generate and validate its post-version
generated-document receipt. Use `--fix` only when the tool should prepare and
commit both slices:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . publish-preflight --base origin/main --head HEAD --fix
```

Raw `git push` is guarded by `.githooks/pre-push`. If a version-sync commit is needed, the hook creates it and stops that push; rerun the push after reviewing the generated commit. This two-step guard is intentional because Git determines the commit being pushed before the `pre-push` hook runs.

Useful planning check:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . version-plan
```

The `version-plan` output includes the selected `policy`, diagnostic `raw_bump` values from Conventional Commit parsing, the effective release `bump`, and `version_sync_matches_target` for validating in-range version-sync commits.

## GitHub release workflow

Prepare and review the versioned release content record in the source checkout.
It generates homepage highlights, current guide links, the guide, and GitHub
release notes. Run `release-docs render` and `release-docs check` before the
canonical version and generated-document sync commits. The protected QC authoring
route remains available through explicit `release-docs prepare` and `apply`
commands when editorial help is wanted; the ordinary hosted release consumes
already committed and signed source. It never authors, signs or pushes source.

The `publish` workflow is explicitly dispatched on `main`. Its modes are
`release` (validate committed prose and build a draft from a pre-existing signed
tag), `repair` (validate reviewed committed prose and update notes for the current
published release without changing its tag or assets), and `qualify` (prepare
model evidence on an ephemeral runner without integration or release mutation).
Model settings reuse the existing `QC_LLM_*` configuration. Hosted preparation
uses the verified published framework wheel's hashed dependency exports and
builds the completion runtime wheel offline from the clean candidate commit.
It verifies packaged source and data bytes against that commit and rejects dependency
lock changes before installing through the protected runtime owner. The runtime
receipt records the candidate commit and wheel digest. It does not select new
dependency versions. Runtime provisioning failure retains evidence and blocks the
affected run; it is never treated as successful qualification.
Optional qualification enforces a total completion-call budget. Its hosted
workflow allows two hours for model calls inside a 135-minute job, leaving runner
time for validation and evidence upload. Direct local runs retain the 30-minute
default unless the caller explicitly configures another positive deadline. An
oversized release stops before publication with an actionable scope/slice error;
incomplete audits are never recorded as successful coverage.

Push the accepted source to `main` and wait for its required validation. Then
create and verify an OpenPGP-signed annotated `vX.Y.Z` tag at that exact commit,
push the tag, and dispatch `publish` in `release` mode. The workflow checks the
tag and commit against the required signer fingerprint using public certificate
material before building. It verifies version sync and generated docs, runs the
framework suite, builds and verifies the archive/checksum/SBOM, attests the
archive, and creates a draft with `--verify-tag`. Existing releases and uncertain
API lookups stop preparation for reconciliation; reruns never overwrite assets.

Complete the draft before publication. Attach the verified wheel/sdist and any
qualified native bundle, provenance, SBOM and corresponding source assets required
by the release. Verify the complete asset inventory, checksums, licenses, commit
identity and accurate release notes, then publish the existing draft:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . release-docs notes > release-notes.md
gh release edit "v$(cat VERSION)" --notes-file release-notes.md
git fetch origin "refs/tags/v$(cat VERSION):refs/tags/v$(cat VERSION)"
uv run --locked python ls/tools/localsetup.py --source-root . release-docs check --draft-tag "v$(cat VERSION)" --expected-commit "$(git rev-parse HEAD)"
gh release edit "v$(cat VERSION)" --draft=false
```

These commands require existing publication authority and a reviewed payload.
`release-notes.md` denotes the prepared notes file; do not commit private release
preparation. Workflow success proves draft preparation, not completed publication.
Download the published assets and perform the exact-release acceptance checks.
With [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases),
assets and the associated tag are fixed at publication. If required assets were
omitted, preserve that release and prepare a corrected patch release; do not
attempt to replace assets or move its tag.

## Verification

Before release, run:

```bash
uv lock --check
uv sync --locked --all-groups
git fetch --tags origin
uv run --locked python ls/tools/localsetup.py --source-root . publish-preflight --base origin/main --head HEAD
# Review and commit a prepared_not_ready direct version-sync candidate, then create and validate its generated-doc receipt.
uv run --locked python ls/tools/localsetup.py --source-root . version-plan
uv run --locked python ls/skills/ls-framework-audit/scripts/run_framework_audit.py --output /tmp/ls-framework-audit.md
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/localsetup.py --source-root . scan-migration
uv run --locked python ls/tools/localsetup.py --source-root . audit-global-first
uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
uv run --locked python ls/tools/localsetup.py --source-root . package --out "dist/localsetup-v$(cat VERSION).tar.gz"
uv run --locked python ls/tools/localsetup.py --source-root . verify-release "dist/localsetup-v$(cat VERSION).tar.gz"
git diff --check
```
