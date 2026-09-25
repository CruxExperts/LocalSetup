# Repository Guidelines

## Governing Context Contract

This root `AGENTS.md` is the governing agent contract for this repository. Treat it as the first place to preserve durable repo-specific operating rules, not as a lightweight summary.

When the user corrects agent behavior in a way that should survive future sessions, update this file in the same work wave unless the rule is private, secret, temporary, or explicitly not meant for the repo. Do not rely on chat history, memory, or prior-run summaries for rules that should govern future agents.

Do not impose an arbitrary length cap on this file. In particular, do not trim it to fit a 300-line preference or a generic prompt-aesthetics target. If repo rules need more context, add the context here with clear headings and keep it operational.

Keep this file aligned with the repo's actual workflow. If a rule also belongs in installed Codex context for future converted repos, mirror the portable part into `ls/templates/codex/AGENTS.md`. If the rule is only for this checkout, keep it here.

## COIT Repository Mapping

Apply the machine-wide Controlled Outcome Investigation and Termination (COIT)
control to material LocalSetup retries. Keep append-only records in
`.agents/state/<task-slug>/ledger.md`, binding each LocalSetup release problem to its invariant,
minimal reproduction, affected gate, and exact source/tree/diff state. The
common policy alone determines COIT triggers, cycle and review requirements,
counter changes, and terminal dispositions. Do not begin dependent LocalSetup
work or publish while a common COIT or blocker gate remains open.

## Product Naming Contract

Use **LocalSetup (LS)** when introducing the framework abbreviation. Product
and framework display text uses **LocalSetup**; integrated CLI display text uses
**LSCli** and its command is `lscli`. The existing framework command and Python
distribution remain `localsetup`. Do not create `LS` or `ls` command aliases.
The canonical GitHub repository name is **LocalSetup** (`CruxExperts/LocalSetup`);
preserve existing repository URL paths and other compatibility identifiers.
Preserve established imports, paths, URLs, environment variables, persisted keys
and other compatibility identifiers. Model-request identity is constructed by
`ls.core.branding.user_agent()` as `LocalSetup/<framework_version()>` and verified
at final transport send. Preserve upstream attribution, immutable historical
evidence, and managed source ownership. Correct generators before their outputs;
review embedded asset text visually and keep accessibility text aligned. Follow
`ls/docs/BRANDING.md` and the exact exceptions in `ls/config/branding.json`.

## Sequential Release Version Policy

Starting at the last verified published version, apply logical accepted slices
in integration ancestry order: each independently accepted user-visible feature
increments MINOR once and resets PATCH to zero; each small fix, documentation
consolidation, or maintenance slice increments PATCH once. Breaking changes need
an explicit compatibility decision and appropriate MAJOR treatment unless an
active major-line lock blocks that release; while locked, restore compatibility
or stop release work until the lock is explicitly amended. Count a
slice's final accepted outcome once, not its fixups, merges, generated receipts,
release syncs, or work already published. Record the deterministic mapping from
logical slice to source commit, classification, and resulting version.

The corrected 4.x release line is explicitly major-locked: accepted releases may
increment MINOR or PATCH only, with no automatic or implicit MAJOR increment.
Reject any prospective MAJOR classification while this lock is active; do not
silently downgrade it. The one-time numbering reconciliation preserves published
v5.6.2 and its tag/assets as immutable while mapping its content to corrected
arithmetic 4.43.2 after the exact historical issue-100 MAJOR-to-MINOR
reconciliation. Branding maps to 4.44.0, and canonical repository-name policy
maps to 4.44.1. Published v4.44.1 was the first release under corrected 4.x
arithmetic and served as the baseline for the 4.44.2 patch. The v5.6.2 and
v4.22.9 historical release evidence remains preserved. The SDK paging and Agent Q
v2/envelope compatibility breaks remain; corrected numbering does not restore
source or protocol compatibility, and the v4.44.1 release guidance must disclose
both breaks clearly.

Use canonical version/release tooling and preserve source/receipt semantics.
If tooling cannot represent this sequence, implement and validate the missing
policy before publication; never hand-edit generated versions or weaken gates.

