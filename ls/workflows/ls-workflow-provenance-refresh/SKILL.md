---
name: ls-workflow-provenance-refresh
description: Refresh owner-specific mutable provenance and generated documentation without rewriting release or installed-target evidence.
metadata:
  version: "1.0"
---

Use this agent workflow to scout provenance drift, map each finding to its canonical owner, and refresh only owner-approved mutable outputs. This package orchestrates agents; it is not the scheduled GitHub Actions job.

Start with bounded, independent, read-only scouts. Report each finding with its owner, source/evidence, mutable or lifecycle-bound status, and proposed canonical action. The controller reviews and scopes findings before dispatching one worker per owner. Workers edit only their owner's scope, then the controller verifies generated evidence and reviews the combined result.

Canonical docs owners: [Output and doc generation](../../docs/OUTPUT_AND_DOC_GENERATION.md), [workflow packages](../../docs/WORKFLOW_PACKAGES.md), [versioning](../../docs/VERSIONING.md), and [documentation alignment](../../skills/ls-documentation-alignment/SKILL.md). Use `ls/tools/generate_docs_artifacts.py`, `ls/tools/localsetup.py generate-docs`, and `ls/tools/docs_alignment.py` only for outputs those owners define.

Never rewrite release records, signed tags, published assets/SBOMs/attestations, exact-SHA CI evidence, dependency snapshots, or installed-target markers in a scheduled refresh. Report their drift with evidence and hand it to the existing owning manual or explicitly authorized lifecycle workflow. Do not repair existing machine targets. See [repository maintenance](../../docs/REPO_MAINTENANCE.md) and [pre-publish safeguards](../ls-workflow-pipeline-pre-publish/SKILL.md) before release-related handoffs.

## Owner map

- Source-tree and package-marker provenance: `ls/core/provenance_source.py` and `ls/core/provenance.py`; refresh through source-backed install, update, or repair, not report-only reads.
- Current generated documentation, facts, asset inventory, and artifact registry: `ls/tools/generate_docs_artifacts.py`, `localsetup generate-docs`, and docs alignment generators.
- Dependency locks, SDK exports, receipts, and source SBOMs: their dependency and package owners; refresh only after an accepted dependency change.
- Exact-commit CI evidence: validation workflows; evidence applies to one repository revision and is never rebound.
- Release records, signed tags, archives, SBOMs, and attestations: authorized release lifecycle; preserve published evidence as immutable.
- Installed-target markers and client package state: the exact target's install/update/repair lifecycle; no fleet-wide or scheduled repair.

## Scheduled handoff

The scheduled GitHub Action runs deterministic generators and emits an exact-source patch artifact when mutable generated documentation drifts. It does not launch agents, write a branch, open a pull request, or sign commits. Retrieve the artifact from the exact run, verify its recorded source commit, patch SHA-256, and changed paths, then use this workflow locally to review and apply it. Commit with the repository's required signer; do not push without explicit authorization. The artifact expires after 30 days.

For each run, scouts report owner, source revision, evidence, mutability, and disposition. The controller scopes one worker per owner, validates each canonical refresh, reviews the aggregate diff, writes the evidence record, and creates a task-scoped signed local commit. Lifecycle-bound drift is a handoff, not a refresh request.
