---
status: ACTIVE
version: 5.7
owner_skill: ls-framework-compliance
---

# Multi-platform install (LocalSetup)

**Purpose:** How to install LocalSetup for each supported AI agent platform. Supported platforms are listed in `ls/config/platforms.yaml` and summarized in [_generated/platform-adapters.md](_generated/platform-adapters.md). Same framework; explicitly selected platform adapter paths point at a shared managed package library. For a copy-paste option table, see [Command reference](COMMAND_REFERENCE.md).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../assets/localsetup-install-lifecycle-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="../../assets/localsetup-install-lifecycle.png">
    <img src="../../assets/localsetup-install-lifecycle.png" alt="LocalSetup install flow: inspect, plan and confirm scope, apply, verify, and restore recorded managed paths with rollback." width="960">
  </picture>
</p>

## Platform detection and script selection

- **Linux / macOS:** Use `./install`, which opens the interactive wizard by default. Automation uses `./install --non-interactive --yes`.
- **Windows:** LocalSetup supports Windows through WSL2 only. Native PowerShell installation surfaces are removed.
- **Git Bash (or MSYS/Cygwin) on Windows:** Open WSL2 and run the Bash installer from there.

## Install command

### Linux / macOS (Bash)

Global bootstrap from any directory:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s --
```

This opens the interactive terminal wizard. The raw bootstrap wrapper checks the latest stable GitHub release, falls back to the latest stable-looking tag when the release API is unavailable, creates or refreshes the managed source checkout from that ref, and shows source/release status, global package-library selection, optional repo setup, selected platforms and packs, warnings, blockers, and planned actions before asking for final confirmation.

Single-choice wizard prompts show `Enter number(s) | d details | b back | q quit | ? help`. Detailed mode is on by default, so source, package-library, repo setup, and adapter choices explain what they do, when to pick them, and their tradeoffs. Multi-select prompts use checkbox controls in real terminals: move with arrows or `j`/`k`, press `Space` to toggle global packs, repo-visible packs, or platforms, and press `Enter` to accept. Scripted streams fall back to comma-separated line input. Press `d` to toggle compact mode; compact mode still shows one-line summaries for each option. Use `q` or Ctrl-C to quit; bare Esc is ignored so terminal arrow-key sequences do not cancel selection.

The visual layer remains standard-library only. `--color auto` enables ANSI color only for capable interactive terminals, while `--no-color` and `--color never` force output free of ANSI color. `--glyphs auto` uses simple Unicode status hints only on UTF-8 interactive terminals; `--glyphs ascii` keeps portable labels like `[OK]`, `[WARN]`, and `[FAIL]`. Scripted installs should continue to use `--non-interactive --yes`, which preserves machine-readable output instead of wizard screens.

The legacy public form still opens the wizard when a terminal is available:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --yes
```

Note: `sudo curl ... | bash -s --` only elevates curl; install and deploy run as the current user. For a full install as root: `curl -sSL <url> -o /tmp/install.sh && sudo bash /tmp/install.sh`.

Automation global-only install:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --non-interactive --yes
```

Automation mode creates or refreshes a managed LocalSetup source checkout at `~/.local/share/localsetup/source` from the latest non-draft, non-prerelease GitHub release, with a stable-tag fallback when release lookup is unavailable. It installs the managed LocalSetup package library, registers `~/.local/bin/localsetup`, creates no repo adapter paths unless selected, and preserves machine-readable output. If release lookup fails and a clean managed source already exists, the wrapper warns and continues from that existing source; if no managed source exists, it fails with an actionable message. If no terminal is available and `--non-interactive --yes` is not provided, the installer exits with an actionable message.

When raw managed bootstrap finds a clean legacy managed source checkout identified by `_localsetup/tools/localsetup.py`, it recognizes and refreshes that checkout to the release-backed modern layout with `ls/tools/localsetup.py`. Before fetching and replacing the checkout, LocalSetup stores a Git rollback bundle and JSON manifest outside the source checkout under `<source-parent>/state/source-migrations` when that location is external, or `~/.local/share/localsetup/state/source-migrations` otherwise. Dirty or untracked source checkouts remain rejected before refresh.

Selected platforms for the current repo after global bootstrap:

```bash
localsetup install --tools cursor,claude-code --yes
```

Or bootstrap LocalSetup and attach selected adapters to the current directory in one raw command:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --tools cursor,claude-code
```

