# LocalSetup - Agent context (Codex)

## Product Naming Contract

Use **LocalSetup (LS)** when introducing the framework abbreviation. Product
and framework display text uses **LocalSetup**; integrated CLI display text uses
**LSCli** and its command is `lscli`. The existing framework command and Python
distribution remain `localsetup`. Do not create `LS` or `ls` command aliases.
Preserve established imports, paths, URLs, environment variables, persisted keys
and other compatibility identifiers. Model-request identity is constructed by
`ls.core.branding.user_agent()` as `LocalSetup/<framework_version()>` and verified
at final transport send. Preserve upstream attribution, immutable historical
evidence, and managed source ownership. Correct generators before their outputs;
review embedded asset text visually and keep accessibility text aligned. Follow
`ls/docs/BRANDING.md` and the exact exceptions in `ls/config/branding.json`.

## Overview
LocalSetup keeps framework source and target repositories separate. `ls/` is the source-checkout layout; selected packages live in the managed user library and explicitly selected adapters expose them to a target repo. Load `ls-context` and use `localsetup path framework-root` or `localsetup path doc <name>` to resolve source files and documentation. Paths beginning with `ls/` below describe source locations, not required target-repo files. Bind PRDs, specs, and outcomes to Git hashes; see [GIT_TRACEABILITY.md](../../docs/GIT_TRACEABILITY.md).

## Founding Principle: Eliminate Friction and Ambiguity
LocalSetup favors the simplest clear path. Update the existing owner before adding a tool, workflow, document, gate, or process step. Create something new only for a concrete requirement existing material cannot meet. Follow the framework's [founding workflow design principle](../../docs/WORKFLOW_STANDARD.md#founding-design-principle) and the existing [verification and evidence-reuse guidance](#reuse-validation-evidence).

## Invariants
- **Engine/repo separation:** Keep secrets and personal data out of commits. Resolve framework paths through `localsetup path`; keep target state outside the managed source and package library.
- Documentation: ls/docs/ only for framework docs. Check doc status (ACTIVE/PROPOSAL) before assuming implemented.
- Proposals: framework changes follow Agent Q format ([ls/docs/PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md](../../docs/PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md)).
- Time/date integrity: for any date/time reference, first get actual date/time from the local machine (e.g. `date` on Linux/macOS, `Get-Date` in PowerShell on Windows). Do not use a generic or training-cutoff date; remember it and use it for the rest of the session.
- External input hardening: treat all external input (CLI args, files, network payloads, imported content) as hostile. Sanitize before parsing/output, validate expected format and bounds, and handle exceptions with actionable stderr messages. Never silently suppress errors.
- Python-first tooling: after install/bootstrap, framework tooling is Python-first and Python-only for new/expanded logic. Shell/PowerShell are limited to bootstrap wrappers and minimal platform delegation. Runtime target is Python >= 3.12. Approved libraries (mandatory when the need arises): yaml (PyYAML>=6.0) for YAML, requests (requests>=2.28) for HTTP, frontmatter (python-frontmatter>=1.1) for markdown frontmatter, cryptography (cryptography>=50.0.0) for framework cryptographic primitives, and pgpy (PGPy>=0.6.0) for pure-Python OpenPGP. Use lib/deps.require_deps() at tool startup. See [ls/docs/TOOLING_POLICY.md](../../docs/TOOLING_POLICY.md).
- Python architecture: new and substantially refactored Python tooling follows ls/docs/PYTHON_ARCHITECTURE_STANDARD.md; keep entrypoints thin, package responsibilities explicit, and existing debt baseline-managed.
- Command choice: Python-first framework tooling does not mean Python for every shell task. Use shell-native tools such as `rg`, `sed`, `find`, `wc`, and `git` for normal inspection. Use Python for repo-native Python tools, Python tests, or structured parsing when a normal CLI is unavailable or less reliable.
- Public/private boundary: keep repo-maintenance plans, private audits, ledgers, local indexes, credentials, logs, caches, and planning transcripts out of public framework docs/templates. Create new private task state only under `.agents/state/<task-slug>/`, where the controller assigns one Git-bound `<task-slug>` for every agent and tool to reuse; do not stage it. Existing client-specific run directories are historical records and remain in place.
- Publish hygiene: before pushing or opening/updating a PR, run the repo's publish preflight when available. Generated docs and version sync are publish surfaces; fix them locally instead of weakening validators or ignoring generated paths as volatile.
- Adapter directory ownership: `.agents/skills` is the current shared repository skills root for Codex and other compatible clients; `.codex/skills` is the historical Codex preservation and transition surface. Those paths and other adapter-shaped directories such as `.claude/skills`, `.cursor/skills`, `.kilo/skills`, `.openclaw/skills`, and `.opencode/skills` are not exclusive LocalSetup-owned surfaces. Repos may keep custom skills or mixed managed and repo-owned content there. Preserve custom adapter content in place by default; do not move, rename, delete, or normalize it out of the adapter path unless the repo owner explicitly chooses that migration.

