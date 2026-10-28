# LocalSetup - Agent context (Codex)

## LocalSetup toolchain

Human-facing CLI layouts use an 80-column baseline. Reserve right-edge percentage
space in those rows without fixing terminal or tmux window dimensions. The TUI
canvas fills the qualified primary viewer's terminal and resizes with it.
Keep CLI row sizing independent of native viewport sizing. Subtract reserved
fields before sizing activity chunks; pad fixed rows and put telemetry on its own
rows. Count visible columns, not ANSI bytes, and use one coordinated output owner.
Wrap secondary fields instead of overflowing. Preserve machine-readable JSON and
exact copyable values, with an explicit compact fallback for narrower terminals.
Use the selected theme and readable no-color output across framework tools.

Use compact TUI labels and semantic theme colors to distinguish identity,
purpose, location and state. Center overlays horizontally and vertically, with
an upper-right clickable `[ESC]` that shares the keyboard Escape close or cancel
action. CLI entrypoints and subcommands support `-h`, `--help`, `-?` and `--?`;
preserve literal arguments after `--`.

This environment uses LocalSetup (LS) for reusable skills, workflows and managed
operations. Load `ls-localsetup-project-context` first when available. Select
available capabilities from the current client's skill descriptions; use
`ls-task-skill-matcher` when the match is unclear. Resolve the broader catalog
with `localsetup path doc SKILLS.md` and `localsetup path doc WORKFLOW_REGISTRY.md`.
Catalog entries describe capabilities; they do not prove installation or authority.

For observable shell work, use `localsetup shell` when its capability checks pass
and follow `ls-wf-tmux-terminal-mode`. Report the execution machine, whether the
owned session was created or joined, and its copyable local join command at
startup, and at least once every five minutes while a long-running task is
active. Display the deterministic CLI join banner directly in user-visible
updates at command starts and periodic progress; tool output alone is insufficient.
Keep banner lines separate from prose and use their distinct terminal color
(`FORCE_COLOR=1` for capable captured streams); never insert raw ANSI into chat.
Reserve the
terminal status row for activity. Use the verified local command without an SSH prefix. Preserve native permissions
and sandboxing.
Privileged work retains `ls-wf-sudo-tmux-session` and its controller-owned pane.
Change authored policy owners and generators, never rendered policy tails.

## Deterministic Automation And Feedback

Move repeatable decisions into tested deterministic framework code. Configuration
owns adjustable policy; CLI interfaces expose validated choices to people and
agents without requiring an agent to calculate, reconstruct, or enforce the
workflow. Preserve provenance and applicable policy for client-agent actions.

Build reusable deterministic components with versioned input, output, error and
provenance contracts. Separate policy from measurement, dispatch and presentation;
compose existing owners through small adapters. Use the common schema-backed
configuration entry point for preferences, presets and effective-value origins.
Keep private keys, trust records and signed execution grants with their protected
owners. TLS authenticates supported connections; OpenPGP signatures authenticate
supported artifacts. Neither alone authorizes execution. Validate trust, scope,
compatibility, permission and isolation at the operation boundary.

When using the framework test runner, mix pending work with a recorded seed and
share that seed across CI shards and failed-job reruns. Resource limits and
debounced pressure controls remain authoritative; random mixing alone does not
guarantee balanced memory. Read versioned telemetry through the CLI data interface
for worker limits, queues, memory, freshness and cleanup. Terminal views consume
that interface without recalculating policy or signaling jobs. Fixed single-worker
runs retain telemetry while bypassing adaptive scaling decisions.

Operational failures must provide structured exception context and useful debug
evidence through the existing paginated CLI event interface. Extend the existing
feedback and request owners to prepare a deterministic PRD draft from that
evidence, with clear context and fields for optional agent observations. Keep raw
private evidence separate from a reviewed, portable report. The configured
LocalSetup issue destination, duplicate checks, and explicit user authorization
control submission; an error or generated draft never authorizes publication.
Keep feedback preparation, review, and submission discoverable as an embedded
workflow for both console users and agents. Improve existing owners rather than
adding parallel log stores, report formats, or submission paths.