When the raw installer is run with `--tools` or `--platforms` and no explicit `--target-directory`, the wizard defaults adapters to the current directory where you launched the command. Without selected tools or platforms and without a target directory, the install is global-library-only. With an explicit target directory and no selector flags, non-interactive automation uses auto mode: existing LocalSetup state is inferred and refreshed, safe legacy repairs are applied only when unambiguous, and brand-new repos get the `normal` global baseline without adapter paths.

Repo-targeted auto mode:

```bash
localsetup plan --target-directory .
localsetup install --target-directory . --apply
localsetup update --target-directory .
```

`--tools` is a compatibility alias for the current `--platforms` selector.

Full local installation for the Codex, Kilo, and OpenCode adapters:

```bash
./install --directory . --tools codex,kilo,opencode --packs bootstrap,core,dev,frontend,architecture,ops,integrations,publishing,harness,skill-lifecycle,growth-content,specialized --sync-env
```

That command installs every declared skill and workflow pack, attaches only `.agents/skills`, `.kilo/skills`, and `.opencode/skills`, and syncs the uv-managed source checkout `.venv` dependency environment.

The `harness` pack only installs autonomous-harness capability. It does not create `HEARTBEAT.md`, `config/codex_heartbeat.yaml`, `cron/manifest.yaml`, or `.localsetup/state/codex-heartbeat/`. Activate a target repo later with `localsetup harness codex-heartbeat init` and `localsetup harness codex-heartbeat enable`; see [HARNESS_AUTOMATION.md](HARNESS_AUTOMATION.md).

For a smaller footprint, select by preset, taxonomy class, tag, individual skill, or exclusion:

```bash
localsetup install --tools codex --preset suggested --skill-classes development --skill-tags git --skills ls-context --exclude-skills ls-linux-patcher --yes
```

Presets are `core`, `normal`, `suggested`, `all`, and `custom`. Fresh selector-free installs default the managed global baseline to `normal`; `core` remains the compact baseline when selected explicitly. Interactive installs first choose the global package-library baseline, defaulting to `normal` or the prior registry setting. Repo setup is a separate choice; when selected, repo-visible packs default from the target lockfile or repo-detected suggestions. `--packs`, `--skill-classes`, `--skill-tags`, `--skills`, and `--workflows` are additive; `--exclude-skills` removes named packages unless a selected workflow requires them. The legacy selector flags apply to both the managed global baseline and repo-visible adapter selection for compatibility. Use `--global-packs` / `--global-preset` / `--global-workflows` and `--repo-packs` / `--repo-preset` / `--repo-workflows` when you want the managed package library to contain a broader baseline than the target repo exposes.

Attach a selected adapter to another repo or directory:

```bash
./install --directory /path/to/localsetup --target-directory /path/to/project --tools cursor
```

The managed global command separates source from target. The source is the registered LocalSetup checkout recorded in `~/.local/bin/localsetup`; the target is the nearest Git worktree root from the command CWD, or the exact CWD outside Git. `--target-directory` always overrides detection.

Repo conversion uses the same target rules:

```bash
localsetup convert --tools codex --packs core
localsetup convert --tools codex --packs core --yes
```

The dry report lists artifacts and blockers. Apply mode writes `.localsetup/backups/conversion-*/conversion-report.json`, archives known managed or legacy LocalSetup content, refuses ambiguous unmanaged content, removes stale target `ls/` copies, installs selected adapters, and verifies.

### Windows (WSL2)

```bash
wsl
cd /path/to/repo
./install --directory .
```

## Options

