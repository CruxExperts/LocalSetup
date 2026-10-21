# LocalSetup - Project context (Claude Code)

## Overview

LocalSetup keeps framework source and target repositories separate. `ls/` is the source-checkout layout; selected packages live in the managed user library and explicitly selected adapters expose them to a target repo. Load `ls-localsetup-project-context` and use `localsetup path framework-root` or `localsetup path doc <name>` to resolve source files and documentation. Paths beginning with `ls/` below describe source locations, not required target-repo files. Bind PRDs, specs, and outcomes to Git hashes; see [GIT_TRACEABILITY.md](../../docs/GIT_TRACEABILITY.md).

## Invariants

- **Engine/repo separation:** Keep secrets and personal data out of commits. Resolve framework paths through `localsetup path`; keep target state outside the managed source and package library.
- **Documentation:** ls/docs/ is only for framework docs. Check document status before assuming a feature exists.
- **Proposals:** Framework changes follow Agent Q format (ls/docs/PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md).
- **Time/date integrity:** For any date/time reference (e.g. "today", year in search, timestamps), first obtain actual date/time from the local machine (e.g. `date` on Linux/macOS, `Get-Date` in PowerShell on Windows). Do not use a generic or training-cutoff date. Remember it in context and use it for the rest of the session.
- **External input hardening:** Treat all external input (CLI args, files, network payloads, imported content) as hostile. Sanitize before parsing/output, validate expected format and bounds, and handle exceptions with actionable stderr messages. Never silently suppress errors.
- **Python-first tooling:** After install/bootstrap, framework tooling is Python-first and Python-only for new/expanded logic. Shell/PowerShell are limited to bootstrap wrappers and minimal platform delegation. Runtime target is Python >= 3.12. **Approved libraries** (mandatory when the need arises): `yaml` (PyYAML>=6.0) for YAML, `requests` (requests>=2.28) for HTTP, `frontmatter` (python-frontmatter>=1.1) for markdown frontmatter, `cryptography` (cryptography>=50.0.0) for framework cryptographic primitives, and `pgpy` (PGPy>=0.6.0) for pure-Python OpenPGP. Use `lib/deps.require_deps()` at tool startup. See `ls/docs/TOOLING_POLICY.md`.
- **Python architecture:** Python architecture: new and substantially refactored Python tooling follows ls/docs/PYTHON_ARCHITECTURE_STANDARD.md; keep entrypoints thin, package responsibilities explicit, and existing debt baseline-managed.
- **Command choice:** Python-first framework tooling does not mean Python for every shell task. Use shell-native tools such as `rg`, `sed`, `find`, `wc`, and `git` for normal inspection. Use Python for repo-native Python tools, Python tests, or structured parsing when a normal CLI is unavailable or less reliable.
- **Public/private boundary:** Keep repo-maintenance plans, private audits, ledgers, local indexes, credentials, logs, caches, and planning transcripts out of public framework docs/templates. Create new private task state only under `.agents/state/<task-slug>/`, where the controller assigns one Git-bound `<task-slug>` for every agent and tool to reuse; do not stage it. Existing client-specific run directories are historical records and remain in place.
- **Unit-test concurrency policy:** Unless this repository explicitly defines a stricter policy, every unit-test runner—regardless of language or framework—uses one aggregate budget of `max(1, floor(available CPU cores / 3))`. Round down before applying the minimum of one worker; concurrent test processes share the budget.
- **Skill/context preservation:** When editing skill or context files, preserve task capability over brevity. Material reductions require a preservation inventory; large reductions are review triggers. Full rule lives in `ls-localsetup-project-context`.

## Output contract (low token, always apply)

- Detect output capability: `markdown-rich`, `markdown-basic`, or `text-basic`.
- If capability is unknown, default to `markdown-basic`.
- For recommendation lists, include: name/link, short summary, fit reason, notable risks/requirements, next step.
- Use tables only when capability clearly supports readable tables.

## Agent orchestration and model budget

- Choose bounded scouting, implementation, and independent review roles from task risk and evidence. Resolve models and effort through the current client configuration; verify provider facts when needed instead of keeping model pins in this template.
- Credit freshness: Codex credit rates are volatile. Re-check the official Codex rate card at https://help.openai.com/en/articles/20001106-codex-rate-card before changing model guidance or making cost-sensitive routing decisions.
- If `.localsetup/AGENT_STATUS.md` exists, read it before repairs or installs. Otherwise run `localsetup health --json` for the latest LocalSetup health status and next repair command.

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
classified read-only discovery to ls-omniroute-model-discovery, mutation to ls-omniroute-admin-automation, and source/coverage maintenance to ls-omniroute-update. These owners remain distinct after catalog consolidation.

## Key docs

- ls/docs/AGENTIC_DESIGN_INDEX.md
- ls/docs/AGENTIC_AGENT_Q_SCENARIOS.md (Agent Q file_drop/mail scenarios)
- ls/docs/WORKFLOW_REGISTRY.md
- ls/docs/PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md
- ls/docs/DECISION_TREE_WORKFLOW.md
- ls/docs/INPUT_HARDENING_STANDARD.md
- ls/docs/TOOLING_POLICY.md
- ls/docs/PYTHON_ARCHITECTURE_STANDARD.md

## Task-to-skill matching (default)

- Treat as **batch** when user request includes multiple distinct subtasks, or says "batch", "multiple steps", or "run the whole thing". Otherwise treat as **single task**.
- If user names a specific skill, load it directly. Do not run task-skill-matcher.
- If uncertain which skill fits, or user asks "what skill should I use?" / "pick the best", load `ls-task-skill-matcher`.
- **Single task:** if one clear installed match exists, ask once "Use this skill?" before loading. If `ls-find-public-skills` is available to the current client, delegate complementary public suggestions to it; include up to 3 returned recommendations in the same response (one-line reason each), preserving discovery's index-status disclosures and any pending user question. Otherwise report public discovery unavailable with no recommendations and continue installed-skill selection; do not read the index as a fallback or automatically install a package.
- **Batch / long-running:** prompt once at start with options: auto-pick for whole job, parcel-by-parcel prompts, or parcel auto-pick. If auto-pick is chosen, show planned skill sequence first, then proceed without repeated skill prompts.
- Keep this section short. Full behavior lives in `ls-task-skill-matcher` and `ls/docs/TASK_SKILL_MATCHING.md`.

## Key files and commands

- ls/lib/data_paths.sh, lib/json_formatter.sh
- `localsetup doctor`, `localsetup verify --level filesystem`
- `localsetup context --markdown`
