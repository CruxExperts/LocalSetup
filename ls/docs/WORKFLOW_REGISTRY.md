---
status: ACTIVE
version: 4.45
owner_package: generate-docs
localsetup_provenance:
  schema_version: 1
  source_provenance_hash: ab17daa441090b3272d099a07d96f4f22c77cb92eddab1e9d8f9ab6fa1251375
  emitter: generate-docs
framework_version: 4.45.2
source_commit: 8ab721c02fa530045c0268b9af0c077120187da8
artifact_sha256: b28127a43599d5e7eb72413820bdc49380c109876bbaa73cd949cd67253f63e9
---
# Workflow and module registry (LocalSetup)

This page is generated from `ls/workflows/*/workflow.yaml`.

For the framework rules, see [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md).

## Core

| Name | Description | When to use | Impact review |
|------|-------------|-------------|---------------|
| Master rule / context | Always-loaded framework context | Always | No |
| Skills index | List of capability skills and when to use | When discovering which skill to load | No |

## Workflows

| Workflow ID | Package | Name | Description | Aliases | Required skills | Primary docs/tools |
|-------------|---------|------|-------------|---------|-----------------|--------------------|
| `codex-github-issue-goal-loop` | `ls-workflow-codex-github-issue-goal-loop` | Codex GitHub Issue Goal Loop | Use when keeping a Codex goal active across turns to research GitHub issues, PRs, and feature requests, handle each accepted change as a documented local commit, and publish only within the user's scope. | codex github issue goal loop; github issue goal loop; slash goal issue sweep; github maintenance goal | `ls-framework-compliance`; `ls-git-workflows`; `ls-safety-and-backup`; `ls-docs-organization`; `ls-documentation-alignment`; `ls-test-runner`; `ls-pr-reviewer`; `ls-github-publishing-workflow`; `ls-automatic-versioning` | [CODEX_GITHUB_ISSUE_GOAL_LOOP.md](CODEX_GITHUB_ISSUE_GOAL_LOOP.md); [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md); [DOCUMENT_LIFECYCLE_MANAGEMENT.md](DOCUMENT_LIFECYCLE_MANAGEMENT.md); [OUTPUT_AND_DOC_GENERATION.md](OUTPUT_AND_DOC_GENERATION.md); [VERSIONING.md](VERSIONING.md); `git`; `gh` |
| `github-repository-enhancement` | `ls-workflow-github-repository-enhancement` | GitHub Repository Enhancement | Audit, plan, apply, and verify documented GitHub repository settings through LocalSetup's fixed CLI workflow. | github repository enhancement; audit GitHub repository settings | `ls-github-publishing-workflow`; `ls-safety-and-backup`; `ls-documentation-alignment`; `ls-docs-organization`; `ls-test-runner`; `ls-framework-compliance`; `ls-git-workflows`; `ls-automatic-versioning` | [REPO_MAINTENANCE.md](REPO_MAINTENANCE.md); [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md); [VERSIONING.md](VERSIONING.md); [DOCUMENT_LIFECYCLE_MANAGEMENT.md](DOCUMENT_LIFECYCLE_MANAGEMENT.md); [SKILL.md](../../ls/skills/ls-github-publishing-workflow/SKILL.md); [SKILL.md](../../ls/skills/ls-git-workflows/SKILL.md); [SKILL.md](../../ls/skills/ls-safety-and-backup/SKILL.md); [SKILL.md](../../ls/skills/ls-documentation-alignment/SKILL.md); [SKILL.md](../../ls/skills/ls-docs-organization/SKILL.md); [SKILL.md](../../ls/skills/ls-test-runner/SKILL.md); [SKILL.md](../../ls/skills/ls-framework-compliance/SKILL.md); [SKILL.md](../../ls/skills/ls-automatic-versioning/SKILL.md) |
| `lscli-compact-worker` | `ls-workflow-lscli-compact-worker` | LSCli Compact Worker Qualification | Qualify and assign a bounded local compact worker task through existing LSCli profiles and native tool calling, without adding a runner. | lscli compact worker; qualify local compact worker | `ls-agent-routing`; `ls-task-skill-matcher` | [LSCLI.md](LSCLI.md); [LSCLI_RUNTIME.md](LSCLI_RUNTIME.md); [LSCLI_QUALIFICATION.md](LSCLI_QUALIFICATION.md); [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md) |
| `openpgp-lifecycle` | `ls-workflow-openpgp-lifecycle` | OpenPGP Key Lifecycle | Adopt, generate, back up, rotate, revoke, or recover OpenPGP owner and publisher keys using LocalSetup's shared implementation and explicit local trust. | manage OpenPGP keys; recover OpenPGP authority | n/a | [AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md](AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md); [LSCLI_RUNTIME.md](LSCLI_RUNTIME.md); `ls/core/openpgp/__init__.py` |
| `ops-guarded` | `ls-workflow-ops-guarded` | Ops Guarded | Use when risky operations need approval checkpoints, impact review, or guarded execution; hand off sudo, elevated, PTY, or interactive password execution to ls-workflow-ops-tmux-session. | lazy admin; manual execution | `ls-framework-compliance`; `ls-safety-and-backup` | [SKILL.md](../../ls/skills/ls-safety-and-backup/SKILL.md); [SKILL.md](../../ls/workflows/ls-workflow-ops-tmux-session/SKILL.md) |
| `ops-tmux-session` | `ls-workflow-ops-tmux-session` | Ops Tmux Session | Use when commands need sudo, root/admin elevation, require_escalated, pseudo-terminal/PTY handling, interactive sudo or elevated terminal password prompts, or managed tmux run tracking. | tmux shared session; sudo tmux; elevated permissions; interactive sudo prompt; sudo password prompt handoff; require_escalated; pseudo-terminal ops; managed tmux ops | `ls-safety-and-backup` | [tmux-ops-managed.md](ops/tmux-ops-managed.md); [tmux-ops-remote.md](ops/tmux-ops-remote.md); `ls/tools/tmux_ops` |
| `pipeline-git-repair-hygiene` | `ls-workflow-pipeline-git-repair-hygiene` | Pipeline Git Repair Hygiene | Use when recovering broken Git state and enforcing follow-up workflow hygiene checks. | git repair pipeline | `ls-unfuck-my-git-state`; `ls-git-workflows`; `ls-framework-compliance` | [GIT_TRACEABILITY.md](GIT_TRACEABILITY.md) |
| `pipeline-pr-feedback-loop` | `ls-workflow-pipeline-pr-feedback-loop` | Pipeline PR Feedback Loop | Use when turning pull request feedback into fixes, tests, and follow-up review. | pr feedback pipeline | `ls-receiving-code-review`; `ls-tdd-guide`; `ls-pr-reviewer` | n/a |
| `pipeline-pre-publish` | `ls-workflow-pipeline-pre-publish` | Pipeline Pre Publish | Use when running pre-publish checks, version sync, and framework audit before release actions. | pre publish pipeline | `ls-github-publishing-workflow`; `ls-automatic-versioning`; `ls-framework-audit` | [VERSIONING.md](VERSIONING.md); [SKILL.md](../../ls/workflows/ls-workflow-github-repository-enhancement/SKILL.md); [SKILL.md](../../ls/skills/ls-github-publishing-workflow/SKILL.md); [SKILL.md](../../ls/skills/ls-framework-audit/SKILL.md) |
| `pipeline-repo-convert` | `ls-workflow-pipeline-repo-convert` | Pipeline Repo Convert | Use when converting an existing repo to the current LocalSetup framework with backup, blocker, install, and verification gates. | repo convert pipeline; convert repo; localsetup convert | `ls-framework-compliance`; `ls-safety-and-backup`; `ls-git-workflows`; `ls-test-runner` | [REPO_CONVERSION.md](REPO_CONVERSION.md); [MULTI_PLATFORM_INSTALL.md](MULTI_PLATFORM_INSTALL.md); `git` |
| `pipeline-repo-polish` | `ls-workflow-pipeline-repo-polish` | Pipeline Repo Polish | Use when polishing repository docs and scripts for sharing readiness. | repo polish pipeline | `ls-script-and-docs-quality`; `ls-humanizer`; `ls-github-publishing-workflow` | [README.md](README.md); [SKILL.md](../../ls/workflows/ls-workflow-github-repository-enhancement/SKILL.md); [SKILL.md](../../ls/skills/ls-script-and-docs-quality/SKILL.md); [SKILL.md](../../ls/skills/ls-humanizer/SKILL.md); [SKILL.md](../../ls/skills/ls-github-publishing-workflow/SKILL.md) |
| `pipeline-server-triage-patch` | `ls-workflow-pipeline-server-triage-patch` | Pipeline Server Triage Patch | Use when capturing a Linux server baseline, diagnosing service issues from read-only evidence, and producing a patch plan without executing changes. | server triage patch pipeline | `ls-system-info`; `ls-linux-service-triage`; `ls-linux-patcher` | [WORKFLOW_QUICK_REF.md](WORKFLOW_QUICK_REF.md) |
| `pipeline-skill-onboard` | `ls-workflow-pipeline-skill-onboard` | Pipeline Skill Onboard | Use when running the skill onboarding pipeline from vetting through sandbox testing. | skill onboarding pipeline | `ls-skill-vetter`; `ls-skill-importer`; `ls-skill-normalizer`; `ls-skill-sandbox-tester` | [SKILL_IMPORTING.md](SKILL_IMPORTING.md); [SKILL.md](../../ls/skills/ls-skill-vetter/SKILL.md); [SKILL.md](../../ls/skills/ls-skill-importer/SKILL.md); [SKILL.md](../../ls/skills/ls-skill-normalizer/SKILL.md); [SKILL.md](../../ls/skills/ls-skill-sandbox-tester/SKILL.md) |
| `planning-critic-loop` | `ls-workflow-planning-critic-loop` | Planning Critic Loop | Use when creating decision-complete plans through grounding, capped clarification, subagent delegation, and critic iteration. | planning critic loop; planning agent critic; critic reviewed plan | n/a | [DECISION_TREE_WORKFLOW.md](DECISION_TREE_WORKFLOW.md); [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md); [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md); [SKILLS_AND_RULES.md](SKILLS_AND_RULES.md) |
| `queue-batch-implement` | `ls-workflow-queue-batch-implement` | Queue Batch Implement | Use when processing queued PRD tasks in batch with status tracking and outcome reporting. | Agent Q queue; process PRDs | n/a | [AGENTIC_AGENT_Q_PATTERN.md](AGENTIC_AGENT_Q_PATTERN.md); [PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md](PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md) |
| `repo-finalizer` | `ls-workflow-repo-finalizer` | Repo Finalizer | Use when safely inspecting repo dirty state and optionally checkpointing allowlisted managed outputs without destructive git operations. | repo finalizer; finalizer harness; finalization checkpoint | `ls-framework-compliance`; `ls-git-workflows` | [HARNESS_AUTOMATION.md](HARNESS_AUTOMATION.md); [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md); `ls/tools/localsetup.py` |
| `spec-clarify-reverse` | `ls-workflow-spec-clarify-reverse` | Reverse Prompt Spec Clarify | Use when running reverse-prompt spec clarification with one question per turn and bounded choices. | decision tree; reverse prompt | n/a | [DECISION_TREE_WORKFLOW.md](DECISION_TREE_WORKFLOW.md) |
| `tmux-terminal-mode` | `ls-workflow-tmux-terminal-mode` | Tmux Terminal Mode | Use when enabling, disabling, defaulting, or checking tmux terminal mode; do not use for one-off sudo or interactive password handoff. | tmux terminal mode; always-on tmux | n/a | [TMUX_TERMINAL_MODE.md](TMUX_TERMINAL_MODE.md); `ls/tools/tmux_terminal_mode` |
| `umbrella-run` | `ls-workflow-umbrella-run` | Umbrella Run | Use when executing a named multi-phase umbrella workflow with explicit pre-human-confirmation gates. | umbrella workflow | n/a | [AGENTIC_UMBRELLA_WORKFLOWS.md](AGENTIC_UMBRELLA_WORKFLOWS.md) |

## Usage

- Agents load the workflow package when a user invokes a workflow ID, package name, or alias.
- Workflow packages install into the managed skill library because every package includes a valid `SKILL.md`.
- Required skills listed in `workflow.yaml` are automatically selected when a workflow's pack is selected.
- Historical publish workflow pointers are retired; use `ls-github-publishing-workflow` plus `ls-automatic-versioning`.