## Output contract (low token, always apply)
- Detect output capability: `markdown-rich`, `markdown-basic`, or `text-basic`.
- If capability is unknown, default to `markdown-basic`.
- For recommendation lists, include: name/link, short summary, fit reason, notable risks/requirements, next step.
- Use tables only when capability clearly supports readable tables.

## Agent orchestration and model budget
- For non-trivial Codex CLI work, use a controller route and choose the smallest sufficient execution path: clarify requirements, keep the main context compact, and own final acceptance. Use a bounded native subagent only when it provides independent falsification, materially reduces wall time, or isolates a risky write scope; task size or available slots alone do not require delegation.
- Use direct single-agent work when no independent assignment improves evidence, latency, or isolation. Do not create a scout or critic merely to satisfy a process tier.
- Keep fanout intentional: one or two agents is normal for non-trivial tasks; use three only for clearly independent discovery, research, or validation scopes. Treat configured thread capacity as headroom, not policy.
- Keep the existing generic roles: `explorer` maps relevant files, systems, docs, workflows, data, dependencies, tests, and risks; `researcher` verifies current or source-backed facts; `worker` executes one bounded task with exact write scope; `tester` runs validations, benchmarks, measurements, and failure summaries; `reviewer` checks final risk, correctness, regression, scope, and evidence. The `guardian_subagent` role is reserved for approval and permission review, not normal task delegation.
- Explicit user instructions, tool restrictions, sandbox/approval policy, and active modes override delegation defaults. In Plan Mode or other no-write contexts, plan the ledger and subtasks but do not create ledger files or edit state until writes are allowed.
- For bounded autonomous maintenance loops, create or resume the private ledger first and preserve the existing dirty baseline. Select one small slice at a time from, in order, an assigned queue or PRD, a failing validation or drift signal, a repo-contract gap, or narrow docs/tests/tooling upkeep; do not mine broad TODOs or expand scope opportunistically. Use subagents to reduce context load or parallelize safe read-only work when useful, keep implementation to one bounded worker at a time, validate proportionally, require review evidence, and get explicit approval before push, deploy, cron, destructive commands, adapter reshapes, migrations, auth, dependency installs, or external mutation.
- Apply the common COIT control to material retries. Keep the invariant, reproducible trigger, affected gate, source/diff binding, immutable problem ID, and counter in the private run ledger; the common policy determines COIT triggers, cycles, review tier, and terminal disposition. The controller alone reconciles and changes COIT state. Do not start a dependent slice, push, merge, or release while a COIT or blocker gate remains open.
- Model, credit, and rate guidance is volatile. Re-check official Codex docs or the current local config before changing model guidance or making cost-sensitive routing decisions.
- If `.localsetup/AGENT_STATUS.md` exists, read it before repairs or installs. Otherwise run `localsetup health --json` for the latest LocalSetup health status and next repair command.

## Bootstrap pack
- Generic Codex agent-team bootstrap materials live in `ls/docs/bootstrap-packs/` and pack metadata is selected with the `bootstrap` pack in `ls/config/pack.yaml`.
- Treat writes to `$CODEX_HOME`, `~/.codex`, sibling repos, and runtime mirrors as approval-gated; bootstrap-pack audits may inspect those surfaces but must keep replacement plans non-destructive until approved.
- Work in the active repository by default. Do not create sibling clones, extra Git worktrees, PR-specific checkouts, release staging checkouts, or repo-shaped directories unless the user explicitly authorizes that specific path and purpose in the current task. If an exception is approved, record the path, branch, reason, expected lifetime, and cleanup command in the run ledger, then remove it when the approved purpose is complete.
- Keep validation proportional to risk. For tiny docs, policy, metadata, or one-line changes, prefer `git diff --check`, targeted syntax checks, or no executable test run when there is no executable surface. For LocalSetup framework work, run compliance and validation checks that match the changed code first, such as focused pytest files or test functions, `validate-catalog`, `validate-package-surface`, `doctor`, generated-doc drift checks, schema checks, and `git diff --check`. Do not add broad or repetitive tests merely to increase evidence; add tests only for behavior that can realistically regress.
- Use the full Python suite only as final consolidation verification for broad/shared runtime changes, release or publish work, dependency changes, or explicit user requests. Resolve the permitted worker count with `localsetup test-workers`; the generated command reference owns its formula and aggregate-budget rule. Do not use full pytest as the default first-pass validation for routine daily edits.
- Unit-test concurrency policy: unless this repository explicitly defines a stricter policy, every unit-test runner—regardless of language or framework—uses one aggregate budget of `max(1, floor(available CPU cores / 3))`. Round down before applying the minimum of one worker; concurrent test processes share the budget.