## Project Structure & Module Organization

This repository packages LocalSetup, a repo-local framework for agent context, skills, and install workflows. Root files include the Bash installer (`install`), top-level docs, `VERSION`, and support files. The main engine lives in `ls/`: reusable code is under `ls/lib/`, OS discovery helpers under `ls/discovery/`, shipped skills under `ls/skills/`, platform templates under `ls/templates/`, and framework docs under `ls/docs/`. Tests live in `ls/tests/`; static assets live in `assets/`.

## Build, Test, and Development Commands

- `uv sync --locked --all-groups`: sync the repo-local uv project environment from `pyproject.toml` and `uv.lock`.
- `uv run --locked ./ls/tests/automated_test.sh`: run the core Linux/macOS smoke test suite.
- `workers="$(uv run --locked python ls/tools/localsetup.py --source-root . test-workers)" && uv run --locked pytest -n "$workers" ls/tests -q`: run the full Python pytest suite using LocalSetup's hardened worker default. Use this as final consolidation verification for broad/shared changes, release or publish readiness, dependency changes, or explicit user requests, not as the first validation step for routine edits.
- `./install --directory . --tools codex --sync-env --non-interactive --yes`: test a local non-interactive install path for one platform and sync the uv environment.
- `uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .` and `uv run --locked python ls/tools/localsetup.py --source-root . generate-docs`: refresh generated docs artifacts when documentation inputs change.
- `uv run --locked python ls/tools/localsetup.py --source-root . publish-preflight --base <base-ref> --head HEAD`: from a clean worktree, deterministically prepare an unstaged direct version-sync candidate; a changed candidate returns `prepared_not_ready` for review and separate generated-document receipt work. No-flag preflight never stages or commits; only `--fix` prepares and commits the version and generated-document sync slices.
- `uv run --locked python ls/tools/localsetup.py --source-root . release-push`: compute the outgoing Conventional Commit version bump, sync versioned docs, create the release sync commit, and push.

## Command Choice Clarification

Python-first framework tooling does not mean using Python for every shell task. Use standard shell tools for normal inspection and file discovery, such as `rg`, `sed`, `find`, `wc`, and `git`. Use Python when running repo-native Python tools, testing Python code, or parsing structured data where a normal CLI tool is unavailable or less reliable.

Python architecture: new and substantially refactored Python tooling follows ls/docs/PYTHON_ARCHITECTURE_STANDARD.md; keep entrypoints thin, package responsibilities explicit, and existing debt baseline-managed.

## Sandboxed `uv` Cache

When running `uv` commands from an agent sandbox, first check whether the sandbox already allows writes to the user uv cache, normally `~/.cache/uv` or `$XDG_CACHE_HOME/uv`. If the sandbox policy can be adjusted for the session or workspace, prefer adding that cache directory as a writable cache exception; it is host cache state, not repository content.

If the sandbox cannot be changed from the current agent session, do not let `uv` default to a read-only user-level cache. That causes avoidable `Could not acquire lock` or read-only filesystem failures before pytest starts. Use a repo-local ignored cache:

```bash
UV_CACHE_DIR="$PWD/.agents/state/<task-slug>/uv-cache" uv run --locked pytest ls/tests -q
```

Use the same `UV_CACHE_DIR="$PWD/.agents/state/<task-slug>/uv-cache"` prefix for `uv run`, `uv sync`, and related validation commands unless the command intentionally needs the user cache. The controller-assigned `<task-slug>` keeps transient caches Git-bound with the rest of the task state. If the repo-local state path is unavailable, use a writable `/tmp` cache such as `UV_CACHE_DIR=/tmp/localsetup-uv-cache`. Escalate for `uv` cache access only when using the real user cache is required or after trying a writable cache location and confirming the command still fails for a reason that requires approval.

## Single Checkout Development Boundary

LocalSetup development is consolidated in this checkout. Do not create sibling clones, extra Git worktrees, release staging checkouts, PR-specific checkouts, or other repo-shaped directories for LocalSetup work unless the user explicitly authorizes that specific path and purpose in the current task.