## Envelope Schema Compatibility

When extending a versioned envelope, preserve previous supported generations,
exact signed ciphertext, public APIs, and version-specific decoding. Test older
fixtures and interoperability. Define bounded optional extensions separately
from required capabilities; reject unknown required features explicitly before
acceptance. Optional extensions must not change security semantics. Preserve
historical authorization and explicit verified migration for older objects;
compatibility never makes unsigned or unverifiable data authenticated or grants
new authority. Do not rewrite historical ciphertext in place.

## Machine Encryption Identity Defaults

For authorized secure-container runtime setup, prefer explicit custom keys,
then a persisted machine identity, then automatic creation of a distinct default
identity through the shared key lifecycle. Protect and persist that mapping;
restarts and concurrent setup must not generate replacements. Label generated
keys `LocalSetup Encryption` with a distinct non-sensitive machine identifier;
select authority by full fingerprint, not the label. Keep lifecycle owner and
machine roles distinct. Recipient
enrollment follows configured owner/controller authority. New keys do not grant
access to old ciphertext. Installing the framework alone does not activate the
runtime or enroll a machine.

Default generated identities use `pgp@cruxexperts.com` and signed provenance
metadata naming `https://github.com/CruxExperts/LocalSetup`,
`Copyright © <creation year> Crux Experts LLC`, `https://www.cruxexperts.com/`,
and `Private asset. Property of Crux Experts LLC. Use, reproduction, modification,
distribution, or publication without prior written permission is prohibited.`
Include the GitHub URL, copyright, company URL, and complete rights notice
together in the signed description metadata. Keep the visible label concise and
preserve the
complete notice in dedicated signed metadata fields. Custom overrides remain
available.

Keep local catalog protection and incoming content approval in independently
configured trust contexts. Reuse the shared trust implementation for distinct
scoped stores; local catalog signatures and generated identities cannot enroll
peers or replace origin signatures, controller approval, or content receipts.

## Product Naming Contract

Skill and workflow names and discovery context must lead with the circumstances
and task phrases that naturally trigger them. For new or intentionally renamed
packages, use the three to five most relevant activation keywords:
`ls-<trigger-keywords>` for skills and `ls-wf-<trigger-keywords>` for workflows.
Prefer signals such as sudo, permission denied, merge conflict, failing tests,
or dependency advisory to abstract outcomes. Avoid redundant type words and filler.
Descriptions must state what the package does, when to use it, and meaningful
scope limits; include natural synonyms there instead of stuffing the name.
Keep published selectors as aliases during package renames, migrate recorded
exposure through its owner, and regenerate catalogs and provenance from source.
The skill authoring owner and workflow standard must carry this default; do not
rely on a chat reminder or broaden a naming migration into capability changes.

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
LocalSetup keeps framework source and target repositories separate. `ls/` is the source-checkout layout; selected packages live in the managed user library and explicitly selected adapters expose them to a target repo. Load `ls-localsetup-project-context` and use `localsetup path framework-root` or `localsetup path doc <name>` to resolve source files and documentation. Paths beginning with `ls/` below describe source locations, not required target-repo files. Bind PRDs, specs, and outcomes to Git hashes; see [GIT_TRACEABILITY.md](../../docs/GIT_TRACEABILITY.md).

For LocalSetup product work, follow its source-owned branding and licensing
contracts. Lead with visible operations, explicit security/privacy boundaries,
and practical engineering value; do not infer universal encryption or native
client permissions from marketing language. Preserve earlier and third-party
license grants when current first-party terms change.

## Founding Principle: Eliminate Friction and Ambiguity
LocalSetup favors the simplest clear path. Update the existing owner before adding a tool, workflow, document, gate, or process step. Create something new only for a concrete requirement existing material cannot meet. Follow the framework's [founding workflow design principle](../../docs/WORKFLOW_STANDARD.md#founding-design-principle) and the existing [verification and evidence-reuse guidance](#reuse-validation-evidence).