- `--directory PATH` / `-Directory PATH`  - LocalSetup source checkout containing `ls/`. Defaults to `.` when run from a checkout; otherwise the raw Bash installer creates or refreshes `~/.local/share/localsetup/source`.
- `--target-directory PATH`  - Directory where selected repo adapter links and `.localsetup/lock.json` are written. Defaults to the source checkout for explicit local installs and to the caller's current directory for raw bootstrap installs with selected platforms. In non-interactive automation without selectors, enables auto mode for that target.
- `--tools LIST`  - Compatibility alias for comma-separated platforms: cursor, claude-code, codex, openclaw, kilo, opencode
- `--platforms LIST`  - Space-separated platform ids. Explicit values override auto mode.
- `--preset NAME`  - Skill selection preset: core, normal, suggested, all, or custom.
- `--packs LIST`  - Comma-separated skill/workflow packs to install.
- `--skills LIST`  - Comma-separated individual skill ids to include.
- `--skill-classes LIST`  - Comma-separated taxonomy classes to include.
- `--skill-tags LIST`  - Comma-separated taxonomy tags to include.
- `--exclude-skills LIST`  - Comma-separated individual skill ids to remove from the resolved selection unless a selected workflow requires them.
- `--workflows LIST`  - Comma-separated workflow package ids or aliases to include.
- `--global-preset NAME`, `--global-packs LIST`, `--global-skills LIST`, `--global-workflows LIST`, `--global-skill-classes LIST`, `--global-skill-tags LIST`, `--global-exclude-skills LIST`  - Selector aliases for the managed package-library baseline.
- `--repo-preset NAME`, `--repo-packs LIST`, `--repo-skills LIST`, `--repo-workflows LIST`, `--repo-skill-classes LIST`, `--repo-skill-tags LIST`, `--repo-exclude-skills LIST`  - Selector aliases for packages exposed through repo adapter paths.
- `--yes`  - Legacy accepted flag for interactive installs. For automation, combine with `--non-interactive`.
- `--non-interactive`  - Automation mode. Requires `--yes` and preserves machine-readable output.
- `--global`  - Removed legacy flag; installs the managed home library by default and exits with an explicit error if this flag is supplied.
- `--sync-env`  - Sync the uv-managed source checkout `.venv` from `pyproject.toml` and `uv.lock`; quarantines corrupt LocalSetup-owned legacy environments before rebuild.
- `--install-deps`  - Deprecated migration alias for `--sync-env`.
- `--install-uv` / `--no-install-uv`  - Explicitly allow or forbid uv bootstrap when `--sync-env` is requested.
- `--offline`  - Run uv dependency sync in offline/cache-only mode.
- `--no-register-shell`  - Skip creating/updating `~/.local/bin/localsetup`
- `--help`  - Print usage and exit

## Wizard choice guide

The wizard keeps the install portable and dependency-free; it uses plain terminal output with optional ANSI styling and no curses/Textual dependency. Single-choice prompts can be selected by number or label. Multi-select prompts use spacebar toggles in real terminals and comma-separated values in scripted fallback mode.

- **Source and release:** shows the source checkout, current source ref, and latest upstream release result when the raw managed bootstrap path performed a release check.
- **Global package library:** `normal` is the suggested fresh baseline for normal use. It combines bootstrap, core, dev, frontend, architecture, ops, and publishing packs. `bootstrap` adds agent-team startup and audit workflows. `dev` adds code, docs, git, test, repo repair, and engineering workflow guidance. `frontend` adds UI, design, accessibility, React, Next.js, Tailwind, and browser-debugging coverage. `architecture` adds system design, diagrams, deployment readiness, incident response, and tech-debt planning. `ops` adds server and maintenance workflows. `integrations` adds external system connectors and provider/API guidance. `publishing` adds release and public-repo support. `harness` adds opt-in autonomous harness capability without activating it. `skill-lifecycle` adds skill authoring, import, vetting, and bundle inventory workflows. `growth-content` adds marketing, CRO, SEO/GEO, lifecycle email, and writing/editing support. `specialized` adds narrow human-review, Kilo, and umbrella workflow support. `experimental` is reserved for future incubation and is currently empty.
- **Repo setup:** `No repo setup` refreshes the managed package library without repo adapter paths. `Current directory` prepares the directory you launched from. `Another target directory` prepares a different repo while using this checkout as the source.
- **Repo adapters:** each selected platform shows the adapter path it writes, such as `.agents/skills` for Codex and Cursor. Repo-visible packs are selected separately from the global baseline.

## Shared home library

LocalSetup installs managed skills and workflow packages to `~/.local/share/localsetup/packages` and writes a registry beside them. The managed package root contains the union of the global baseline and any repo-visible packages. Explicitly selected repo adapter paths such as `.agents/skills` and `.kilo/skills` are shared agent surfaces where LocalSetup writes scoped managed entries. In symlink mode, each selected repo-visible LocalSetup package inside the adapter links to the managed home library. In portable mode, the selected repo-visible packages are copied. Both modes write `.localsetup-adapter.json` so LocalSetup can recognize, verify, detach, and replace its own managed entries later. A prior `.codex/skills` surface is retired only when LocalSetup ownership is proven; custom content remains in place.

