# GitHub maintenance adoption recipe

This recipe packages a small, opt-in maintenance profile for a GitHub repository. It reuses the target's existing CI, review, issue, security, documentation, signing, and release owners. It is an agent-executed tracked-change plan, not an installer: selecting the `github-maintenance` pack only makes this workflow and its templates available in the managed package library. It does not copy files into a target repository, create a workflow, authenticate to GitHub, change repository or account settings, or activate enforcement.

Use the repository's existing maintenance workflow and review procedure. First inspect the target and its existing owners; do not replace a file or policy merely because a template is available. A non-GitHub target is inapplicable and must not be modified. The target's stronger controls remain authoritative.

## 1. Discover the target and preserve its owners

Read the target's root instructions and relevant CI, review, issue-form, security, hook, version, and publication guidance. Determine locally where possible, then verify the GitHub-hosted facts needed for any remote operation:

- GitHub host, immutable repository ID, owner/name, and current default branch.
- Existing validation workflow, its install/runtime/test commands, successful check names and GitHub application IDs, plus how the required aggregate gates depend on prerequisite jobs.
- Existing review contract and instruction paths; issue labels, bug form, and private security reporting route.
- The target's signing, protected-branch/ruleset, integration, version, and release rules.
- Copilot plan and organization entitlement, current credit/request billing mode and allowance, who pays for reviews (including external contributors), additional-usage controls, and inherited review settings.
- Current CodeQL setup, supported languages, and existing security features/entitlements.

Do not infer an absent setting, permission, entitlement, bill payer, successful status check, or private reporting route from a local file. Missing test commands or a private security route block only the dependent change, not unrelated work. Resolve `__REPOSITORY__`, `__REVIEW_CONTRACT__`, and `__SECURITY_REPORT_URL__` only when used, then remove every unresolved placeholder from adapted tracked files before committing. Never put personal subscriptions, credentials, signing keys, local machine paths, or repository-specific fingerprints into reusable templates.

For a repeat adoption, compare target content and effective settings with the accepted profile first. Make no change where they already match. If an existing owner differs, merge only the requested semantics; preserve custom content, labels, allowed-action rules, stronger policies, and unrelated settings. Report a specific owner conflict or decision rather than overwriting it.

## 2. Adapt the tracked templates

The four canonical files are in `assets/github-maintenance/` and their source paths, adoption roles, and ownership notice are declared in this workflow's `workflow.yaml` extension metadata. The workflow package's normal LocalSetup managed marker records package provenance; adapted target files use ordinary Git provenance.

### Repository policy

Copy `repository-policy.json` only to a private, reviewed policy input. Edit it structurally as JSON and validate it with the existing schema-v2 `normalize_policy` owner before planning. Preserve existing action allowlists and inherited policy; do not add an `allowed_actions` policy unless target evidence and the approved change require it. Keep the starter's read-only Actions permissions, disabled workflow-review approval, SHA pinning, five security requests, and `LocalSetup maintenance` default-branch ruleset profile as written.

Before any policy plan can be applied, discover the target's successful required status contexts and their application IDs, then add a non-empty `required_status_checks` list and `strict_required_status_checks: false` to the ruleset. The starter deliberately omits both fields because the existing policy normalizer rejects a strictness value without a non-empty check list. Until real bindings have been discovered and added, this starter is **not activation-ready** and must not be applied as a complete maintenance policy. Do not invent check names or reuse another repository's IDs.

Retain the template's branch target, active enforcement, `~DEFAULT_BRANCH` include, empty exclude, signed commits, deletion block, and force-push block. Do not add bypass actors, visibility or branch replacement, merge-method changes, review-count/code-owner requirements, merge queue, automatic merge, branch deletion, release/tag controls, or other unrequested restrictions. If a requested security feature is unavailable on the target's plan, report the entitlement gap first and omit only that unsupported request from the adapted policy; the affected protection remains unmet, not complete. Do not buy an entitlement or imply equivalent protection.

### Copilot instructions

Merge `copilot-instructions.md` into the target's existing advisory instruction owner or create `.github/copilot-instructions.md` only after inspection. If a substantive review contract exists, resolve the placeholder to its actual path. If none exists, remove the placeholder and keep the short actionable-diff standard in the instruction file itself. Keep the changed-line regression standard and safety boundary at the start. Instructions are advisory; GitHub permissions, deterministic checks, and rulesets enforce policy.

