---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-compliance
---

# Quickstart

Use this page to install LocalSetup, choose agent platforms, verify the install, and update later. For copy-paste command tables, see [Command reference](COMMAND_REFERENCE.md). For the product pitch, see the [root README](../../README.md).

## Requirements

- Python `>= 3.12`
- Bash on Linux, macOS, or WSL2
- Git and network access to GitHub for raw bootstrap, unless installing from a local clone
- Required for dependency sync: `uv`, with dependency intent in `pyproject.toml` and the committed `uv.lock`.
- Recommended: `rg`; GitHub/network access is needed for raw bootstrap unless installing from a local clone.

Windows is WSL2-only in LocalSetup. Native PowerShell install is intentionally not supported; run the Bash installer inside WSL2.

## Install In One Command

Global bootstrap from any directory:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s --
```

This opens a terminal wizard. The raw bootstrap wrapper checks the latest stable GitHub release, falls back to the latest stable-looking tag when the release API is unavailable, creates or refreshes the managed source checkout from that ref, shows the source/release status, target, managed package library, selected platforms and packs, then asks for confirmation before applying.

The wizard uses guided choices on every step. Single-choice prompts show `Enter number(s) | d details | b back | q quit | ? help`; detailed mode is on by default and explains what each option does, when to choose it, and its tradeoff. Multi-select prompts use checkbox controls in real terminals: move with arrows or `j`/`k`, press `Space` to toggle global packs, repo-visible packs, or platforms, and press `Enter` to accept. Scripted streams fall back to comma-separated line input. Press `d` to switch to compact mode when you only want the one-line summaries. Use `q` or Ctrl-C to quit; bare Esc is ignored so terminal arrow-key sequences do not cancel selection.

Color and glyphs are optional presentation aids, not part of the automation contract. Interactive runs default to `--color auto --glyphs auto`, honor `NO_COLOR`, avoid color on `TERM=dumb` and non-TTY output, and fall back to text labels such as `[OK]`, `[WARN]`, and `[FAIL]` when Unicode is not appropriate. Use `--no-color`, `--color never`, or `--glyphs ascii` for portable logs.

The old public command form still opens the wizard when a terminal is available:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --yes --tools codex
```

The public command is release-backed even though the small wrapper is downloaded from `main`: managed bootstrap installs resolve the latest non-draft, non-prerelease GitHub release tag before cloning or refreshing `~/.local/share/localsetup/source`, with a stable-tag fallback when release lookup is unavailable. Set `LOCALSETUP_BOOTSTRAP_REF` only when you intentionally want an explicit branch, tag, or commit. Explicit `--directory` checkouts are source-authoritative and are never auto-fetched or replaced.

For release verification, use the GitHub release tarball and its `.sha256` sidecar, then run:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . verify-release dist/localsetup-v$(cat VERSION).tar.gz
```

Release builds also publish a CycloneDX SBOM sidecar. When it is available, include it in verification:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . verify-release \
  dist/localsetup-v$(cat VERSION).tar.gz \
  --sha256 dist/localsetup-v$(cat VERSION).tar.gz.sha256 \
  --sbom dist/localsetup-v$(cat VERSION).tar.gz.cdx.json
```

Selecting tools in the wizard, or passing `--tools` / `--platforms`, attaches adapters such as `.agents/skills` to the selected target. If no tools are selected, the install is global-library-only. Interactive installs first choose the global package-library baseline, defaulting to `normal` or the prior registry setting. Repo setup is a separate choice; when selected, repo-visible packs default from the target lockfile or repo-detected suggestions.