Work within the active repository by default. Use normal branches, local commits, stashes, ledgers, and tightly scoped subagent assignments inside this checkout instead of creating filesystem-level copies. Subagents may inspect and edit bounded paths in the active checkout; they do not need separate worktrees merely for isolation.

If a separate checkout or worktree is genuinely necessary, stop and explain why the active checkout cannot satisfy the requirement. Record the approved path, branch, reason, owner, expected lifetime, and cleanup command in the run ledger before creating it. Remove the temporary worktree with `git worktree remove` as soon as the approved purpose is complete.

## Coding Style & Naming Conventions

Keep scripts portable and explicit: Bash files should use `set -euo pipefail`; PowerShell should prefer clear parameter names and non-interactive modes for automation. Python code uses 4-space indentation, standard-library path handling via `pathlib` where practical, and small helper functions in `ls/lib/`. Skill directories use `ls-<topic>` naming and each skill must include a spec-compatible `SKILL.md` with `name` and `description` frontmatter. Markdown should use clear headings, relative links, and concise task-oriented language.

## Testing Guidelines

Add or update tests under `ls/tests/` for changes to path resolution, discovery, parsing, deploy behavior, or skill tooling. Name Python tests `test_<feature>.py` and keep shell wrappers thin. Before the full suite, run compliance and validation checks that match the code you changed, such as focused pytest files or test functions, `validate-catalog`, `validate-package-surface`, `doctor`, generated-doc drift checks, schema checks, and `git diff --check`.

- Use the full Python suite as final consolidation verification for broad framework changes, shared runtime behavior, release/publish work, dependency changes, or explicit user requests. Compute the default worker count with `localsetup test-workers`: `max(1, floor(available CPU cores / 3))`. Worker-consuming tests must not overlap unless they share that aggregate budget. Do not run the full suite as the default first-pass validation for routine daily work; the codebase is large and full-suite runs have noticeable CPU cost. Windows support is WSL2-only in the current framework.

## Unit-Test Concurrency Policy

Unless a repository explicitly defines a stricter policy, every unit-test runner—regardless of language or framework—MUST use an aggregate concurrency budget of `max(1, floor(available CPU cores / 3))`. Always round down before applying the minimum of one worker. Concurrent unit-test processes share that one budget; they MUST NOT each claim the full allowance.

Test effort must be proportional to risk. For tiny docs, policy, metadata, or one-line behavior changes, prefer static checks such as `git diff --check`, targeted syntax checks, or no test run when there is no executable surface. Do not add broad or repetitive tests merely to increase evidence. Add tests only for behavior that can realistically regress, and keep test code smaller than the implementation unless the behavior is safety-critical or has multiple important edge cases. Run the smallest relevant validation first; broaden only after focused checks pass or when the affected surface justifies it.

## Commit & Pull Request Guidelines

Use the existing Conventional Commit style: `feat:`, `fix:`, `docs:`, `chore:`, `test:`, `ci:`, or `type!:` followed by a short imperative summary. Keep PRs focused, target `main`, describe what changed and why, link related issues, and include test results. For framework, skill, or workflow changes, include a brief rationale and note compatibility impact across supported agent platforms. Do not add AI tools or assistants as co-authors or contributors.

Before publishing a branch or opening/updating a PR, treat generated docs and version sync as part of the publish surface, not as volatile noise. Run `publish-preflight` locally against the intended base ref; if it creates sync commits, rerun the focused tests and push the final generated-doc refresh commit with the branch. Do not weaken generated-doc/version validators or ignore those paths merely to get past GitHub validation.

## Kilo Code Review Contract

`REVIEW.md` is the canonical base-branch contract for LocalSetup Kilo Code
reviews. Maintainers must update it in the same change wave when changed review
surfaces make it stale, keeping its concise, actionable, repository-specific
regression standard intact. Kilo Code review agents must read and apply
`REVIEW.md` before reviewing a change; do not assume or invent a Kilo
configuration path or mechanism. Keep this public contract consistent with the
public/private boundary and documentation ownership rules in this file. Agents
report only concrete regressions tied to the changed diff, not unrelated debt or
redesigns.