### Bug report

Merge `bug-report.yml` into the existing bug form while preserving field IDs and labels that downstream triage depends on, or create `.github/ISSUE_TEMPLATE/bug_report.yml` when no form exists. Adapt the client/platform prompt as required, free text with useful examples and `not platform-specific`, never a closed product catalog. Keep steps-to-reproduce required and accept commands or UI actions. Provide portable problem, expected, actual, version, and optional additional-context fields without adding more required fields to an existing form. Resolve the private reporting URL, retain the redaction warning, and adapt to the target's existing label catalog. If no safe private security route is available, do not imply that a public issue is appropriate for vulnerabilities; block the dependent public form change until the route is confirmed.

### Dependency review fragment

`dependency-review-job.yml` is a job fragment for the target's existing CI owner, not a second all-purpose workflow. Before adding it, recheck the exact action release, tag-to-commit binding, action contract, and dependency-security advisory evidence under the target's dependency-review policy. If source/release/advisory evidence is incomplete or blocked, pause this action addition and report the evidence gap; do not substitute another action or treat the check as passed.

Inspect the existing allowed-actions policy before adoption. Preserve it; if it excludes the exact reviewed pin, surface the required allowlist addition as an explicit decision and do not broaden the policy automatically.

Reuse the existing workflow's dependency, prerequisite, and aggregate jobs. Run the fragment on pull requests and merge groups; pass explicit base/head SHAs for each event as shown. Add its result to the cheap prerequisite and required aggregate dependencies, with explicit event-sensitive result handling: require `success` for pull requests and merge groups, and exactly `skipped` for push/manual events where the job does not run. Reject unknown events or missing/failed/cancelled results. A failed dependency check must stop costly test shards; a previously successful full-suite result cannot make a failed or missing dependency result pass. Do not turn this conditional job into a universal required branch check or release-evidence job.

If no CI owner exists, discover real install/test commands and runtime first. When all are known, create one `.github/workflows/validation.yml` with pull-request and default-branch-push triggers, read-only contents, a 30-minute timeout, and a named `validation` job bound to those commands/runtime. Do not invent a passing test command. Integrate dependency review into that same owner only after its evidence gate and event/result wiring can be maintained safely; never create a parallel full test suite.

## 3. Configure bounded review and security assistance

Native GitHub Copilot review is optional advisory help, never a replacement for the existing repository agents or deterministic validation. Do not add a custom review Action, new MCP integration, repository secret, CLI hook, or cloud coding assignment. Preserve existing fork/no-secret behavior, deterministic review artifacts, and existing independent/human review sources. Do not weaken inherited review requirements. Do not assign issues, review comments, or security alerts to a cloud coding agent or invoke "Fix with Copilot." Keep CodeQL Autofix suggestions available; the target's existing repository agents review and implement accepted suggestions. Do not enable a separate paid model provider alongside Copilot.

Only after actual plan, billing, payer, allowance, and inherited settings are verified, consider repository-scoped automatic review for pull requests targeting the default branch. Use Lite effort, leave draft-pull-request and new-push reviews off, and keep Copilot approval and counting AI approvals toward merge requirements off. A deliberate Balanced review is reserved for a material security, installer, or release-boundary change that needs additional independent analysis; do not routinely run both effort levels. Reviews consume the account's current AI credit/request allowance and Actions minutes; do not claim they are free. Do not change account-wide settings, billing, budgets, or additional-usage controls. If the available allowance cannot keep the profile within the authorized no-additional-paid-usage boundary, leave automatic review inactive and use only owner-requested review when entitlement is confirmed. GitHub selects the review model; do not invent a model control. A parent setting that forces draft or push reviews is a conflict to report, not a setting to bypass. An outage or exhausted allowance is advisory and cannot disable deterministic checks. For an explicitly requested, sanitized issue or failed-run summary only, prefer GPT-6 Luna with low reasoning if the actual account picker offers it; verify the selection there, and otherwise return the work to existing repository agents without silently choosing a more expensive model. Do not schedule AI digests or automatic model escalation.

If CodeQL is not already configured, use GitHub's default setup with detected supported languages, the default query suite, and standard GitHub-hosted runners. Select the UI's normalized language list rather than duplicating aliases. Preserve an existing advanced setup; do not add a second scanner workflow. Configured status alone is not evidence of a completed successful analysis. Keep CodeQL Autofix suggestions available; route alerts to the target's existing security owner and let repository agents review and implement accepted suggestions. Do not auto-dismiss alerts or expose sensitive finding details.