For scripts and CI, use explicit automation mode:

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --non-interactive --yes
```

Automation mode keeps machine-readable output. Without a terminal and without `--non-interactive --yes`, the installer exits with a short message explaining both choices.

LocalSetup CLI commands emit JSON by default unless a command has an explicit human-readable mode such as `context --markdown`. The `--json` config flag remains available when scripts want to make that output contract explicit.

From a local checkout:

```bash
./install --directory .
```

The local checkout command uses that checkout as the registered source. Like the raw global bootstrap, it installs the default `normal` baseline into the managed library, registers `~/.local/bin/localsetup`, and does not create repo adapter paths unless you pass `--tools` or `--platforms`. If `~/.local/bin` is not on `PATH`, the installer warns and the command becomes available after you add that directory to your shell path.

After registration, run LocalSetup from any project:

```bash
localsetup install --tools codex --yes
```

The global command uses the registered LocalSetup checkout as source. For repo-scoped commands, it targets the nearest Git worktree root from your current directory, or the exact current directory outside Git. Override that with `--target-directory`.

Attach selected agent hosts explicitly:

```bash
localsetup install --tools codex,kilo --yes
```

Select packages by preset, pack, taxonomy class, tag, individual skill, or exclusion:

```bash
localsetup install --tools codex --preset suggested --skill-classes development --skill-tags git --skills ls-context --exclude-skills ls-linux-patcher --yes
```

The selector flags are additive except for `--exclude-skills`. Presets are `core`, `normal`, `suggested`, `all`, and `custom`; fresh selector-free installs default the managed global baseline to `normal`. `core` is the compact baseline, `normal` includes bootstrap, core, dev, frontend, architecture, ops, and publishing packs, `suggested` starts with `core` plus repo-detected additions, and `custom` lets the named packs, classes, tags, skills, and workflows define the footprint. Exclusions do not remove skills required by a selected workflow. The legacy selector flags apply to both the managed global baseline and repo-visible adapter selection for compatibility. Use `--global-packs` / `--global-preset` / `--global-workflows` and `--repo-packs` / `--repo-preset` / `--repo-workflows` when you want the managed library to contain a broader baseline than a target repo exposes.

For a full local installation with all shipped skill and workflow packs attached to Codex, Kilo, and OpenCode:

```bash
./install --directory . --tools codex,kilo,opencode --packs bootstrap,core,dev,frontend,architecture,ops,integrations,publishing,harness,skill-lifecycle,growth-content,specialized --sync-env
```

If Python dependencies are missing or you want the uv project environment prepared:

```bash
./install --directory . --sync-env
```

LocalSetup does not mutate system Python. Framework libraries sync into the source checkout `.venv` with uv, while app-style CLI tools should use `pipx` when they are distributed as commands.

The default `prompt-only` dependency mode is non-mutating: it reports missing uv, stale locks, and corrupt legacy LocalSetup environments with a repair command. Explicit sync paths such as `./install --sync-env` and `--dependency-mode uv-sync` may quarantine only LocalSetup-owned corrupt environments, including source checkout `.venv`, `~/.local/share/localsetup/venv`, and target `.localsetup/venv`, then let uv rebuild the source checkout `.venv`. A target project's own `.venv` is application-owned and is never changed.

## Platform IDs

| ID | Agent host | Adapter path | Managed package library |
|---|---|---|---|
| `cursor` | Cursor | `.agents/skills` | `~/.local/share/localsetup/packages` |
| `claude-code` | Claude Code | `.claude/skills` | `~/.local/share/localsetup/packages` |
| `codex` | OpenAI Codex CLI | `.agents/skills` | `~/.local/share/localsetup/packages` |
| `openclaw` | OpenClaw | `.agents/skills` | `~/.local/share/localsetup/packages` |
| `kilo` | Kilo CLI | `.agents/skills` | `~/.local/share/localsetup/packages` |
| `opencode` | OpenCode CLI | `.agents/skills` | `~/.local/share/localsetup/packages` |

Comma-separate multiple IDs:

```bash
./install --directory . --tools cursor,claude-code,codex
```

Omit `--tools` and `--platforms` on a source-only install for a global-only install. No omitted selector expands to every platform. For repo-targeted CLI automation, selector-free commands use auto mode:

```bash
localsetup plan --target-directory .
localsetup install --target-directory . --apply
localsetup update --target-directory .
```

Auto mode refreshes inferred existing adapters, applies only safe legacy repairs, or installs the `normal` global baseline for a brand-new repo without creating adapter paths.

Attach an adapter to another repo or directory while using this checkout as the source:

```bash
./install --directory /path/to/localsetup --target-directory /path/to/project --tools cursor
```

Convert a repo that may already contain old LocalSetup files:

```bash
localsetup convert --tools codex --packs core
localsetup convert --tools codex --packs core --yes
```

The first command is a dry report. Apply mode writes a timestamped backup and `conversion-report.json`, archives known managed or legacy LocalSetup artifacts, backs up and removes stale target `ls/` folders, blocks ambiguous unmanaged content, installs selected adapters, and verifies the result.

## What Gets Installed

- A registered framework source checkout under `~/.local/share/localsetup/source` or the checkout passed with `--directory`
- Managed skills under `~/.local/share/localsetup/packages`
- Managed workflow packages under the same library; their source remains `ls/workflows/ls-workflow-*`
- Explicitly selected LocalSetup-managed entries inside platform adapter paths such as `.agents/skills` or `.kilo/skills`
- `.localsetup/lock.json` and reports that support verification and rollback
- Transaction journals under `.localsetup/install-journal/` for applied installs

Consuming target repos do not receive `ls/` by default. If conversion finds an old target `ls/`, it backs it up under `.localsetup/backups/` and removes it before installing adapters.

Selected adapters use symlink mode by default. Use portable mode when symlinks are not suitable:

```bash
./install --directory . --tools codex --mode portable
```

Symlink mode creates a scoped marker and managed package entries inside the selected adapter directory rather than a monolithic link to the whole global library. The adapter contains `.localsetup-adapter.json` and one symlink per selected repo-visible package, so the repo sees only the selected LocalSetup skills and workflow packages even when the global library contains a broader baseline. The adapter directory itself remains a shared agent surface; custom skills, files, and non-LocalSetup symlinks may live beside the managed entries and are preserved. Portable mode uses the same marker and scoped package list, but copies selected managed packages into the adapter.

## Verify

For a target repo, verify the installed adapter state:

```bash
localsetup verify --tools codex
localsetup doctor --tools codex
```

For the LocalSetup source checkout itself, maintainers can also run source checks:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/localsetup.py --source-root . audit-global-first
```

