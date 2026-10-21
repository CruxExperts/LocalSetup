---
name: ls-wf-provenance-drift-check-refresh
description: Refresh owner-specific mutable provenance and generated documentation without rewriting release or installed-target evidence.
metadata:
  version: "1.0"
---

Use this agent workflow to scout provenance drift, map each finding to its canonical owner, and refresh only owner-approved mutable outputs. This package orchestrates broader reviewed refresh work. The separate scheduled GitHub Actions job checks only the canonical Herdr provenance manifest and creates an issue handoff for confirmed upstream drift.

Start with bounded, independent, read-only scouts. Report each finding with its owner, source/evidence, mutable or lifecycle-bound status, and proposed canonical action. The controller reviews and scopes findings before dispatching one worker per owner. Workers edit only their owner's scope, then the controller verifies generated evidence and reviews the combined result.

Canonical docs owners: [Output and doc generation](../../docs/OUTPUT_AND_DOC_GENERATION.md), [workflow packages](../../docs/WORKFLOW_PACKAGES.md), [versioning](../../docs/VERSIONING.md), and [documentation alignment](../../skills/ls-docs-source-drift/SKILL.md). Use `ls/tools/generate_docs_artifacts.py`, `ls/tools/localsetup.py generate-docs`, and `ls/tools/docs_alignment.py` only for outputs those owners define.

Never rewrite release records, signed tags, published assets/SBOMs/attestations, exact-SHA CI evidence, dependency snapshots, or installed-target markers in a scheduled refresh. The Herdr watcher compares official release and source evidence, then creates a deduplicated issue handoff for confirmed drift; it never edits repository contents or upgrades a host. Report unknown observations as unknown. Do not repair existing machine targets. See [repository maintenance](../../docs/REPO_MAINTENANCE.md) and [pre-publish safeguards](../ls-wf-release-publish-readiness/SKILL.md) before release-related handoffs.

## Owner map

- Source-tree and package-marker provenance: `ls/core/provenance_source.py` and `ls/core/provenance.py`; refresh through source-backed install, update, or repair, not report-only reads.
- Current generated documentation, facts, asset inventory, and artifact registry: `ls/tools/generate_docs_artifacts.py`, `localsetup generate-docs`, and docs alignment generators.
- Dependency locks, SDK exports, receipts, and source SBOMs: their dependency and package owners; refresh only after an accepted dependency change.
- Exact-commit CI evidence: validation workflows; evidence applies to one repository revision and is never rebound.
- Release records, signed tags, archives, SBOMs, and attestations: authorized release lifecycle; preserve published evidence as immutable.
- Installed-target markers and client package state: the exact target's install/update/repair lifecycle; no fleet-wide or scheduled repair.

## Scheduled handoff

The scheduled GitHub Action checks only the canonical Herdr provenance manifest against bounded official release and watched-file evidence. It creates a deduplicated issue handoff for confirmed drift and retains a structured observation report as a run artifact; it does not run documentation generators, create a patch, write a branch, open a pull request, or sign commits. Unknown observations remain unknown and do not create speculative issues. Review the issue and its immutable references, then use the owning Herdr maintenance workflow to assess any proposed update. The broader local workflow may refresh generated outputs only through their canonical owners and checks. Commit accepted repository changes with the required signer; push only with explicit authorization.

For each run, scouts report owner, source revision, evidence, mutability, and disposition. The controller scopes one worker per owner, validates each canonical refresh, reviews the aggregate diff, writes the evidence record, and creates a task-scoped signed local commit. Lifecycle-bound drift is a handoff, not a refresh request.