LocalSetup does not own an entire adapter directory merely because the path matches a supported platform. Custom skills, ordinary files, repo-local symlinks, and other agent-owned entries may live beside LocalSetup-managed entries and must be preserved in place. See [Adapter ownership](ADAPTER_OWNERSHIP.md).

Workflow packages are sourced from `ls/workflows/ls-workflow-*`. They install beside skills because every workflow package includes a valid `SKILL.md`, while its LocalSetup-specific `workflow.yaml` stays in source for validation and generated docs.

A fresh global-only invocation without platform or target selection and without
a recorded target refreshes the managed library without attaching repo adapters.
Omitting `--tools` or `--platforms` is not a no-adapter-change guarantee for an
existing target: selector-free updates retain its recorded scope, clients and
adapter paths. Inspect the plan and follow
[updates using the default target](ADAPTER_OWNERSHIP.md#updates-using-the-default-target).
Custom neighboring agent configuration remains protected by the ownership rules.

To remove a install, run:

```bash
localsetup rollback
```

## Dependency preflight

Before install, use the dependency list below as the canonical source of truth. The Bash wrapper performs the same minimal LocalSetup-owned environment quarantine needed before uv can run the Python CLI, then delegates preflight and dependency work to `localsetup.py`; it does not call system pip directly.

### Canonical dependency list

| Dependency | Required / Recommended | Used by |
|------------|------------------------|---------|
| `git` >= 2.20.0 | Required for raw bootstrap clone and refresh; recommended otherwise | Cloning or refreshing the managed source checkout, source traceability, and release workflows |
| `rg` (ripgrep) | Recommended | Framework search and review workflows |
| `python` >= 3.12 | Required | Installer, framework tools, tests, and Python-first policy |
| `uv` | Required for dependency sync | Sync `pyproject.toml` / `uv.lock` into the source checkout `.venv` |
| Python: `yaml` (PyYAML>=6.0) | Recommended | YAML parsing for skill index, config, and PRD files |
| Python: `requests` (requests>=2.28) | Recommended | HTTP client used by index refresh and scrub tools |
| Python: `frontmatter` (python-frontmatter>=1.1) | Recommended | YAML frontmatter parsing for skill and PRD markdown files |
| Python: `cryptography` (cryptography>=50.0.0) | Recommended | Framework cryptographic primitives for secure envelope workflows |
| Python: `pgpy` (PGPy>=0.6.0) | Recommended | Pure-Python OpenPGP support for secure mail workflows |

Python package intent is listed in `pyproject.toml`; `uv.lock` is the committed lock used by automation and installer sync. Dependency PRs must update both files when dependency intent changes. The conservative default is `prompt-only` dependency checking; explicit `--sync-env` or `--dependency-mode uv-sync` creates or updates the uv-managed source checkout `.venv` without mutating externally managed system Python. To check the current machine without changing files, run:

```bash
localsetup doctor
```

Dependency checks use installed Python distribution metadata from the selected interpreter. This keeps packages with different distribution and import names, such as `PGPy` / `pgpy`, from being misreported as missing after managed dependency installation. If `doctor` finds an old `~/.local/share/localsetup/venv` or target `.localsetup/venv` from pre-uv installs, it reports the ignored legacy venv and a repair hint instead of executing that interpreter.

To normalize install intent without changing files, run:

```bash
localsetup configure --platforms codex --packs core
```

To install dependencies automatically during install, add the `--sync-env` flag:

```bash
./install --directory . --tools cursor --sync-env
```

Without `--sync-env`, the root wrapper runs doctor in `prompt-only` mode and applies the install without syncing Python dependencies. Direct Python CLI installs use the same conservative default; pass `--dependency-mode uv-sync` when the CLI should sync the project environment before applying adapter changes.

When explicit sync is requested, LocalSetup may quarantine corrupt LocalSetup-owned environments by rename, never deletion. Eligible paths are the source checkout `.venv`, legacy global `~/.local/share/localsetup/venv`, and legacy target-local `.localsetup/venv`. Each quarantine writes a JSON record under LocalSetup state with the original path, reason, mode, timestamp, and uv error text when applicable. A target project's own `.venv` is not LocalSetup-owned and is never modified.

Do not alter system Python to satisfy LocalSetup framework dependencies. Install
uv or set `LOCALSETUP_UV_BIN` to a preinstalled uv binary. Use `pipx` for ordinary
standalone CLI tools and the uv project environment for libraries imported by
source-checkout framework modules. LSCli already supports
[explicit offline wheel/runtime setup](LSCLI.md#explicit-offline-runtime-setup) and
[owned command registration](LSCLI.md#public-fresh-command-registration). Ordinary
CLI installation alone does not establish protected coding readiness; use those
contracts for artifact verification, runtime ownership and per-run qualification.

## Reinstall behavior

On re-run, the LocalSetup installer refreshes the managed shared package library, rewrites the global registry, updates selected LocalSetup-managed adapter links or portable copies, and writes `.localsetup/lock.json`. The wizard reloads prior global baseline selectors from the registry and repo-visible selectors, including explicit workflow selectors, platforms, adapter mode, and dependency mode from `.localsetup/lock.json`. Choosing no repo setup for a prior target removes managed adapter entries and metadata while leaving custom adapter content and the shared package library intact.

For legacy or partial targets, `localsetup doctor repair --target-directory <repo>` reports inferred state without mutating by default. `--repair-mode safe-repair --yes` applies only LocalSetup-owned repairs: clean legacy `ls/` trees must match the current framework source contents exactly, tracked trees are untracked with `git rm -r --cached -- ls` before removal, and benign adapter content such as custom repo skills, ordinary files, and repo-local symlinks is preserved. Protected LocalSetup source checkouts stay protected from `ls` removal while still allowing safe adapter and lock refreshes. Use `--repair-mode migration-plan --agent-prompt` or `--emit-agent-prompt PATH` when a dirty, symlinked, custom, unsafe, or content-divergent `ls/` tree needs handoff review.

`--upgrade-policy` is removed. LocalSetup uses managed install metadata and refuses to overwrite unmanaged skill paths.

Adapter paths are conservative. The installer creates only the selected adapter subpath, for example `.agents/skills`, and preserves neighboring project configuration such as `.cursor/rules`. Inside that adapter subpath, unmanaged entries are repo-owned by default and must survive. Ordinary files and repo-local symlinks that are not selected package targets are preserved without requiring an `adapter_content` decision. Same-name selected package collisions, regular files where a managed entry is needed, dangling symlinks, unsafe nodes, and symlinks that point outside managed or repo-local safe roots are decisions or blockers; they are not permission to clear the directory. The installer recognizes current scoped adapters, legacy monolithic managed symlinks, and LocalSetup-managed portable copies so re-running the same install can update managed entries without exposing unrelated global packages or moving custom content.

## What gets deployed

- **Shared library:** Managed skills under `~/.local/share/localsetup/packages`.
- **Workflow packages:** Managed copies of selected `ls/workflows/ls-workflow-*` packages in the same library.
- **Per-platform adapters:** Explicitly selected repo paths from `ls/config/platforms.yaml`, such as `.agents/skills` and `.kilo/skills`.
- **Lock and registry:** `.localsetup/lock.json` in the repo and `~/.local/share/localsetup/registry.json` in the LocalSetup home.
- **Runtime state:** `.localsetup/health.json`, `.localsetup/AGENT_STATUS.md`, `.localsetup/install-journal/`, `.localsetup/backups/`, `.localsetup/state/`, and `.localsetup/context-index/` are local runtime paths added to `.git/info/exclude`; `.localsetup/lock.json` remains managed repo state.

## Framework tools

| Tool | Linux/macOS | Windows |
|------|-------------|---------|
| Install | `./install` or `localsetup install --tools codex --yes` | WSL2 only |
| Doctor | `localsetup doctor` | WSL2 only |
| Configure | `localsetup configure --platforms codex --packs core` | WSL2 only |
| Agent context | `localsetup context --markdown` | WSL2 only |
| Migrate | `localsetup migrate` | WSL2 only |
| Convert | `localsetup convert --tools codex --packs core` | WSL2 only |
| Plan | `localsetup plan --tools codex` | WSL2 only |
| Verify | `localsetup verify --tools codex` | WSL2 only |
| Tests | `uv run --locked ./ls/tests/automated_test.sh` | WSL2 only |

Framework install logic is Python-first. Shell is limited to the bootstrap wrapper, and PowerShell is not a native install target.

## Repo-local

Framework source lives in the LocalSetup source checkout. Target repo context and state live under `.localsetup/`, while installed skill and workflow package copies live in the managed home library. Add `--tools` or `--platforms` when you want to attach repo adapter paths.
