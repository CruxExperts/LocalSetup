---
name: ls-workflow-pipeline-repo-polish
description: Use when polishing repository docs and scripts for sharing readiness.
metadata:
  version: "1.0"
---

Use this pipeline package for presentation and quality improvements before sharing.
Follow [ls-script-and-docs-quality](../../skills/ls-script-and-docs-quality/SKILL.md)
for the quality pass, [ls-humanizer](../../skills/ls-humanizer/SKILL.md) for
readability, and
[ls-github-publishing-workflow](../../skills/ls-github-publishing-workflow/SKILL.md)
for publishing and README policies and shareability checks. Use the
[framework docs index](../../docs/README.md) to locate the relevant public docs.
These skills own the procedures. This pipeline ends with sharing-readiness
checks; publishing actions remain with the publishing skill and its gates.

For a GitHub-hosted target, the remote-settings review is a required phase:
follow [GitHub repository enhancement](../ls-workflow-github-repository-enhancement/SKILL.md)
and run its `github-repo` audit before declaring the repository share-ready.
Build a plan with `--policy POLICY.json` only from explicit desired policy,
review its digest and operation IDs, and apply only that exact authorized
subset; then run verification with `--plan PLAN.json`. The ordinary plan
excludes destructive or access-changing operations; each such change needs a
separate policy, reviewed plan, digest, and exact operation authorization.
Controls without supported typed operations remain report-only. An audit does
not authorize a write. If the target is not GitHub-hosted, record
that this phase is not applicable and continue with the local sharing checks.
The enhancement workflow uses `--repository OWNER/REPO` and `--hostname HOST`
for the remote identity; global `--repo` keeps its LocalSetup source-checkout
meaning.

Remote write operations are enabled only on `github.com` while the GitHub
Enterprise Server API-version matrix remains unverified. Audits and reads may
run on other GitHub hosts; requested drift there remains incomplete and
report-only with a compatibility reason. Inventory caveats and ambient token
authorization-visibility findings do not make the requested policy incomplete;
unmet requested-policy values or handoffs do.
