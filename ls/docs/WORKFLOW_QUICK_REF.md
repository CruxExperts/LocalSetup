---
status: ACTIVE
version: 4.44
owner_package: generate-docs
localsetup_provenance:
  schema_version: 1
  source_provenance_hash: d772a3cbe049ca55d4bdb809c8f6982b9250f0b64c5db8dce2f9a80def1ee5fe
  emitter: generate-docs
framework_version: 4.44.3
source_commit: 213176b18d5683d0354d6de307692589e2879185
artifact_sha256: 393552d58b8412c44afc9fbbd0aff0975a19010d587d5779e0346ef5ff2e05a7
---
# Workflow quick reference

This page is generated from `ls/workflows/*/workflow.yaml`.

## Workflows

| Workflow ID | Name | Aliases | Package | Required skills |
|------------|------|---------|---------|-----------------|
| `codex-github-issue-goal-loop` | Codex GitHub Issue Goal Loop | codex github issue goal loop; github issue goal loop; slash goal issue sweep; github maintenance goal | `ls-workflow-codex-github-issue-goal-loop` | `ls-framework-compliance`; `ls-git-workflows`; `ls-safety-and-backup`; `ls-test-runner`; `ls-tdd-guide`; `ls-receiving-code-review`; `ls-pr-reviewer`; `ls-github-publishing-workflow`; `ls-automatic-versioning`; `ls-framework-audit` |
| `lscli-compact-worker` | LSCli Compact Worker Qualification | lscli compact worker; qualify local compact worker | `ls-workflow-lscli-compact-worker` | `ls-agent-routing`; `ls-task-skill-matcher` |
| `openpgp-lifecycle` | OpenPGP Key Lifecycle | manage OpenPGP keys; recover OpenPGP authority | `ls-workflow-openpgp-lifecycle` | n/a |
| `ops-guarded` | Ops Guarded | lazy admin; manual execution | `ls-workflow-ops-guarded` | `ls-framework-compliance`; `ls-safety-and-backup` |
| `ops-tmux-session` | Ops Tmux Session | tmux shared session; sudo tmux; elevated permissions; interactive sudo prompt; sudo password prompt handoff; require_escalated; pseudo-terminal ops; managed tmux ops | `ls-workflow-ops-tmux-session` | `ls-safety-and-backup` |
| `pipeline-git-repair-hygiene` | Pipeline Git Repair Hygiene | git repair pipeline | `ls-workflow-pipeline-git-repair-hygiene` | `ls-unfuck-my-git-state`; `ls-git-workflows`; `ls-framework-compliance` |
| `pipeline-pr-feedback-loop` | Pipeline PR Feedback Loop | pr feedback pipeline | `ls-workflow-pipeline-pr-feedback-loop` | `ls-receiving-code-review`; `ls-tdd-guide`; `ls-pr-reviewer` |
| `pipeline-pre-publish` | Pipeline Pre Publish | pre publish pipeline | `ls-workflow-pipeline-pre-publish` | `ls-github-publishing-workflow`; `ls-automatic-versioning`; `ls-framework-audit` |
| `pipeline-repo-convert` | Pipeline Repo Convert | repo convert pipeline; convert repo; localsetup convert | `ls-workflow-pipeline-repo-convert` | `ls-framework-compliance`; `ls-safety-and-backup`; `ls-git-workflows`; `ls-test-runner` |
| `pipeline-repo-polish` | Pipeline Repo Polish | repo polish pipeline | `ls-workflow-pipeline-repo-polish` | `ls-script-and-docs-quality`; `ls-humanizer`; `ls-github-publishing-workflow` |
| `pipeline-server-triage-patch` | Pipeline Server Triage Patch | server triage patch pipeline | `ls-workflow-pipeline-server-triage-patch` | `ls-system-info`; `ls-linux-service-triage`; `ls-linux-patcher` |
| `pipeline-skill-onboard` | Pipeline Skill Onboard | skill onboarding pipeline | `ls-workflow-pipeline-skill-onboard` | `ls-skill-vetter`; `ls-skill-importer`; `ls-skill-normalizer`; `ls-skill-sandbox-tester` |
| `planning-critic-loop` | Planning Critic Loop | planning critic loop; planning agent critic; critic reviewed plan | `ls-workflow-planning-critic-loop` | n/a |
| `queue-batch-implement` | Queue Batch Implement | Agent Q queue; process PRDs | `ls-workflow-queue-batch-implement` | n/a |
| `repo-finalizer` | Repo Finalizer | repo finalizer; finalizer harness; finalization checkpoint | `ls-workflow-repo-finalizer` | `ls-framework-compliance`; `ls-git-workflows` |
| `spec-clarify-reverse` | Reverse Prompt Spec Clarify | decision tree; reverse prompt | `ls-workflow-spec-clarify-reverse` | n/a |
| `tmux-terminal-mode` | Tmux Terminal Mode | tmux terminal mode; always-on tmux | `ls-workflow-tmux-terminal-mode` | n/a |
| `umbrella-run` | Umbrella Run | umbrella workflow | `ls-workflow-umbrella-run` | n/a |

## Common Phrases

- "codex github issue goal loop" -> `codex-github-issue-goal-loop`
- "github issue goal loop" -> `codex-github-issue-goal-loop`
- "slash goal issue sweep" -> `codex-github-issue-goal-loop`
- "github maintenance goal" -> `codex-github-issue-goal-loop`
- "lscli compact worker" -> `lscli-compact-worker`
- "qualify local compact worker" -> `lscli-compact-worker`
- "manage OpenPGP keys" -> `openpgp-lifecycle`
- "recover OpenPGP authority" -> `openpgp-lifecycle`
- "lazy admin" -> `ops-guarded`
- "manual execution" -> `ops-guarded`
- "tmux shared session" -> `ops-tmux-session`
- "sudo tmux" -> `ops-tmux-session`
- "elevated permissions" -> `ops-tmux-session`
- "interactive sudo prompt" -> `ops-tmux-session`
- "sudo password prompt handoff" -> `ops-tmux-session`
- "require_escalated" -> `ops-tmux-session`
- "pseudo-terminal ops" -> `ops-tmux-session`
- "managed tmux ops" -> `ops-tmux-session`
- "git repair pipeline" -> `pipeline-git-repair-hygiene`
- "pr feedback pipeline" -> `pipeline-pr-feedback-loop`
- "pre publish pipeline" -> `pipeline-pre-publish`
- "repo convert pipeline" -> `pipeline-repo-convert`
- "convert repo" -> `pipeline-repo-convert`
- "localsetup convert" -> `pipeline-repo-convert`
- "repo polish pipeline" -> `pipeline-repo-polish`
- "server triage patch pipeline" -> `pipeline-server-triage-patch`
- "skill onboarding pipeline" -> `pipeline-skill-onboard`
- "planning critic loop" -> `planning-critic-loop`
- "planning agent critic" -> `planning-critic-loop`
- "critic reviewed plan" -> `planning-critic-loop`
- "Agent Q queue" -> `queue-batch-implement`
- "process PRDs" -> `queue-batch-implement`
- "repo finalizer" -> `repo-finalizer`
- "finalizer harness" -> `repo-finalizer`
- "finalization checkpoint" -> `repo-finalizer`
- "decision tree" -> `spec-clarify-reverse`
- "reverse prompt" -> `spec-clarify-reverse`
- "tmux terminal mode" -> `tmux-terminal-mode`
- "always-on tmux" -> `tmux-terminal-mode`
- "umbrella workflow" -> `umbrella-run`