## Release Authority And Blockers

Release documentation is a publication gate. Use `localsetup release-docs` to
audit the upcoming canonical version, prepare and review affected prose, and
check managed highlights, release-guide links, and release notes before building.
Generate those surfaces from the versioned release content record; numeric
version synchronization alone does not establish documentation readiness. Keep
historical guides and compatibility examples intact. Before publishing a completed
draft, run `release-docs check --draft-tag v<version> --expected-commit <sha>` in
addition to complete artifact verification. Documentation-only repairs preserve
the published tag/assets and use the repair workflow without rebuilding packages.

Do not invent a maintainer-approval requirement. Require GitHub approval only when a live ruleset, branch-protection rule, repository policy, or the user's current instruction requires it. A user request to publish authorizes the ordinary in-scope release actions named in the accepted plan; do not re-ask for each step.

If a real blocker needs a decision, state the issue briefly, recommend the best next action, and ask for one concise authorization. Offer alternatives only when they materially change risk or outcome.

## Security & Configuration Tips

Do not commit local secrets, generated private state, or machine-specific agent data. Treat imported third-party skills as untrusted until reviewed with the repository's safety and validation workflows. Keep version and generated documentation sync changes in the automatic release-sync commit generated by the repo tooling.

## Adapter Directory Ownership

Agent adapter directories, including `.codex/skills`, `.claude/skills`, `.cursor/skills`, `.kilo/skills`, `.openclaw/skills`, `.opencode/skills`, and historical `.agents/skills`, are not exclusive LocalSetup-owned surfaces. Repositories may intentionally keep custom skills, symlinks, files, or mixed managed and repo-owned content in those paths.

LocalSetup repair, migration, installer, and cleanup work must preserve custom adapter content in place by default. Do not move, rename, delete, or "normalize" repo-owned content out of an adapter-shaped directory merely because LocalSetup also writes managed links there. A migration out of an adapter directory requires an explicit repo-owner decision and a preservation plan recorded in the run ledger.

When a LocalSetup tool reports `adapter_content`, `adapter_collision`, or same-directory custom skills, treat that as a manual preservation decision, not permission to make the directory LocalSetup-exclusive. Prefer a tool/code fix or a mixed-adapter preservation path over relocating user content.

## Public And Private Context Boundary

`ls/docs/`, root documentation, platform templates, generated docs, and package/catalog surfaces are publishable framework context. Do not place repo-maintenance plans, private audits, unapproved inventories, internal ledgers, or transient planning artifacts in those locations unless the user explicitly authorizes public documentation.

There are three separate destinations. Do not blur them:

- tracked and GitHub-visible: source, public framework docs, templates, tests, and examples intentionally meant for the public repo
- tracked but release-archive-excluded: rare repo metadata examples that are safe on GitHub but should not ship in framework archives, controlled by `.gitattributes export-ignore`
- local private and untracked: active maintenance state, run ledgers, private audit drafts, generated local indexes, credentials, logs, caches, and planning transcripts

Use `.agents/state/` for all new private repo-maintenance state. The controller allocates one shared task directory at `.agents/state/<task-slug>/`; ledgers, run evidence, audit drafts, handoff material, and task-local caches belong beneath it. Existing `.codex/`, `.omp/`, and `.localsetup-maint/` records are historical and remain in place; do not migrate or add new task state there.

The controller assigns `<task-slug>` once per Git-bound objective as `<YYYYMMDD-HHMMSS>-<git-short-sha>-<objective-slug>`. Every agent and tool reuses that exact directory and records the repository/ref binding in the ledger; no agent-specific state root is created.

Use `.git/info/exclude` only for one-machine extras that are not covered by the tracked `.agents/state/` ignore rule.

Before creating or moving planning material, classify the destination as public framework context or private maintenance state. If public placement is not explicitly authorized, keep the material private and record only a compact public pointer when the user asks for one.