Friction-free, robust operation is a framework-wide LocalSetup policy tenet. Improve the existing owner before creating another path. Automate routine, reversible work within existing authority; ask only for a materially different decision or a genuine authorization boundary. Reuse unchanged approvals and applicable validation evidence. Do not add duplicate checks, reviewers, ledgers, polling loops, or mandatory phases. Every additional gate must protect a named safety, correctness, or acceptance need. Friction-free never means bypassing security, signatures, privacy, or the user's control.

For long-running interactive work, give the human operator concise, scannable progress at milestones and periodically while work continues. Use the [human progress signal guidance](../../skills/ls-response-style-tools/SKILL.md#human-progress-signals); skip command-by-command narration.

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
- For non-trivial Codex CLI work, the main agent coordinates and is the primary implementation writer. Use up to two auxiliary agents for bounded discovery, research, validation, and review when useful. Keep implementation with the main agent unless the user explicitly changes this preference; preserve the user's active model and effort selection. Task size or available slots alone do not require delegation.
- Use direct single-agent work when no independent assignment improves evidence, latency, or isolation. Do not create a scout or critic merely to satisfy a process tier.
- Keep fanout intentional: one or two agents is normal for non-trivial tasks; use three only for clearly independent discovery, research, or validation scopes. Treat configured thread capacity as headroom, not policy.
- Keep the existing generic roles: `explorer` maps relevant files, systems, docs, workflows, data, dependencies, tests, and risks; `researcher` verifies current or source-backed facts; `worker` executes one bounded task with exact write scope; `tester` runs validations, benchmarks, measurements, and failure summaries; `reviewer` checks final risk, correctness, regression, scope, and evidence. The `guardian_subagent` role is reserved for approval and permission review, not normal task delegation.
- Explicit user instructions, tool restrictions, sandbox/approval policy, and active modes override delegation defaults. In Plan Mode or other no-write contexts, plan the ledger and subtasks but do not create ledger files or edit state until writes are allowed.
- For bounded autonomous maintenance loops, create or resume the private ledger first and preserve the existing dirty baseline. Select one small slice at a time from, in order, an assigned queue or PRD, a failing validation or drift signal, a repo-contract gap, or narrow docs/tests/tooling upkeep; do not mine broad TODOs or expand scope opportunistically. Use subagents to reduce context load or parallelize safe read-only work when useful, keep implementation with the main agent, validate proportionally, require review evidence, and get explicit approval before push, deploy, cron, destructive commands, adapter reshapes, migrations, auth, dependency installs, or external mutation.
- Apply the common COIT control to material retries. Keep the invariant, reproducible trigger, affected gate, source/diff binding, immutable problem ID, and counter in the private run ledger; the common policy determines COIT triggers, cycles, review tier, and terminal disposition. The controller alone reconciles and changes COIT state. Do not start a dependent slice, push, merge, or release while a COIT or blocker gate remains open.
- Model, credit, and rate guidance is volatile. Re-check official Codex docs or the current local config before changing model guidance or making cost-sensitive routing decisions.
- If `.localsetup/AGENT_STATUS.md` exists, read it before repairs or installs. Otherwise run `localsetup health --json` for the latest LocalSetup health status and next repair command.

## Bootstrap pack
- Generic Codex agent-team bootstrap materials live in `ls/docs/bootstrap-packs/` and pack metadata is selected with the `bootstrap` pack in `ls/config/pack.yaml`.
- Treat writes to `$CODEX_HOME`, `~/.codex`, sibling repos, and runtime mirrors as approval-gated; bootstrap-pack audits may inspect those surfaces but must keep replacement plans non-destructive until approved.
- Work in the active repository by default. Do not create sibling clones, extra Git worktrees, PR-specific checkouts, release staging checkouts, or repo-shaped directories unless the user explicitly authorizes that specific path and purpose in the current task. If an exception is approved, record the path, branch, reason, expected lifetime, and cleanup command in the run ledger, then remove it when the approved purpose is complete.
- Keep validation proportional to risk. For tiny docs, policy, metadata, or one-line changes, prefer `git diff --check`, targeted syntax checks, or no executable test run when there is no executable surface. For LocalSetup framework work, run compliance and validation checks that match the changed code first, such as focused pytest files or test functions, `validate-catalog`, `validate-package-surface`, `doctor`, generated-doc drift checks, schema checks, and `git diff --check`. Do not add broad or repetitive tests merely to increase evidence; add tests only for behavior that can realistically regress.
- Use the full Python suite only as final consolidation verification for broad/shared runtime changes, release or publish work, dependency changes, or explicit user requests. Resolve the permitted worker count with `localsetup test-workers`; the generated command reference owns its formula and aggregate-budget rule. Do not use full pytest as the default first-pass validation for routine daily edits.
- Unit-test concurrency policy: The default local aggregate test-worker budget is `max(1, floor(available logical CPU cores / 3))`. A configured positive integer override may increase or decrease that budget, with a hard cap of the available logical CPU count. Resolve and enforce the budget in code from the selected repository configuration; agents and users edit settings, not scheduling calculations. Hosted shard jobs each use their isolated runner's CPU and memory budget; matrix concurrency bounds active jobs. Concurrent test processes on one host share that host's budget; they must not each claim it. Preserve stricter native execution limits.
- On resume, recalculate the budget from currently available CPUs. Inspect inherited worker overrides and do not carry forward a stale lower setting when the user requests the full formula-based budget.

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

Load `ls-localsetup-project-context` for framework layout and resolver guidance. Use the current
client's available-skill descriptions to select installed capabilities; a catalog
entry alone does not mean that package is installed or available to this client.
For the full framework catalog, resolve `localsetup path doc SKILLS.md` and
`localsetup path doc WORKFLOW_REGISTRY.md`, then read only the entries relevant
to the task. Package frontmatter and generated catalogs own descriptions, tags,
and pack membership; do not duplicate their lists in platform context.

Load an explicitly named available skill directly. Use `ls-task-skill-matcher`
when selection is unclear, following the matching procedure below.

OmniRoute has one ambiguous-task/preflight router, `ls-omniroute-task-triage`: route
classified read-only discovery to ls-omniroute-model-discovery, mutation to ls-omniroute-admin-automation, and source/coverage maintenance to ls-omniroute-skills-update. These owners remain distinct after catalog consolidation.

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

### Observable agent family terminal

Keep one active UX session per owning agent and execution machine for the agent's
lifespan. Its admitted subagents and concurrent shell commands share that machine's
family session. Bind ownership to the exact machine, endpoint and owner generation;
machine labels alone do not establish authority. Multiple machines have distinct
sessions and machine-labelled join instructions.

The terminal deliverable is a complete dynamic TUI, including discoverable pane,
layout, page, log and help controls plus reusable text, multiline, password,
checkbox, radio and confirmation dialogs. An empty Idle screen, a transcript,
or a passing renderer test is not acceptance of that interface. Verify the
actual advertised session, visible controls, keyboard and mouse interaction,
resize/redraw, and the secure consumer handoff. Mark unfinished controls and
adapters as unfinished; do not replace the requested UI with a reduced view.

Let the qualified primary terminal control a dynamic canvas, with columns clamped
between 80 and 500 and 120 as the unattached default. Redraw and rebalance on size
changes while preserving running processes and operation identity. Track lifecycle,
output activity and requested input separately. Compact idle work into a single
visible status line where the backend supports it; expand active work and preserve
accurate state labels. Qualify physical layout and history bounds rather than
claiming a two-row pane is one line. Label panes from trusted agent identity and a
short purpose slug, such as `root · tests` or `subagent scout · source-map`;
application titles and command arguments do not establish identity. Include the
verified current working directory in the divider title. Show its full path when
it fits; otherwise shorten the middle with `...`, preserving a recognizable
prefix and the final folder name. Sanitize terminal controls and format syntax.

Observers should be able to scroll with command input disabled by default.
Enable requested human input explicitly, show its state visibly, and revoke it
when that request ends. Protect healthy running commands, requested input,
unsaved editor buffers and unknown operations during recovery. Reconcile lost
acknowledgements without replaying mutations. Exhausted deterministic recovery
must hand the controller bounded evidence and safe next options.

Target 20,000 history lines and cap retained text at 20,000,000 bytes per pane.
Monitor tmux's separate native allocation; a line limit does not establish a
native memory byte limit. Automatic context contains at most the newest 200
lines and 32 KiB of output text in total, whichever limit is reached first.
Truncate oversized lines safely at UTF-8 boundaries and report truncation;
earlier history requires explicit bounded paging. The protected privileged
operations pane retains its own controller authority and lifecycle. Put these
requirements in the shared terminal owner with thin client adapters.

Enforce terminal limits, timed join advertisements, ownership, input leases and
recovery transitions deterministically in the CLI. Instructions describe the
contract; they are not a substitute for executable enforcement.

Show the overview as Idle when it is idle; keep failed command states distinct.
Expose compact activity telemetry from actual operation records, such as running
and completed counts, elapsed time and output activity, in one status row. Advertise
the join command at startup and periodically without reserving a footer row for
it. Construct advertisements in the CLI from verified session and machine data,
never by hand: center the hostname with one space on either side in the top
hash border, place the unmodified command alone between blank lines, and use a
final hash border. Derive the border width from the command display width,
expanding only to fit the hostname. Multiple entries use their common maximum
width and share each following hostname border with the preceding entry. Color
is optional and respects terminal capability and NO_COLOR. A completed agent may
close its session for future dispatch while observers remain attached. Preserve
their view and automatically remove the idle owned session after the last viewer
detaches; never destroy healthy active or uncertain work to satisfy cleanup.

Store managed tmux settings in versioned JSON or YAML with a schema. At each
owned runtime startup or resume, load valid configured values, establish the
complete effective baseline and verify native readback before dispatch. Ignore
ambient tmux configuration. Create a missing local file from packaged defaults;
report invalid fields and use defaults only for those fields, preserving valid
values. Keep previous supported configurations readable. Future optional fields
must be bounded and safely ignored and preserved by older compatible readers;
unknown required capabilities must fail explicitly before applying configuration.
Use the approved branding palette and labels rather than color alone for state.
Give controls explicit background colors and readable foreground contrast. Do not
dim instructions or inherit grey-on-black terminal defaults. Distinguish selected
controls with both a visible marker and a contrasting surface, and verify the
actual terminal render after resizing before accepting a UI change.
Explicit control selections must repaint the qualified viewer even when the
selected value is unchanged, while preserving workload processes and output.

### Live configuration, signatures and traceability

Use one shared configuration lifecycle across framework owners rather than
independent polling and persistence loops. Its cadence is an integer from 1 to
3,600 seconds, defaulting to 60 seconds. Stage validated persistent changes in
memory, flush dirty values at that cadence, and check for file changes at the
same cadence. Provide an explicit CLI operation to flush all participating
persistent values and reload their committed revision after every write finishes.
Direct file editing remains supported through the same validation, signature,
conflict and authority checks; an external edit must not be silently overwritten.

Verify signatures against explicitly trusted configuration authority before
applying protected configuration. Key generation or a model request cannot grant
that authority. Preserve exact signed bytes, supported older schemas and bounded
optional extensions. Use atomic revision publication and crash recovery for
multi-file updates, compare expected revisions before writes, and retain the last
accepted revision when validation, signatures or persistence fail. Never accept
a partial set of files as a completed configuration transaction.

Apply eligible tuning changes live without restarting healthy jobs. Changes to
grants, identities, endpoints, credentials, filters or confinement must retain
their owning security checks; self-regulation cannot expand an agent's authority.
Record bounded, redacted provenance for proposed, persisted, applied and rejected
changes, including actor, machine, operation, revision, content digest and result.
Keep secrets and raw sensitive data out of those records. Describe the supported
traceability controls and their limits without claiming HIPAA or SOX compliance.

### Linux automation and the built-in agent boundary

The built-in LSCli agent must obtain filesystem, process and network effects
through the shared CLI broker and its live capability grants. Enforce confinement
at the tool and process boundary; prompt instructions alone are not an isolation
mechanism. Keep external development clients' native permissions intact while
implementing this runtime. Prefer precise structured operations and compact,
session-bound context to repeated discovery, long command flags and improvised
shell recovery. Reuse existing owners for privileged operations and authentication.

Remote control must use the same authenticated, encrypted operation protocol over
SSH, HTTP streaming or ordinary request/response. Transport identity does not
grant execution authority. Bind requests, inputs, cancellation and receipts to
the admitted agent, machine, scope and operation ID. Select fallbacks before
dispatch; reconcile an uncertain operation without replaying it through another
backend. Prefer the newest qualified available tmux release, retain explicit
legacy compatibility, and provide a visible, bounded human manual handoff.

Show pane state in its header, never as an added notice in application output.
Use plain labels such as Idle, Running, Input needed, Completed, Failed and
Finishing; do not expose tmux wording such as "pane is dead" to viewers.
Preserve internal native state fields and the application's original output.

Use each pane's existing title or separator for real activity telemetry, with
stable widths and the approved theme. Keep one bottom control row for clearly
labelled keyboard and mouse actions. Input-disabled observers must not accidentally
send those shortcuts to running applications. Coalesce layout changes and use
activity hysteresis so new output and idle transitions do not cause resize jitter.
Observe actual terminal columns and rows through the terminal event path. Use a
trailing debounce before layout changes so a cascading resize applies once the
border settles. Keep this separate from configuration polling. Below 80 columns,
or the current layout's minimum height, show an observer-local size warning and
preserve healthy commands. Distinguish actual terminal size from the qualified
canvas; wider dynamic layouts require a proven retained-text bound.
Collapsed dormant sections occupy exactly one visible row and sort above expanded
sections. A dormant-to-active transition moves the section below earlier active
sections; continuing output does not repeatedly reorder it. Concurrently active
sections share available height proportionately. Keep the most recently activated
section expanded with its output when all work becomes dormant. Display dormancy
never stops or suspends a running process, and input-required remains distinct.
Expanded sections have a configurable minimum height, default 10 rows including
the title, with explicit per-operation overrides. Pack overflow into deterministic
logical pages rather than squashing below those minima; cap sections per page
through configuration. Reserve one bottom row for Page left and Page right.
Recalculate dimensions, page boundaries and heights on every settled resize,
preserving the observer's selected content and dialog input. Background activity
must not pull an observer away from a manually selected page. Preserve collapsed
headers and retained output through redraws; test actual native resize behavior.

Provide an emergency halt action that immediately closes dispatch and input gates,
revokes leases and signals owned workers. Report confirmed, pending and unreachable
stops accurately while keeping emergency control available. Journal bounded,
filtered events in a versioned operational buffer while agent delivery is paused.
Expose that buffer through the CLI with references, cursors and checkpoints;
resume with trusted instructions and bounded context, reconciling uncertain work
before new dispatch. The event buffer does not replace durable semantic memory.

Configure output filters through versioned JSON and a schema. Apply word and
bounded pattern rules consistently to configured observation, retained-log and
agent-context sinks, with explicit transformation metadata. Preserve original
file bytes, authenticated protocol fields, receipt integrity and input semantics.
Reject unsupported or unsafe rules explicitly rather than silently bypassing a
required filter. Bound streaming work, retained data and context by bytes as well
as lines.

Rules have explicit writable positive integer order values and execute in ascending
order. Use default gaps such as 10, 20 and 30 to permit insertions; reject duplicate
orders within a set. Transforms continue through subsequent rules; blocking,
quarantine and halt actions terminate delivery. Preserve authenticated identities,
permissions and protocol fields when translating display terminology. Regex
detection supplements structured validation and parameterized database operations.

Agent configuration must support endpoint, secret reference, model and relevant
request parameters through the existing configuration owner and environment
variables. OmniRoute is the primary OpenAI-compatible gateway target. Verify its
current capabilities and preserve supported wire behavior without claiming all
provider features are interchangeable. Do not print, persist in portable files,
or pass API key values to child workloads; keep the active client's model and
configuration unchanged unless the user explicitly requests that change.

Prioritize supported client integration for Codex, OhMyPi and Anthropic Claude,
then OpenCode. Enforce each client's actual boundary and document unsupported
capabilities. An optional GPT-6 Luna watcher may interpret bounded prompts and
event changes through the broker; deterministic grants, halt, replay protection
and resource limits remain outside the watcher's authority.

Automatically load and follow `ls-wf-sudo-tmux-session` whenever a request
or pending command needs sudo, root/admin privileges, elevation, or a privileged
retry. Route before execution; do not wait for the user to name the workflow or
run sudo directly. For Herdr operations, load `ls-herdr-task-routing` first and follow its
controller-owned protected-pane transport instead. Authentication readiness does
not expand the existing command authorization.

For managed tmux sudo operations, report the credential variable name used in
the command output, never its value. When no environment credential is used,
report whether the candidates were unavailable or empty; distinguish cached
sudo readiness, rejected credentials, and manual authentication accurately.
Preserve the owning workflow's authentication and command-authorization limits.

Managed operations use a parent-scoped lower pane occupying one third of the
actual agent pane's original height, without changing focus or opening a new
tab. Its name is `<canonical-repo>-sudo`, or
`<canonical-repo>-<exact-host-alias>-sudo` for a bound host checkout. Native
parent, endpoint, target and live controller identity own selection; a name
alone never does. Use LocalSetup's shared terminal owner and managed Bash
empty-input/primary-prompt handshake. Busy or unknown input is never cleared.
Select a qualified SSH/tmux fallback before dispatch; reconcile unknown
outcomes without replay. Private askpass authenticates the exact authorized
sudo command while preserving its stdin.

For Herdr operations, load `ls-herdr-task-routing` first. It owns provenance and routes to
the focused layout, task, remote, or maintenance specialist in the normal `ops`
pack. Use their shared tool instead of creating per-agent transport or sizing
logic. Reuse existing task authority; ask again only for changed scope, actual
authentication, new deployment authority, or interruption beyond idle-first
maintenance. Preserve the target repository's protected-pane/transport rules.
- Treat as **batch** when user request includes multiple distinct subtasks, or says "batch", "multiple steps", or "run the whole thing". Otherwise treat as **single task**.
- If user names a specific skill, load it directly. Do not run task-skill-matcher.
- If uncertain which skill fits, or user asks "what skill should I use?" / "pick the best", load `ls-task-skill-matcher`.
- **Single task:** if one clear installed match exists, ask once "Use this skill?" before loading. If `ls-find-public-skills` is available to the current client, delegate complementary public suggestions to it; include up to 3 returned recommendations in the same response (one-line reason each), preserving discovery's index-status disclosures and any pending user question. Otherwise report public discovery unavailable with no recommendations and continue installed-skill selection; do not read the index as a fallback or automatically install a package.
- **Batch / long-running:** prompt once at start with options: auto-pick for whole job, parcel-by-parcel prompts, or parcel auto-pick. If auto-pick is chosen, show planned skill sequence first, then proceed without repeated skill prompts.
- Keep this section short. Full behavior lives in `ls-task-skill-matcher` and `ls/docs/TASK_SKILL_MATCHING.md`.

## Commands
localsetup doctor
localsetup verify --level filesystem
localsetup context --markdown