After using `--sync-env`, `doctor` reports uv path/version, lock status, the source checkout `.venv` interpreter when present, and any repair metadata from dependency sync. Old global or target-local LocalSetup venvs from earlier releases are reported as ignored legacy state in prompt-only mode and quarantined only when explicit sync is requested.

Agent-readable install context:

```bash
uv run --locked python ls/tools/localsetup.py --source-root . context --markdown
```

Verify supports the explicit `filesystem` level, which runs the implemented local rule engine:

```bash
localsetup verify --tools codex --level filesystem
```

Use JSONL tracing when automation needs resumable evidence:

```bash
localsetup verify --tools codex --level filesystem --trace-json /tmp/localsetup-events.jsonl
localsetup doctor --trace-json /tmp/localsetup-events.jsonl
```

Inspect planned changes against the current lockfile before applying:

```bash
localsetup diff --tools codex
```

Search the installed catalog and explain pack selection:

```bash
localsetup skill search context
localsetup skill info ls-context
localsetup workflow search audit
localsetup skill info ls-framework-audit
localsetup why --packs core
localsetup graph
localsetup adopt --target-directory .
```

Generate SBOMs for the source tree or an installed target:

```bash
localsetup sbom --out /tmp/localsetup-source.cdx.json
localsetup sbom --installed --target-directory . --out /tmp/localsetup-installed.cdx.json
```

## Update

Refresh the framework source before applying a newer release. Managed bootstrap installations can rerun the [public bootstrap](#install-in-one-command), which resolves the latest stable release and opens the setup wizard. Explicitly selected checkouts remain source-authoritative: update that checkout to your intended release before rerunning install.

`localsetup update` refreshes packages from the registered source. It does not fetch a newer framework release. From an updated source checkout, re-run install with the same directory and platform selection:

```bash
./install --directory . --tools codex,kilo
```

The installer refreshes managed skills, selected LocalSetup-managed adapter links or portable copies, lock metadata, and reports. A global-only re-run refreshes the managed library and records an empty platform list. The wizard reloads prior global baseline selectors from the registry and repo-visible selections, including explicit workflow selectors, from `.localsetup/lock.json`; choosing no repo setup on a prior target removes managed adapter entries and metadata while keeping custom adapter content and the shared package library intact.

Selected workflow packs also refresh their workflow packages and required capability skill dependencies. See [Workflow packages](WORKFLOW_PACKAGES.md) for canonical source/runtime and install details.

`doctor repair` is the safe path for legacy or partial target repos. Report-only mode explains inferred platforms, repo skills, repo workflows, custom repo skills, stale framework classification, and proposed actions. Use `--agent-prompt` to include a compact handoff prompt in JSON, or `--emit-agent-prompt PATH` to write one for another agent. Safe repair only removes legacy `ls/` trees when their contents match the current framework source; custom, dirty, protected, symlinked, or content-divergent framework-like trees are preserved for migration planning.

`.localsetup/lock.json` is managed repo state. Runtime summaries, install journals, backups, health state, and context-index runtime data are local runtime state and are excluded through `.git/info/exclude`.

Non-interactive strict policy blocks high-risk skill metadata unless the operator explicitly chooses a less restrictive mode:

```bash
localsetup install --tools codex --policy-mode strict --yes
```

Use `detach` when you only want to remove selected LocalSetup-managed adapter entries while preserving custom adapter content, shared managed packages, and registry references:

```bash
localsetup detach --tools codex --target-directory .
```

## Roll Back Managed Paths

```bash
uv run --locked python ls/tools/localsetup.py --source-root . rollback
```

Rollback only acts on managed paths recorded by LocalSetup metadata.

## Next Steps

- [Features](FEATURES.md)
- [Shipped skills catalog](SKILLS.md)
- [Platform registry](PLATFORM_REGISTRY.md)
- [Multi-platform install](MULTI_PLATFORM_INSTALL.md)
- [Command reference](COMMAND_REFERENCE.md)
- [Workflow packages](WORKFLOW_PACKAGES.md)
- [Workflow registry](WORKFLOW_REGISTRY.md)