Git hygiene defaults for this repo:

- Keep `.agents/state/`, existing `.codex/runs/`, `.codex/sessions/`, `.codex/logs/`, `.codex/tmp/`, `.localsetup-maint/docs/`, `graphify-out/`, `state/`, `data/`, and root `docs/` out of normal commits.
- Use `.gitignore` for repo-wide private-state patterns. Use `.git/info/exclude` only for one-machine extras.
- Before committing or publishing, check `git status --short`, `git diff --cached --name-status`, and `git ls-files .agents/state .codex/runs .localsetup-maint/docs graphify-out state data docs`.
- If a file must be tracked for local repository operation but should not ship in release archives, add an explicit `.gitattributes export-ignore` rule and state why in the commit.
- The pre-commit hook blocks staged private/local maintenance paths by default. Use `LOCALSETUP_ALLOW_PRIVATE_STAGE=1` only after an explicit public-boundary review.

## Volatile Fact Verification

Before editing or validating markdown claims about latest/current versions, release channels, rate cards, vendor behavior, APIs, protocols, external tool support, security advisories, package compatibility, or platform support, check `.agents/state/volatile-facts.yaml` if it exists.

If the volatile fact index is absent and a volatile claim is found or introduced, create it using the private repo schema. Keep `.agents/state/volatile-facts.yaml` private and untracked; do not move it into public framework docs.

Use source-code, config, and local CLI verification for LocalSetup behavior claims. Use primary upstream research for external claims, such as official docs, release notes, package registries, standards, vendor API schemas, advisories, or live CLI/API behavior where safe. For broad or current external fact verification, use research agents and record source URLs, access dates, conflicts, and limitations in the volatile index.

Update the volatile fact index in the same work wave whenever a volatile claim is verified, corrected, de-volatilized, removed, or newly introduced.

## Agent-Team Workflow Defaults

For non-trivial development work, act as the controller and choose the smallest sufficient route. Use a bounded subagent only when it provides independent falsification, materially reduces wall time, or isolates a risky write scope; non-triviality, file count, uncertainty, or available slots alone do not require delegation. The controller owns requirements clarification, plan quality, task decomposition, delegation, state tracking, verification, and final acceptance.

When a team is useful, define its bounded roles and evidence before launch. Use direct single-agent work when no independent assignment improves evidence, latency, or isolation; do not create a scout or critic merely to satisfy a process tier.

Normal fanout is one or two agents for non-trivial tasks. Use three only when the scopes are clearly independent discovery, research, or validation tasks. Treat configured thread capacity as operational headroom, not a target. Explicit user instructions, tool restrictions, sandbox/approval policy, and active modes override delegation defaults.

Keep the existing native roles generic: `explorer` maps relevant files, systems, docs, workflows, data, dependencies, tests, and risks; `researcher` verifies current or source-backed facts; `worker` executes one bounded task with exact write scope; `tester` runs validations, benchmarks, measurements, and failure summaries; `reviewer` checks final risk, correctness, regression, scope, and evidence. The `guardian_subagent` role is reserved for approval and permission review, not normal task delegation.

For agent-team work, keep a repo-local ledger at `.agents/state/<task-slug>/ledger.md`. The controller assigns `<task-slug>` once as `<YYYYMMDD-HHMMSS>-<git-short-sha>-<objective-slug>` and every participating agent/tool reuses it; do not create client- or agent-specific run directories. Existing legacy ledger paths remain historical records only. In Plan Mode or other no-write contexts, plan the ledger and subtasks but do not create ledger files or edit state until writes are allowed. Record objective, phase, plan, subtasks, checkpoints, validation, decisions, resume notes, and final acceptance criteria. After interruption or compaction, read the ledger, run `git status --short`, inspect outstanding diffs, and resume from the first non-completed task whose dependencies are satisfied.

Only the controller may mark subtasks `verified` or `completed`. A subagent report is evidence, not completion. Claims such as complete, implemented, validated, reviewed, or ready require ledger checkpoints, inspected diffs, validation results, and final review evidence. If evidence is missing, say "Not confirmed yet." and continue the workflow.

