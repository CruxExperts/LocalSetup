# LocalSetup review instructions

## Purpose and review standard

LocalSetup is a cross-platform framework for agent context, skills, templates,
and install workflows. Review the proposed diff and the directly relevant
surrounding code. Report only concrete, actionable regressions introduced by the
pull request; do not use the review to redesign unrelated code or enforce purely
personal style preferences.

Use these severities:

- `critical`: a likely security exposure, data loss, broken install/upgrade path,
  or release-blocking regression.
- `warning`: a credible correctness, compatibility, reliability, or test gap that
  should be addressed before merge.
- `info`: a small, demonstrable improvement that is safe to defer. Do not emit
  `info` findings for formatting, naming, or pre-existing debt alone.

Each finding must identify the affected changed line(s), explain the failure
mode, and state the smallest useful correction. If the concern depends on an
assumption that the diff or repository cannot establish, ask a concise question
instead of presenting it as a defect.

## Repository-specific risk checks

- Preserve the distinction between the framework (`ls/`) and repository-local
  state. Never permit credentials, personal data, host-specific paths, private
  maintenance ledgers, caches, logs, or planning transcripts in tracked public
  source or documentation.
- Treat external input as hostile. Check changed parsers, CLI arguments, file
  reads, and network payload handling for validation, bounds, safe errors, and
  actionable diagnostics; do not accept silent failure or broad exception
  suppression.
- Keep framework tooling Python-first. New or substantially refactored Python
  logic should keep entry points thin, use explicit responsibilities, target
  Python 3.12+, and use `pathlib` where practical. Shell and PowerShell changes
  must remain portable, explicit, and non-interactive for automation; Bash
  scripts normally require `set -euo pipefail`.
- Treat new, upgraded, or newly pinned dependencies as a supply-chain review
  surface. Flag a dependency change that lacks a clear need, supported/pinned
  version strategy, or the required security/provenance evidence. Do not ask for
  opportunistic dependency upgrades unrelated to the pull request.
- Adapter-shaped directories (such as `.codex/skills`, `.kilo/skills`,
  `.claude/skills`, and `.agents/skills`) can contain repository-owned content.
  Flag changes that delete, move, rename, or make that content exclusive without
  an explicit preservation plan.
- Generated documentation and catalogs are outputs, not hand-edited sources.
  When a change affects their source inputs, ensure the appropriate LocalSetup
  generator and validation are included or evidenced. Do not demand unrelated
  generated-file churn.
- For changes to installation, discovery, path resolution, parsing, deployment,
  or skill/workflow tooling, look for focused regression tests under `ls/tests/`.
  For narrow docs, policy, metadata, or one-line changes, accept proportionate
  static validation rather than requiring the full test suite.
- When code claims current vendor, version, API, platform, security, or
  compatibility facts, require primary-source verification or a maintained
  volatile-fact record; flag unsupported “latest” or similarly time-sensitive
  assertions.

## Validation and publication efficiency

- Accept one authoritative full-suite result for an exact candidate commit;
  hosted CI can supply it without a duplicate local suite or release rerun.
- For CI reuse changes, verify exact repository, workflow, tested SHA, latest
  run status, and every required successful job. Missing evidence must not pass.
- For sharding changes, require disjoint complete test collection and an aggregate
  that fails on any missing, failed, or cancelled shard. Cheap docs and audit
  failures must stop costly shards before they begin.
- Treat file-size warnings as advisory; flag concrete structural or behavior
  regressions instead of demanding unrelated refactors to meet a line count.
- Verify changed procedures reach source skills/workflows and generated provenance.
  Retain signature, version, release-document, and artifact verification gates.

## LSCli and release boundaries

For changes affecting LSCli, use the contracts in [LSCli](ls/docs/LSCLI.md),
[runtime boundaries](ls/docs/LSCLI_RUNTIME.md), and
[adapter ownership](ls/docs/ADAPTER_OWNERSHIP.md) to assess the actual behavior:

- Keep filesystem access and permission to disclose content to a provider
  separate. Task grants, current profile trust, approvals, leases, deadlines,
  cancellation and terminal outcomes remain supervisor-owned; saved messages,
  summaries and checkpoints cannot grant authority.
- Require a qualified sandbox before tool-enabled provider dispatch. Check
  process-descendant teardown, bounded output, protected runtime and secret
  boundaries, and reconciliation of uncertain operations before resume. Do not
  accept automatic replay of an operation whose mutation outcome is unknown.
- Check the installed artifact, not only checkout imports: the canonical vendor
  source, private wheel payload, licenses, resolved dependencies and SBOM must
  correspond. SDK imports remain isolated and their actual origins verified.
  Model requests must retain the framework runtime version in their final-send
  user agent, including compaction and direct completion.
- Preserve recorded repository/personal/shared ownership, custom neighboring
  content, profiles and sessions through repair, refresh, upgrades and rollback.
  Distinguish catalog support, deterministic fixtures and actual host
  qualification; a pass in one does not establish the others.

When the diff enters publication, apply the governing
[sequential version policy](AGENTS.md#sequential-release-version-policy) through
the canonical tooling. Count logical accepted slices in integration ancestry
order, preserving source/generated-receipt semantics and excluding already
published work. Check the combined candidate's required tests, documentation
regeneration, archive/wheel contents, dependency provenance and SBOM evidence.
Apply the [branding contract](ls/docs/BRANDING.md) to owned text, generators,
runtime output, wire identity and visually reviewed assets while preserving
technical identifiers and upstream attribution. Release acceptance must bind
the published tag and commit to downloaded artifacts and installed behavior;
a prepared candidate or local test pass alone is insufficient.

## Scope discipline

- Respect existing architecture and established debt baselines. Do not report
  old defects unless the pull request makes them worse or directly exposes them.
- Do not request a version bump, release sync, publishing action, worktree, or
  unrelated formatting sweep unless the pull request explicitly enters that
  workflow.
- Do not propose code changes outside the pull request merely to satisfy this
  review. The review agent is read-only; any follow-up implementation belongs to
  the repository maintainers.
- If there are no actionable findings, say so briefly. Do not manufacture
  comments to appear thorough.