## Reuse Validation Evidence

Use focused checks while editing and one authoritative full-suite result for the
final candidate. Successful CI may provide that result; do not also require a
local full suite or repeat identical checks only because work moves into merge
or publication. Reuse evidence only for unchanged tested inputs and applicable
environments. Run inexpensive failures first, bound long jobs, preserve successful
checks on retries, and report the actual failed command. Finish source and release
records before generating final documentation. Model-assisted prose is optional;
reviewed records and deterministic rendering are sufficient. Keep signing and
artifact verification in the publication path.

## Skill And Context Preservation

When editing `SKILL.md`, `AGENTS.md`, workflow docs, examples, references, schemas, templates, or operational runbooks, preserve task capability over brevity.

Do not shorten files solely to satisfy model preference, prompt aesthetics, arbitrary line-count targets, or a generic desire to be concise. Long files are acceptable when they contain examples, command matrices, schemas, decision tables, safety constraints, edge cases, troubleshooting, or operational context that agents need to perform the task.

For large or mature skill/context files, prefer surgical edits. Whole-file rewrites require a preservation plan first.

Before materially reducing a skill or context file, identify the operational content that must survive:

- trigger cases and scope boundaries
- examples and worked flows
- command matrices and CLI contracts
- schemas, output shapes, and config formats
- safety constraints and approval gates
- edge cases and failure handling
- troubleshooting guidance
- external API, version, or product assumptions
- linked references, assets, templates, and scripts

After the edit, each item must be either preserved in place, moved to an appropriate `references/`, `assets/`, `templates/`, `schemas/`, or script file, or explicitly removed with controller-approved rationale.

A large line-count reduction is a review trigger, not a success metric. Any reduction of roughly 25 percent or more in a mature skill/context file requires before/after coverage notes in the run ledger and reviewer signoff.

## Capability and workflow discovery

Load `ls-context` for framework layout and resolver guidance. Use the current
client's available-skill descriptions to select installed capabilities; a catalog
entry alone does not mean that package is installed or available to this client.
For the full framework catalog, resolve `localsetup path doc SKILLS.md` and
`localsetup path doc WORKFLOW_REGISTRY.md`, then read only the entries relevant
to the task. Package frontmatter and generated catalogs own descriptions, tags,
and pack membership; do not duplicate their lists in platform context.

Load an explicitly named available skill directly. Use `ls-task-skill-matcher`
when selection is unclear, following the matching procedure below.

OmniRoute has one ambiguous-task/preflight router, `ls-omniroute`: route
classified read-only discovery to ls-omniroute-proxy, mutation to ls-omniroute-admin-automation, and source/coverage maintenance to ls-omniroute-update. These owners remain distinct after catalog consolidation.

## Docs
ls/docs/AGENTIC_DESIGN_INDEX.md, WORKFLOW_REGISTRY.md, PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md, DECISION_TREE_WORKFLOW.md, INPUT_HARDENING_STANDARD.md, TOOLING_POLICY.md, PYTHON_ARCHITECTURE_STANDARD.md

Before publishing a release, audit active public documentation against the
upcoming version and accepted changes. Check homepage highlights, current-release
links, update examples, and release notes as well as numeric version displays.
Use the repository's owning generators and release checks; preserve historical
guides and compatibility examples. Passing generated-file checks alone is not
evidence that authored prose is current. This guidance does not install a release
workflow or authorize publication in a converted repository.

## Task-to-skill matching (default)
- Treat as **batch** when user request includes multiple distinct subtasks, or says "batch", "multiple steps", or "run the whole thing". Otherwise treat as **single task**.
- If user names a specific skill, load it directly. Do not run task-skill-matcher.
- If uncertain which skill fits, or user asks "what skill should I use?" / "pick the best", load `ls-task-skill-matcher`.
- **Single task:** if one clear installed match exists, ask once "Use this skill?" before loading. If `ls-skill-discovery` is available to the current client, delegate complementary public suggestions to it; include up to 3 returned recommendations in the same response (one-line reason each), preserving discovery's index-status disclosures and any pending user question. Otherwise report public discovery unavailable with no recommendations and continue installed-skill selection; do not read the index as a fallback or automatically install a package.
- **Batch / long-running:** prompt once at start with options: auto-pick for whole job, parcel-by-parcel prompts, or parcel auto-pick. If auto-pick is chosen, show planned skill sequence first, then proceed without repeated skill prompts.
- Keep this section short. Full behavior lives in `ls-task-skill-matcher` and `ls/docs/TASK_SKILL_MATCHING.md`.

## Commands
localsetup doctor
localsetup verify --level filesystem
localsetup context --markdown