Before spawning a subagent, write the task in the ledger with exact scope, what to inspect or change, what not to do, expected output, and validation commands. After a subagent returns, summarize the report in the ledger, verify the evidence directly, then update task status. A report alone is not a checkpoint.

For bounded autonomous maintenance loops, create or resume the private ledger first and preserve the existing dirty baseline. Select exactly one small slice at a time from, in order, an assigned queue or PRD, a failing validation or drift signal, a repo-contract gap, or narrow docs/tests/tooling upkeep. Do not mine broad TODOs or opportunistically expand scope. Use subagents when they reduce context load or enable safe parallel read-only work, but keep implementation to one bounded worker at a time. Validate proportionally, require review evidence before final acceptance, and do not push, deploy, schedule cron, run destructive commands, reshape adapters, run migrations, authenticate, install dependencies, or mutate external systems without explicit approval.

Before final response on non-trivial work, inspect `git status --short`, inspect the scoped diff, run or delegate required validation, resolve or document reviewer findings, and update the ledger with final evidence. Do not claim completion from memory.

### User-Mediated Handoffs

When the user will manually relay a handoff, address the user with an informative request to review or evaluate the material before relaying it; do not frame it as a declarative instruction to, or a message already sent to, an absent agent. This presentation rule does not replace durable artifact-backed executable or restart-sensitive handoffs. Direct agent messages are available only through a native capability to a currently visible target agent.

### Optional Packs, Parallel Slices, And Publishing Integration

An optional LocalSetup skill pack may carry durable operating guidance needed to
manage that selected capability, but it does not automatically become effective
policy for the default LocalSetup installation or overwrite global Codex
behavior, hooks, active goals, or user settings. A direct global-Codex change
requires a separate explicit user request and an isolated owner-scoped change
with its own validation.

A parallel agent may research, draft a prompted handoff artifact, or implement
an explicitly bounded branch slice when its controller packet names the exact
write scope, branch boundary, validation, and stop conditions. That slice is a
candidate contribution only: it must preserve concurrently owned paths and may
not self-integrate, merge, publish, install, or expand into default policy.

The publishing agent alone reconciles the full timeline of parallel branches,
ledgers, PR/base state, review outcomes, ownership boundaries, version/release
state, and final evidence. It evaluates each packet or PR, chooses whether and
how to integrate accepted work, and performs the normal guarded publication
flow. Treat research handoffs as durable review material for that loop, not as
direct execution authority over an absent agent.

### Durable State, Maintainer Role, And Deferred Work

Before an agent calls itself the publishing agent, maintainer, or integrator,
inspect the current Git branch/HEAD, upstream and base refs, status, staged
paths, scoped diffs, and local commit history. Then read the applicable durable
ledgers in their expected private locations—normally `.codex/runs/` and
`.localsetup-maint/`—and verify any associated GitHub issue/PR/release state
through the repository's authenticated `gh` surface when that external state
matters. Git state, durable artifacts, and verified GitHub state are the source
of truth for ongoing development, ownership, and release status; chat history,
process listings, stale summaries, and an agent's self-description are not.

An agent working outside its declared home repository is presumed not to be the
maintainer or publishing agent for that repository. It may perform only the
bounded research or branch-slice work its packet permits until the user
explicitly assigns it the maintainer/publishing role for that exact repository.
Even inside the home repository, no agent may infer publishing authority from a
dirty worktree, a branch name, or a handoff artifact alone.

Do not leave a reusable defect, follow-up, or deferred task only in chat or an
untracked note. Verify whether the relevant upstream GitHub tracker exists and
search for a duplicate; where the active task authorizes the external tracking
write, create or update one sanitized upstream issue with the evidence,
ownership, and acceptance criterion. Otherwise record the item in the
repository's tracked backlog/TODO file when it exists; if neither tracking
surface is available, keep a durable private ledger record and ask the user to
choose the tracking destination. This routing rule does not waive the active
task's external-mutation, security, privacy, or redaction gates.

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
