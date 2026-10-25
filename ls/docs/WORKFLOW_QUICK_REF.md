---
status: ACTIVE
version: 4.84
owner_package: generate-docs
localsetup_provenance:
  schema_version: 1
  source_provenance_hash: e53a31dd493afd810f634160de47e2d42b6275fddd050b8f067cb72fa5c227eb
  emitter: generate-docs
framework_version: 4.84.1
source_commit: a3789a561f93e0d653a6be061f1939018dee2595
artifact_sha256: 26a858f3b63910fb396d5ea73b8739333145821e153c37eb69cbd94a9d01e302
---
# Workflow quick reference

This page is generated from `ls/workflows/*/workflow.yaml`.

## Workflows

| Workflow ID | Name | Aliases | Package | Required skills |
|------------|------|---------|---------|-----------------|
| `codex-github-issue-goal-loop` | Codex GitHub Issue Goal Loop | codex github issue goal loop; github issue goal loop; slash goal issue sweep; github maintenance goal; ls-workflow-codex-github-issue-goal-loop | `ls-wf-codex-github-issue-goal-loop` | `ls-localsetup-framework-changes`; `ls-git-merge-rebase`; `ls-system-changes-backup`; `ls-docs-create-move`; `ls-docs-source-drift`; `ls-write-run-tests`; `ls-github-pr-review`; `ls-github-release-publishing`; `ls-version-bump-commits` |
| `contribution-review-submit` | Contribution, Review, and Submission | contribution journey; resume contribution | `ls-wf-contribution-review-submit` | `ls-git-merge-rebase`; `ls-requesting-code-review`; `ls-receiving-code-review`; `ls-github-release-publishing` |
| `git-repair-hygiene` | Git Repair Hygiene | git repair pipeline; ls-workflow-pipeline-git-repair-hygiene; pipeline-git-repair-hygiene | `ls-wf-git-repair-hygiene` | `ls-git-broken-state-recovery`; `ls-git-merge-rebase`; `ls-localsetup-framework-changes` |
| `github-repository-settings-audit` | GitHub Repository Settings Audit | github repository enhancement; audit GitHub repository settings; configure GitHub maintenance; ls-workflow-github-repository-enhancement; github-repository-enhancement | `ls-wf-github-repository-settings-audit` | `ls-github-release-publishing`; `ls-system-changes-backup`; `ls-docs-source-drift`; `ls-docs-create-move`; `ls-write-run-tests`; `ls-localsetup-framework-changes`; `ls-git-merge-rebase`; `ls-version-bump-commits` |
| `lscli-compact-worker` | LSCli Compact Worker Qualification | lscli compact worker; qualify local compact worker; ls-workflow-lscli-compact-worker | `ls-wf-lscli-compact-worker` | `ls-agent-capability-routing`; `ls-task-skill-matcher` |
| `multi-phase-task-execution` | Multi-Phase Task Execution | umbrella workflow; ls-workflow-umbrella-run; umbrella-run | `ls-wf-multi-phase-task-execution` | n/a |
| `openpgp-key-lifecycle` | OpenPGP Key Lifecycle | manage OpenPGP keys; recover OpenPGP authority; share encrypted artifacts; share private provenance; verify encrypted file; ls-workflow-openpgp-lifecycle; openpgp-lifecycle | `ls-wf-openpgp-key-lifecycle` | n/a |
| `planning-critic-loop` | Planning Critic Loop | planning critic loop; planning agent critic; critic reviewed plan; ls-workflow-planning-critic-loop | `ls-wf-planning-critic-loop` | n/a |
| `pr-review-feedback-fixes` | Pull Request Review Feedback Fixes | pr feedback pipeline; ls-workflow-pipeline-pr-feedback-loop; pipeline-pr-feedback-loop | `ls-wf-pr-review-feedback-fixes` | `ls-receiving-code-review`; `ls-test-driven-development`; `ls-github-pr-review` |
| `provenance-drift-check-refresh` | Provenance Drift Check and Refresh | provenance refresh pipeline; ls-workflow-provenance-refresh; provenance-refresh | `ls-wf-provenance-drift-check-refresh` | `ls-version-bump-commits`; `ls-docs-source-drift`; `ls-localsetup-framework-changes`; `ls-git-merge-rebase`; `ls-github-actions-builder`; `ls-github-release-publishing`; `ls-system-changes-backup`; `ls-write-run-tests` |
| `queue-batch-implement` | Queue Batch Implement | Agent Q queue; process PRDs; ls-workflow-queue-batch-implement | `ls-wf-queue-batch-implement` | n/a |
| `release-publish-readiness` | Release Publish Readiness | pre publish pipeline; ls-workflow-pipeline-pre-publish; pipeline-pre-publish | `ls-wf-release-publish-readiness` | `ls-github-release-publishing`; `ls-version-bump-commits`; `ls-localsetup-framework-audit` |
| `repository-docs-release-preparation` | Repository Documentation Release Preparation | repo polish pipeline; ls-workflow-pipeline-repo-polish; pipeline-repo-polish | `ls-wf-repository-docs-release-preparation` | `ls-script-docs-authoring`; `ls-humanize-ai-writing`; `ls-github-release-publishing` |
| `repository-localsetup-conversion` | Repository LocalSetup Conversion | repo convert pipeline; convert repo; localsetup convert; ls-workflow-pipeline-repo-convert; pipeline-repo-convert | `ls-wf-repository-localsetup-conversion` | `ls-localsetup-framework-changes`; `ls-system-changes-backup`; `ls-git-merge-rebase`; `ls-write-run-tests` |
| `repository-status-checkpoint` | Repository Status Checkpoint | repo finalizer; finalizer harness; finalization checkpoint; ls-workflow-repo-finalizer; repo-finalizer | `ls-wf-repository-status-checkpoint` | `ls-localsetup-framework-changes`; `ls-git-merge-rebase` |
| `risky-operations-safety-approval` | Risky Operations Safety Approval | lazy admin; manual execution; ls-workflow-ops-guarded; ops-guarded | `ls-wf-risky-operations-safety-approval` | `ls-localsetup-framework-changes`; `ls-system-changes-backup` |
| `server-triage-patch-planning` | Server Triage Patch Planning | server triage patch pipeline; ls-workflow-pipeline-server-triage-patch; pipeline-server-triage-patch | `ls-wf-server-triage-patch-planning` | `ls-system-baseline-diagnostics`; `ls-linux-service-triage`; `ls-linux-patch-planning` |
| `skill-import-normalize-test` | Skill Import, Normalize, and Test | skill onboarding pipeline; ls-workflow-pipeline-skill-onboard; pipeline-skill-onboard | `ls-wf-skill-import-normalize-test` | `ls-skill-security-vetting`; `ls-import-external-skills`; `ls-normalize-skill-format`; `ls-skill-sandbox-tester` |
| `spec-clarify-reverse` | Spec Clarify Reverse | decision tree; reverse prompt; ls-workflow-spec-clarify-reverse | `ls-wf-spec-clarify-reverse` | n/a |
| `sudo-tmux-session` | Sudo Managed Pane | tmux shared session; sudo tmux; elevated permissions; interactive sudo prompt; sudo password prompt handoff; require_escalated; pseudo-terminal ops; managed tmux ops; ls-workflow-ops-tmux-session; ops-tmux-session | `ls-wf-sudo-tmux-session` | `ls-system-changes-backup` |
| `tmux-terminal-mode` | Observable Shells and Tmux Terminal Mode | tmux terminal mode; always-on tmux; observable agent shells; tmux command recovery; ls-workflow-tmux-terminal-mode | `ls-wf-tmux-terminal-mode` | n/a |