Keep available secret scanning, push protection, Dependabot security updates, and private vulnerability reporting enabled. Preserve existing Dependabot grouping, caps, and cadence. Dependency updates still pass through the target's signature, security, and validation owners; do not add automatic merge, stale-item closure, or alert dismissal. Do not put sensitive vulnerability details into public issues or review automation.

## 4. Map hooks, validation, and integration boundaries

Map each existing local hook to its current hosted check owner rather than duplicating it. Inspect hook behavior: a hook that stages generated files, changes versions, commits, or performs a remote write is not a read-only validator and must not run inside a Copilot session. Instructions advise agents; GitHub permissions, required checks, and rulesets enforce boundaries.

Finish source and version preparation before requesting final hosted validation. A hook-generated source/version change creates a new candidate SHA and invalidates earlier evidence for that candidate. Reuse successful exact-commit evidence only while its inputs remain applicable; do not run another full suite solely to repeat unchanged evidence. Do not put AI calls, network reviews, or full suites into per-tool or commit hooks.

For repository settings, use the existing `localsetup github-repo` audit/plan/apply/verify owner. Build the plan only after the local source checkpoint is stable. Review its target binding, operation digest, preconditions, and exact operation IDs; apply only the separately authorized operations and read them back. Keep UI-only controls as explicit handoffs and verify them with fresh evidence. If a mutation response is lost, reconcile by reads and never replay blindly. The maintenance ruleset is separate from existing rulesets and only uses successful, target-specific check bindings; do not replace unrelated or release-tag rulesets. Keep unmodeled native Copilot review settings in a separate dedicated Copilot review ruleset, not the typed maintenance ruleset, so later typed updates cannot erase them. For the LocalSetup pilot this is the separately named `LocalSetup Copilot review` ruleset. Inspect inherited organization rules and other independent review sources before enabling Copilot; preserve them and report conflicts rather than weakening them.

For integration, follow the target's signed publication workflow. Review the exact source payload and obtain authority for the chosen publication action and exact settings payload. Required status checks protect ref updates, not a source commit before it is pushed somewhere for validation. Finish and sign the candidate, validate that exact commit through its PR or manual workflow on another ref, verify its signature identity with the target's local signer policy separately from GitHub-recognized-signature enforcement, then fast-forward the protected target to the same SHA. This is not a separate push ruleset. If a merge, squash, rebase, or other integration creates a different commit, sign, validate, and verify that new candidate before the ref update. Do not infer publication or activation authority from this recipe or from package selection.

## 5. Roll out before enforcement

For a first activation, complete the tracked source changes and their normal local review/validation through the existing owners before requesting hosted validation. Use the existing publication owner to review the exact source payload and obtain explicit authority to publish/open a draft pilot pull request. Group the concrete Copilot, CodeQL, and repository-setting changes into one exact scope review; reuse that authority for the steps it names rather than asking again for unchanged operations.

1. After the source candidate is stable, enable only the separately authorized CodeQL default setup and repository-scoped Copilot review settings. Keep maintenance ruleset enforcement inactive during this pilot. Do not change account-wide review, billing, budget, or additional-usage settings.
2. Mark the authorized pilot pull request ready only after the proposed changes are ready for review. Observe its deterministic validation, dependency review, and advisory Copilot review. Copilot should produce a review, never an approval; verify actual allowance use and that no additional paid usage was enabled.
3. If a substantial accepted review finding changes the candidate, have the existing repository agents implement it, prepare and sign the new candidate, then validate that exact SHA. Reuse successful evidence only when its tested inputs still apply.
4. After the successful check names and application IDs are observed, build and review the exact maintenance-ruleset settings plan with `localsetup github-repo`, apply only its authorized operation, and verify the read-back. Verify UI-only Copilot and CodeQL settings separately with fresh evidence. If the account UI or entitlement cannot be verified, leave the affected setting inactive/pending rather than substitute another provider or bypass inherited policy.
5. Integrate only the exact signed candidate whose required checks and signature identity were verified. Follow the target's actual integration rules; if integration creates a new commit, obtain evidence for that new commit before the ref update. After integration, inspect the actual default-branch issue form and confirm a completed successful CodeQL analysis; a PR-only form or configured-but-not-run scanner is not proof of live behavior.

