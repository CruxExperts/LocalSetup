# LocalSetup

<p align="center">
  <img src="assets/localsetup-readme-hero.png" alt="LocalSetup: portable skills and deliberate workflows, with one package library serving selected agent hosts" width="960">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://agentskills.io/specification"><img src="https://img.shields.io/badge/Agent%20Skills-compatible-2ea44f" alt="Agent Skills compatible"></a>
  <a href="ls/docs/PLATFORM_REGISTRY.md"><img src="https://img.shields.io/badge/platforms-cursor%20%7C%20claude--code%20%7C%20codex%20%7C%20openclaw%20%7C%20kilo%20%7C%20opencode-1f6feb" alt="Supported platforms"></a>
</p>

**Version:** 4.25.0<br>

**LocalSetup gives coding agents a repo-local operating layer.**

Keep reusable agent skills in one managed library, expose the packages each project needs, and review the instructions alongside your code.

LocalSetup provides capability skills, executable workflow packages, explicit adapter selection, install planning, verification, and rollback records. It supports Cursor, Claude Code, OpenAI Codex CLI, OpenClaw, Kilo, and OpenCode. Custom project skills can live beside managed packages.

Start with the [quickstart](ls/docs/QUICKSTART.md) or browse the [documentation](ls/docs/README.md). The [latest published release](https://github.com/CruxExperts/localsetup/releases/latest) provides release notes and downloads.

<!-- release-summary:start -->
## What's new in 5.6.2

LocalSetup 5.6.2 introduces explicit OpenPGP authority and protected key lifecycle operations, authenticated Agent Q transport, and bounded verified file reading in LSCli. The Agent Q registry and SDK file-read response change in this major release; existing identities and historical files require an intentional migration.

- **OpenPGP identity and recovery:** Inspect and generate profile-bound keys, keep passphrases in ENV or Envman references, create ciphertext-only protected backups, and restore into an isolated private home. Recovery authority is independent of possession of an old operational key.
- **Authenticated authority transitions:** Candidate and existing owner proofs, publisher rotation, replay protection, scheduled overlap, revocation, and independent recovery use a persistent private authority store. Verification alone does not activate a key.
- **Sealed Agent Q transport:** File drops and mail carry one opaque signed and encrypted envelope. Inbound processing selects the peer and full fingerprint from private authority before decrypting, records acceptance before queue promotion, and rejects legacy traffic from normal ingest.
- **Verified file pages:** LSCli and its SDK return bounded JSON pages for granted reads, including sealed ciphertext after verification. Each continuation rechecks source and authority, and disclosure remains an explicit grant.
- **Installed discovery and workflows:** The runtime reports shipped OpenPGP, reader, Agent Q and workflow contracts from the selected installed release. The lifecycle and compact worker workflows ship as guidance; installation neither generates keys nor enrolls trust.
- **Release verification:** The signed release path checks committed documentation and version artifacts, runs framework validation, verifies the signed tag and prepares a draft before publication.

See the [5.6.2 release guide](ls/docs/releases/5.6.2.md) for compatibility, updating, and verification.
<!-- release-summary:end -->

The [4.4.0 guide](ls/docs/releases/4.4.0.md) remains available as release history.

## The short version

LocalSetup packages:

- Global framework source under `~/.local/share/localsetup/source` for installed users; source checkouts keep `ls/` for contributors
- 105 shipped capability skills plus 18 first-class workflow packages for debugging, testing, PR review, infrastructure, docs, git recovery, skill import, security vetting, context indexing, TypeScript code quality, opt-in harness automation, OmniRoute integration, and agent workflow control
- Cross-platform adapters for Cursor, Claude Code, OpenAI Codex CLI, OpenClaw, Kilo, and OpenCode
- Agent Skills-compatible `SKILL.md` packages that can be imported, normalized, vetted, installed, and reused
- Workflow packages under `ls/workflows/` that stay executable as skills while carrying LocalSetup `workflow.yaml` metadata for aliases, gates, dependencies, and generated registries
- Documentation alignment tooling that inventories source-owned docs, maps code truth, audits generated facts, and refreshes supported public/generated surfaces
- Context-index tooling for vector-first SQLite retrieval, freshness checks, agent preflight, MCP configuration support, and machine-readable worklists
- A Python-first installer with preflight, planning, verification, rollback metadata, and generated docs sync
- Opt-in harness automation for Codex heartbeat and repo-finalizer workflows, plus human-in-the-loop operations for risky commands, remote/server work, and sudo-aware tmux sessions

That means your agent setup travels with the repo, survives context resets, and can be reviewed like code.

## How it fits together

<p align="center">
  <img src="assets/localsetup-architecture.png" alt="Framework source flows through the LocalSetup CLI into a managed library and selected project adapters; custom project skills stay in place" width="960">
</p>

The registered LocalSetup source checkout is the canonical framework source. The installer resolves configuration, creates the managed package library for skills and workflow packages, attaches only explicitly selected target adapter paths, writes target lock/report metadata under `.localsetup/`, and records an install journal under `.localsetup/install-journal/`. Consuming repos do not receive a copied `ls/` by default.

## Skills and workflow packages

LocalSetup makes one important distinction explicit:

| Package type | Source root | Runtime shape | Use it for |
|---|---|---|---|
| Capability skill | `ls/skills/ls-*` | Agent Skills `SKILL.md` package | A reusable capability such as debugging, testing, skill import, PR review, service triage, or versioning. |
| Workflow package | `ls/workflows/ls-workflow-*` | Agent Skills `SKILL.md` package plus `workflow.yaml` | A named orchestration flow with aliases, required skills, gates, phases, validation, and expected outputs. |

Both package types install into `~/.local/share/localsetup/packages`, so agent hosts can invoke them through explicitly attached adapter paths. The split keeps portable skills clean while making workflow orchestration auditable and generated from source manifests.

Start with the [workflow packages guide](ls/docs/WORKFLOW_PACKAGES.md) for usage and the [workflow standard](ls/docs/WORKFLOW_STANDARD.md) for authoring rules.

## Current snapshot

<!-- facts-block:start -->
| Fact | Value |
|---|---|
| Current version | `4.25.0` |
| Supported platforms | `codex, claude-code, cursor, kilo, opencode, openclaw, github-copilot-cli, github-copilot-vscode, cline-cli, cline-vscode, amp-cli, goose-cli, pi-cli, hermes-agent, qwen-code-cli, kimi-cli, factory-droid, antigravity-app, gemini-cli, omp-cli` |
| Shipped skills | `105` |
| Workflow packages | `18` |
| Source | `ls/docs/_generated/facts.json` |
<!-- facts-block:end -->

## 60-second quickstart

Global bootstrap from any directory on Linux, macOS, or WSL2:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s --
```

This opens an interactive terminal wizard. The raw bootstrap wrapper checks the latest stable GitHub release, falls back to the latest stable-looking tag when the release API is unavailable, creates or refreshes the managed source checkout from that ref, explains what will be changed, lets you choose the managed package-library baseline and optional repo adapters, and asks for confirmation before applying.

Every wizard step shows the same shortcuts before you answer: `Enter number(s) | d details | b back | q quit | ? help`. Detailed mode is on by default, so menu rows explain what each choice does, when to choose it, and the tradeoff. Press `d` at any prompt for compact mode; compact mode still keeps the one-line summary for each option. Use `q` or Ctrl-C to quit; bare Esc is ignored so terminal arrow-key sequences do not cancel selection.

The wizard stays dependency-free and uses standard terminal controls. Multi-select screens use a checkbox UI in real terminals: move with arrows or `j`/`k`, press `Space` to toggle global packs, repo-visible packs, or platforms, and press `Enter` to accept. Scripted or non-TTY streams fall back to line-mode comma-separated choices. It uses semantic color and simple status glyphs only when the terminal supports them; `NO_COLOR`, `TERM=dumb`, non-TTY output, `--no-color`, and `--color never` keep output free of ANSI color. Use `--color always` or `--glyphs unicode` only for an interactive terminal where you explicitly want that rendering; `--glyphs ascii` keeps portable labels such as `[OK]`, `[WARN]`, and `[FAIL]`.

The legacy public form still opens the same wizard when a terminal is available:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --yes --tools codex
```

The public command is release-backed even though the small wrapper is downloaded from `main`: managed bootstrap installs resolve the latest non-draft, non-prerelease GitHub release tag before cloning or refreshing `~/.local/share/localsetup/source`, with a stable-tag fallback when release lookup is unavailable. Set `LOCALSETUP_BOOTSTRAP_REF` only when you intentionally want an explicit branch, tag, or commit. Explicit `--directory` checkouts are source-authoritative and are never auto-fetched or replaced.

When raw managed bootstrap finds a clean legacy managed source checkout identified by `_localsetup/tools/localsetup.py`, it recognizes and refreshes that checkout to the release-backed modern layout with `ls/tools/localsetup.py`. Before fetching and replacing the checkout, LocalSetup stores a Git rollback bundle and JSON manifest outside the source checkout under `<source-parent>/state/source-migrations` when that location is external, or `~/.local/share/localsetup/state/source-migrations` otherwise. Dirty or untracked source checkouts remain rejected before refresh.

For release verification, download the GitHub release tarball with its `.sha256` checksum and `.cdx.json` SBOM sidecars into the same directory and run:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . verify-release dist/localsetup-v$(cat VERSION).tar.gz
```

Selecting tools in the wizard, or passing `--tools` / `--platforms`, attaches adapters such as `.agents/skills` to the chosen target. For repo-targeted CLI automation with no platform or package selectors, LocalSetup uses auto mode: existing LocalSetup state is inferred and refreshed, safe legacy repairs are applied only when unambiguous, and a brand-new repo gets the `normal` global package baseline with no repo adapter paths. Interactive installs first choose the global package-library baseline, defaulting to `normal` or the prior registry setting. Repo setup is a separate choice; when selected, repo-visible packs default from the target lockfile or repo-detected suggestions.

For automation, opt in explicitly:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --non-interactive --yes
```

Automation mode preserves machine-readable output. Without a terminal, the installer asks you to rerun with a TTY or with `--non-interactive --yes`.
For a managed release bootstrap, this mode synchronizes the source checkout's locked production environment before running the CLI. If `uv` is absent, pass `--install-uv` to opt in to its bootstrap; explicit checkout installs continue to require `--sync-env` when environment synchronization is wanted.


LocalSetup CLI commands emit JSON by default unless a command has an explicit human-readable mode such as `context --markdown`. The `--json` config flag remains available when scripts want to make that output contract explicit.

From a cloned checkout, open the same wizard:

```bash
./install --directory .
```

The local checkout command uses that checkout as the registered source. Like the raw global bootstrap, it installs the managed skill library and creates no repo adapter paths unless you pass `--tools` or `--platforms` or run a repo-targeted auto-mode command. Both paths also create a managed user command at `~/.local/bin/localsetup`. After registration, run LocalSetup from any project:

```bash
localsetup plan --target-directory .
localsetup install --target-directory . --apply
localsetup update --target-directory .
```

When invoked through the managed command, LocalSetup uses the registered framework checkout as the source and the nearest Git worktree root from your current directory as the target. Outside Git, it targets the current directory. Use `--target-directory /path/to/project` to override that target.

`localsetup update` reapplies that registered source; it does not download a newer release. Follow the [update guide](ls/docs/QUICKSTART.md#update) to refresh the source first.

Attach adapters only for the hosts you choose:

```bash
localsetup install --tools codex,kilo --yes
```

Explicit selectors such as `--platforms`, `--global-packs`, `--global-preset`, `--repo-packs`, `--repo-skills`, `--packs`, or `--skills` bypass auto mode and keep the requested shape.

Tune the package footprint by preset, pack, taxonomy class, tag, individual skill, or exclusion:

```bash
localsetup install --tools codex --preset suggested --skill-classes development --skill-tags git --skills ls-context --exclude-skills ls-linux-patcher --yes
```

Presets are `core`, `normal`, `suggested`, `all`, and `custom`. `core` is the compact baseline; `normal` is the fresh global default with bootstrap, core, dev, frontend, architecture, ops, and publishing packs; `suggested` starts with `core` plus repo-detected categories; `all` installs every shipped skill and workflow package; `custom` relies on the packs, classes, tags, and skills you name. `--exclude-skills` removes named skills from the resolved selection unless a selected workflow requires them. The legacy selector flags apply to both the managed global baseline and repo-visible adapter selection for compatibility. Use `--global-packs` / `--global-preset` and `--repo-packs` / `--repo-preset` when you want the managed library to contain a broader baseline than a target repo exposes.

Install every shipped skill and workflow package for Codex, Kilo, and OpenCode, while syncing the uv-managed Python dependency environment:

```bash
./install --directory . --tools codex,kilo,opencode --packs bootstrap,core,dev,frontend,architecture,ops,integrations,publishing,harness,skill-lifecycle,growth-content,specialized --sync-env
```

In symlink mode, LocalSetup writes a scoped marker and managed per-package links inside each selected repo adapter path. The adapter directory itself remains a shared agent surface: custom skills, ordinary files, and repo-local symlinks may live beside LocalSetup-managed entries and must be preserved. Same-name selected package collisions and unsafe symlinks still block before mutation. That means a repo sees only the repo-visible LocalSetup skills and workflow packages even when the global library contains a larger baseline. Portable mode uses the same scoped marker and package list, but copies selected managed packages instead of linking them. See [ls/docs/ADAPTER_OWNERSHIP.md](ls/docs/ADAPTER_OWNERSHIP.md) for the ownership boundary.

Attach a selected adapter to another repo while using this checkout as the source:

```bash
./install --directory /path/to/localsetup --target-directory /path/to/project --tools cursor
```

To convert a repo that may contain old LocalSetup files or adapter paths, start with a dry report and apply only after blockers are clear:

```bash
localsetup convert --tools codex --packs core
localsetup convert --tools codex --packs core --yes
```

Conversion writes a timestamped backup and machine-readable report under `.localsetup/backups/conversion-*`, archives known managed or legacy LocalSetup artifacts, backs up and removes stale target `ls/` folders, blocks ambiguous unmanaged content, installs selected adapters, and verifies the result.

Windows support is WSL2-only in the current framework. Open WSL2, change to the repo path, and run the Bash installer.

Full install docs: [ls/docs/QUICKSTART.md](ls/docs/QUICKSTART.md) and [ls/docs/MULTI_PLATFORM_INSTALL.md](ls/docs/MULTI_PLATFORM_INSTALL.md).
Copy-paste command reference: [ls/docs/COMMAND_REFERENCE.md](ls/docs/COMMAND_REFERENCE.md).

Opt-in harness automation is documented separately because normal installs never schedule autonomous work. See [ls/docs/HARNESS_AUTOMATION.md](ls/docs/HARNESS_AUTOMATION.md) for `localsetup harness codex-heartbeat plan/init/enable/status/budget/run/disable` and the [typed LSCli profile and controller accounting](ls/skills/ls-codex-heartbeat/references/config.md#typed-lscli-profile).

[LSCli](ls/docs/LSCLI.md) is the integrated CLI for LocalSetup (LS), invoked as
`lscli` or `localsetup agent`. It provides explicit offline setup, protected coding
runs, session continuation/recovery, branches and compaction;
`localsetup llm complete` provides tool-free structured completion. Profiles and
authority are explicit, and coding requires a qualified sandbox/resource backend.
The [command reference](ls/docs/COMMAND_REFERENCE.md#lscli-and-tool-free-completion)
links the full contracts. Installed candidate evidence does not qualify every
provider, host or eventual published artifact.

## 10 reasons to use LocalSetup

1. **Your agent context becomes code.** Instructions, skills, workflows, platform manifests, and docs live in the repo, so changes are visible in git instead of hidden in a local profile or a forgotten prompt.
2. **One skill library reaches multiple agent hosts.** When selected with `--tools` or `--platforms`, the shipped adapters let Cursor, Claude Code, OpenAI Codex CLI, OpenClaw, Kilo, and OpenCode attach to the same managed LocalSetup skill library.
3. **It leans into portable skill packaging.** Skills use spec-compatible `SKILL.md` frontmatter, which makes them easier to import, export, normalize, and share across ecosystems that understand the package shape.
4. **It tackles the trust gap directly.** The framework pushes agents toward repeatable workflows, explicit verification, documented assumptions, and human gates instead of one-off "looks good" responses.
5. **It treats skill imports as supply-chain events.** External skills are discovered, validated, security-screened, summarized, and normalized before they become part of your library.
6. **It helps with the work developers actually hand agents.** Debugging, tests, PR review, codebase navigation, docs cleanup, git recovery, MCP building, Linux service triage, patching, and release chores are covered out of the box.
7. **It has safety rails for real machines.** Server and operations workflows route through tmux, sudo probing, backup/safety guidance, and explicit approval points for risky actions.
8. **It gives long-running work a shape.** First-class workflow packages, decision trees, PRD queues, Agent Q handoffs, generated registries, and outcome templates make multi-step agent work easier to restart, audit, and delegate.
9. **It makes installs reversible.** The LocalSetup installer plans, applies, verifies, writes lock/registry metadata, supports adapter detach, and can roll back managed paths without treating generated adapter output as source.
10. **It keeps releases tidy.** Version sync, generated facts, strict manifest schemas, checksum/SBOM sidecars, framework audit, and Conventional Commit release tooling reduce the drift that makes public repos feel abandoned.

## Shipped packages worth starting with

These are not toy prompts. They are practical skills and workflows from the shipped library.

| Package | Why it matters |
|---|---|
| `ls-agentlens` | Helps agents explore larger codebases through structured navigation instead of blind file-hopping. |
| `ls-debug-pro` | Gives debugging a repeatable method across Node, Python, Swift, network issues, and git bisect. |
| `ls-test-runner` | Guides test creation and execution across pytest, Jest, Vitest, Playwright, and XCTest. |
| `ls-typescript-code-quality` | Guides TypeScript, TSX, tsconfig, typed linting, Node TypeScript scripts, and framework-heavy TypeScript changes. |
| `ls-pr-reviewer` | Turns PR review into a structured risk hunt: diff analysis, security concerns, test gaps, and style issues. |
| `ls-documentation-alignment` | Audits docs against implementation truth, generated facts, assets, and source-owned documentation surfaces. |
| `ls-github-publishing-workflow` | Packages public GitHub readiness checks around docs, versioning, security policy, release evidence, and repository settings. |
| `ls-mcp-builder` | Helps build high-quality MCP servers for current agent/tool interoperability workflows. |
| `ls-skill-importer` | Imports skills from URLs or local paths with discovery, validation, security screening, and summaries. |
| `ls-skill-vetter` | Reviews third-party skills as untrusted inputs before they join your agent environment. |
| `ls-codex-heartbeat` | Initializes and runs opt-in heartbeat checks with transaction-safe artifacts and explicit cron activation. |
| `ls-keepass-secrets` | Validates logical secret-ID maps, configuration, and reference syntax. Its KeePassXC guard never accesses real vaults; fake is test/example-only. |
| `ls-cloudflare-dns` | Uses the `cf` CLI for Cloudflare zones, DNS records, settings, DNSSEC, scans, imports, exports, batches, analytics, and zone transfers. |
| `ls-workflow-ops-tmux-session` | Keeps human-controlled server operations visible, resumable, and sudo-aware. |
| `ls-workflow-tmux-terminal-mode` | Manages tmux-default terminal mode setup and read-only health checks. |

See the generated catalogs for all shipped skills and workflows: [ls/docs/SKILLS.md](ls/docs/SKILLS.md) and [ls/docs/WORKFLOW_QUICK_REF.md](ls/docs/WORKFLOW_QUICK_REF.md).

## Install lifecycle

<p align="center">
  <img src="assets/localsetup-install-lifecycle.png" alt="Inspect the target, plan changes, confirm scope, apply, and verify; rollback restores recorded managed paths" width="960">
</p>

The Bash wrapper stays thin. The Python CLI handles preflight, dependency setup, adapter planning, managed skill installation, verification, generated docs, packaging, and rollback.

Useful commands:

```bash
localsetup doctor
localsetup plan --target-directory .
localsetup install --target-directory . --apply
localsetup update --target-directory .
localsetup doctor repair --target-directory .
localsetup verify --tools codex --level filesystem
localsetup diff --tools codex
localsetup skill search context
localsetup skill info ls-framework-audit
localsetup why --packs core
localsetup graph
localsetup adopt --target-directory .
localsetup sbom --out /tmp/localsetup-source.cdx.json
uv run --locked python ls/tools/localsetup.py --source-root . context --markdown
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/localsetup.py --source-root . audit-global-first
uv run --locked python ls/tools/localsetup.py --source-root . rollback
```

Use `--trace-json /path/to/events.jsonl` with `install`, `verify`, or `doctor` to append local JSONL trace events for automation review.

For a complete option table, see the [command reference](ls/docs/COMMAND_REFERENCE.md).

`doctor` reports the uv-managed source checkout environment. `doctor repair` emits a dry-run JSON repair report for legacy or partial target repos, and applies only low-ambiguity LocalSetup-owned repairs when rerun with `--yes`. Repair now treats workflow packages as first-class inferred packages, preserves benign adapter content by default, and can emit compact handoff prompts with `--agent-prompt` or `--emit-agent-prompt`. Adapter-shaped directories such as `.codex/skills` and `.agents/skills` are shared surfaces, not LocalSetup-exclusive directories; repair must preserve custom skills, ordinary files, and repo-local symlinks in place while mutating only proven LocalSetup-managed entries. Clean legacy `ls` framework trees are removed only after backup and, when tracked, `git rm --cached`; protected source checkouts still allow safe adapter and lock refreshes, while custom, dirty, symlinked, unsafe, or content-divergent `ls` trees are preserved for migration planning. If `doctor` sees an old `~/.local/share/localsetup/venv` from earlier releases, it reports that legacy venv as ignored and gives a repair hint instead of trying to execute it.

Release note for this repair behavior: `.localsetup/lock.json` is managed repo state and should stay visible to Git. Runtime summaries, journals, backups, health state, and context-index runtime data are local runtime state and are added to `.git/info/exclude`.

## What LocalSetup is solving

Agent tooling moves quickly, but the hard parts stay stubbornly practical. Teams still need context that survives across sessions, standards that work across tools, safety around imported instructions, and workflows that can be resumed by another human or agent without archaeology.

LocalSetup's opinion is simple: keep the agent operating model close to the code. Make it installable. Make it reviewable. Make it boring enough to trust.

The design follows a few durable pressures instead of chasing market snapshots:

- Agents need repo-owned context, not only session memory.
- Imported instructions and skills need supply-chain treatment before they are trusted.
- Tool and data access should be explicit, least-privilege, and reviewable.
- Long-running work needs checkpoints, validation evidence, and handoff notes.
- Interoperability work such as [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro) and [Agent Skills](https://agentskills.io/specification) is useful, but LocalSetup keeps those integrations source-owned and replaceable.

## Requirements

- Python `>= 3.12`
- Bash on Linux, macOS, or WSL2
- Git and network access to GitHub for raw bootstrap, unless installing from a local clone
- Required for dependency sync: `uv`, with dependency intent in `pyproject.toml` and the committed `uv.lock`.
- Recommended: `rg`; GitHub/network access is needed for raw bootstrap unless installing from a local clone.

Use uv-managed dependency setup instead of system Python changes:

```bash
./install --directory . --sync-env
```

The default dependency mode is report-only. It can warn about corrupt legacy LocalSetup environments, but it does not move files. Explicit sync paths such as `--sync-env` may quarantine only LocalSetup-owned corrupt environments and then let uv rebuild the source checkout `.venv`; a target project's own `.venv` is never modified.

## Read more

- [Framework docs index](ls/docs/README.md)
- [Framework README](ls/README.md)
- [Feature catalog](ls/docs/FEATURES.md)
- [LSCli commands and runtime setup](ls/docs/LSCLI.md)
- [SDK source, dependency and artifact ownership](ls/docs/SDK_FORK.md)
- [Platform registry](ls/docs/PLATFORM_REGISTRY.md)
- [Harness automation](ls/docs/HARNESS_AUTOMATION.md)
- [Workflow packages](ls/docs/WORKFLOW_PACKAGES.md)
- [Workflow standard](ls/docs/WORKFLOW_STANDARD.md)
- [Workflow registry](ls/docs/WORKFLOW_REGISTRY.md)
- [Skill importing](ls/docs/SKILL_IMPORTING.md)
- [Skill discovery](ls/docs/SKILL_DISCOVERY.md)
- [Contributing](CONTRIBUTING.md)
- [Support](SUPPORT.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Security](SECURITY.md)

## License

LocalSetup is released under the [MIT License](LICENSE).

## Community and Support

For bugs, use the bug report form and include the LocalSetup version, platform ID, command, expected result, actual result, and validation output. For feature requests, use the feature form and name the affected skill, workflow package, platform, or docs area. For version-sync, generated-doc, publish, or package-artifact problems, use the maintenance form.

Use [GitHub Discussions](https://github.com/CruxExperts/localsetup/discussions) for usage questions and early design conversation. Report security-sensitive issues through private vulnerability reporting when available; otherwise open a minimal public issue asking for a secure contact without details.