## Common Phrases

- "codex github issue goal loop" -> `codex-github-issue-goal-loop`
- "github issue goal loop" -> `codex-github-issue-goal-loop`
- "slash goal issue sweep" -> `codex-github-issue-goal-loop`
- "github maintenance goal" -> `codex-github-issue-goal-loop`
- "ls-workflow-codex-github-issue-goal-loop" -> `codex-github-issue-goal-loop`
- "contribution journey" -> `contribution-review-submit`
- "resume contribution" -> `contribution-review-submit`
- "git repair pipeline" -> `git-repair-hygiene`
- "ls-workflow-pipeline-git-repair-hygiene" -> `git-repair-hygiene`
- "pipeline-git-repair-hygiene" -> `git-repair-hygiene`
- "github repository enhancement" -> `github-repository-settings-audit`
- "audit GitHub repository settings" -> `github-repository-settings-audit`
- "configure GitHub maintenance" -> `github-repository-settings-audit`
- "ls-workflow-github-repository-enhancement" -> `github-repository-settings-audit`
- "github-repository-enhancement" -> `github-repository-settings-audit`
- "lscli compact worker" -> `lscli-compact-worker`
- "qualify local compact worker" -> `lscli-compact-worker`
- "ls-workflow-lscli-compact-worker" -> `lscli-compact-worker`
- "umbrella workflow" -> `multi-phase-task-execution`
- "ls-workflow-umbrella-run" -> `multi-phase-task-execution`
- "umbrella-run" -> `multi-phase-task-execution`
- "manage OpenPGP keys" -> `openpgp-key-lifecycle`
- "recover OpenPGP authority" -> `openpgp-key-lifecycle`
- "share encrypted artifacts" -> `openpgp-key-lifecycle`
- "share private provenance" -> `openpgp-key-lifecycle`
- "verify encrypted file" -> `openpgp-key-lifecycle`
- "ls-workflow-openpgp-lifecycle" -> `openpgp-key-lifecycle`
- "openpgp-lifecycle" -> `openpgp-key-lifecycle`
- "planning critic loop" -> `planning-critic-loop`
- "planning agent critic" -> `planning-critic-loop`
- "critic reviewed plan" -> `planning-critic-loop`
- "ls-workflow-planning-critic-loop" -> `planning-critic-loop`
- "pr feedback pipeline" -> `pr-review-feedback-fixes`
- "ls-workflow-pipeline-pr-feedback-loop" -> `pr-review-feedback-fixes`
- "pipeline-pr-feedback-loop" -> `pr-review-feedback-fixes`
- "provenance refresh pipeline" -> `provenance-drift-check-refresh`
- "ls-workflow-provenance-refresh" -> `provenance-drift-check-refresh`
- "provenance-refresh" -> `provenance-drift-check-refresh`
- "Agent Q queue" -> `queue-batch-implement`
- "process PRDs" -> `queue-batch-implement`
- "ls-workflow-queue-batch-implement" -> `queue-batch-implement`
- "pre publish pipeline" -> `release-publish-readiness`
- "ls-workflow-pipeline-pre-publish" -> `release-publish-readiness`
- "pipeline-pre-publish" -> `release-publish-readiness`
- "repo polish pipeline" -> `repository-docs-release-preparation`
- "ls-workflow-pipeline-repo-polish" -> `repository-docs-release-preparation`
- "pipeline-repo-polish" -> `repository-docs-release-preparation`
- "repo convert pipeline" -> `repository-localsetup-conversion`
- "convert repo" -> `repository-localsetup-conversion`
- "localsetup convert" -> `repository-localsetup-conversion`
- "ls-workflow-pipeline-repo-convert" -> `repository-localsetup-conversion`
- "pipeline-repo-convert" -> `repository-localsetup-conversion`
- "repo finalizer" -> `repository-status-checkpoint`
- "finalizer harness" -> `repository-status-checkpoint`
- "finalization checkpoint" -> `repository-status-checkpoint`
- "ls-workflow-repo-finalizer" -> `repository-status-checkpoint`
- "repo-finalizer" -> `repository-status-checkpoint`
- "lazy admin" -> `risky-operations-safety-approval`
- "manual execution" -> `risky-operations-safety-approval`
- "ls-workflow-ops-guarded" -> `risky-operations-safety-approval`
- "ops-guarded" -> `risky-operations-safety-approval`
- "server triage patch pipeline" -> `server-triage-patch-planning`
- "ls-workflow-pipeline-server-triage-patch" -> `server-triage-patch-planning`
- "pipeline-server-triage-patch" -> `server-triage-patch-planning`
- "skill onboarding pipeline" -> `skill-import-normalize-test`
- "ls-workflow-pipeline-skill-onboard" -> `skill-import-normalize-test`
- "pipeline-skill-onboard" -> `skill-import-normalize-test`
- "decision tree" -> `spec-clarify-reverse`
- "reverse prompt" -> `spec-clarify-reverse`
- "ls-workflow-spec-clarify-reverse" -> `spec-clarify-reverse`
- "tmux shared session" -> `sudo-tmux-session`
- "sudo tmux" -> `sudo-tmux-session`
- "elevated permissions" -> `sudo-tmux-session`
- "interactive sudo prompt" -> `sudo-tmux-session`
- "sudo password prompt handoff" -> `sudo-tmux-session`
- "require_escalated" -> `sudo-tmux-session`
- "pseudo-terminal ops" -> `sudo-tmux-session`
- "managed tmux ops" -> `sudo-tmux-session`
- "ls-workflow-ops-tmux-session" -> `sudo-tmux-session`
- "ops-tmux-session" -> `sudo-tmux-session`
- "tmux terminal mode" -> `tmux-terminal-mode`
- "always-on tmux" -> `tmux-terminal-mode`
- "observable agent shells" -> `tmux-terminal-mode`
- "tmux command recovery" -> `tmux-terminal-mode`
- "ls-workflow-tmux-terminal-mode" -> `tmux-terminal-mode`