Do not submit a throwaway public issue, expose security findings to public automation, or perform an unauthorized rejected-push test.


## 6. Record provenance and verify repeat adoption

Before committing adapted tracked files, confirm every placeholder is resolved, each file was merged with the existing owner, and the target's normal checks and signed integration procedure were followed. In the adoption commit body, record the source template paths and the accepted LocalSetup source commit recorded by the installed package provenance. Keep private operational details in the target's existing private task-state location, not in generic assets or a new ownership database.

After adoption, read back the changed files and any separately authorized remote settings. Compare the same inputs on a repeat run; an aligned repository should produce no content or settings change. Record unsupported entitlements, unresolved status-check bindings, blocked advisory evidence, inherited Copilot conflicts, missing private reporting routes, and unavailable UI evidence separately. Do not describe the profile as activated while any required check binding, policy entitlement, signed candidate proof, setting read-back, or authorized handoff remains unresolved.

## 7. Run factual freshness checks in the existing cloud patrol

Reuse the target's existing GitHub-hosted maintenance schedule and canonical
catalog, source-provenance, and generated-output validators. Do not create a
second scheduler or depend on an interactive workstation session. Inventory
skills, workflows, their references/assets, and other tracked provenance inputs;
compare to the last successful patrol baseline even when its source SHA is
unchanged. Missing baseline or unavailable upstream evidence must be reported
explicitly, not interpreted as fresh.

Use deterministic checks to traverse files and compare provenance, with no
automatic fixes or dependency upgrades. For optional model-assisted factual
classification, reuse the existing cloud QC provider only after its concrete
low-cost model, supported low reasoning, credentials, runtime, and allowance are
verified. Bind a separate patrol model explicitly; do not inherit an opaque or
premium review route. Send only new, bounded evidence packets, cap calls and
input/output tokens, and permit no retries or automatic model escalation.
Missing model or allowance evidence leaves model activation pending without
disabling deterministic checks. A changed source hash is only
change evidence, not proof that content is outdated. Use the existing provenance
owner's expected-versus-observed comparison to identify an actual stale record.
Local structural and provenance checks do not establish that every external
claim is current: run upstream checks only where an authoritative source and
refresh contract exist, and state uncovered sources rather than guessing.

For a confirmed stale-provenance defect, reuse the existing duplicate-aware
issue owner. Publish only basic factual information: affected file, failed
check, expected provenance and observed provenance. Omit AI explanations,
speculative recommendations, implementation plans, credentials, private paths,
and sensitive finding details. Reuse the existing issue for the same defect;
unchanged evidence should not produce another issue or repeated comment.
Publication and recurring operation still require the target's existing
authority; selecting this pack does not activate a cloud schedule or authorize
issues.

Copilot Lite reviews a resulting repair PR through the normal advisory profile,
once entitlement and allowance are verified. Native review is PR-triggered,
not a scheduled audit, and GitHub selects its model. The scheduled factual
patrol's existing QC provider is a separate explicitly budgeted route, not a
claim that native Copilot can run a periodic repository audit.

## Current GitHub documentation to recheck

GitHub's settings and billing surfaces change over time. Recheck the target's current behavior and entitlement before any activation:

- [Configure Copilot code review](https://docs.github.com/en/copilot/how-tos/copilot-on-github/set-up-copilot/configure-code-review) for repository/account review scope, effort, draft/push controls, and approval settings.
- [Use Copilot code review](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/copilot-code-review) for review operation and pull-request instructions.
- [Copilot models and pricing](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing) for current AI credit/request attribution and review-model selection limits.
- [Individual Copilot billing and additional usage](https://docs.github.com/en/copilot/concepts/billing-and-usage/individuals/billing) for current allowance and additional-usage controls.
- [CodeQL default setup](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configure-code-scanning) and [Copilot Autofix](https://docs.github.com/en/code-security/concepts/code-scanning/autofix-for-code-scanning) for scanner setup and suggestion boundaries.
- [Dependency Review configuration](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/manage-your-dependency-security/configure-dependency-review-action) and the pinned action's release, source, contract, and advisory evidence before adoption.
- [Repository rulesets](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset) for required-check ref-update semantics and the current typed write surface.
