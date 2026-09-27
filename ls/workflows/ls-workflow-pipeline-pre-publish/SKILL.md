---
name: ls-workflow-pipeline-pre-publish
description: Use when running pre-publish checks, version sync, and framework audit before release actions.
metadata:
  version: "1.0"
---

Use this pipeline package to prepare a repo for publishing.
Follow [ls-github-publishing-workflow](../../skills/ls-github-publishing-workflow/SKILL.md)
for publishing readiness, `ls-automatic-versioning` and
[VERSIONING.md](../../docs/VERSIONING.md) for version consistency, then
[ls-framework-audit](../../skills/ls-framework-audit/SKILL.md) for audit checks.
These owners define the procedures. This pipeline prepares readiness evidence;
release actions remain with the publishing skill and its authorization gates.

For a GitHub-hosted release target, complete the required read-only remote
settings audit with [GitHub repository enhancement](../ls-workflow-github-repository-enhancement/SKILL.md)
before reporting publish readiness. If reviewed explicit repository policy
calls for changes, create its plan with `--policy POLICY.json` and review it
separately; apply only the exact authorized plan digest and operation IDs, then
verify the result with `--plan PLAN.json`. Destructive or access-changing
operations are excluded from the ordinary plan and need a separate policy,
reviewed plan, digest, and exact operation authorization. Unsupported controls
remain report-only. A publish
preflight, repository audit, or release authorization does not authorize remote
settings writes. Use `--repository OWNER/REPO` plus `--hostname HOST` for the
GitHub target; global `--repo` continues to select LocalSetup's source
checkout. Keep release tags, assets, and tracked repository changes on the
existing local signed Git and release workflows.

Remote write operations are enabled only on `github.com` while the GitHub
Enterprise Server API-version matrix remains unverified. Audits and reads may
run on other GitHub hosts; requested drift there remains incomplete and
report-only with a compatibility reason. Inventory caveats and ambient token
authorization-visibility findings do not make the requested policy incomplete;
unmet requested-policy values or handoffs do.
