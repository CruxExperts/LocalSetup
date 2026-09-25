---
status: ACTIVE
version: 4.44
owner_skill: ls-framework-compliance
---

# LocalSetup Overview

LocalSetup installs one global package library for skills and workflow packages, then attaches repositories to it.

## Global Library

Default global path:

- `~/.local/share/localsetup/packages/`

The global library contains managed copies of selected capability skills and selected workflow packages. Source stays split:

- Skills: `ls/skills/ls-*`
- Workflow packages: `ls/workflows/ls-workflow-*`

Repo attachment lockfile:

- `.localsetup/lock.json`

## Windows Support

Windows support is WSL2-only in the current framework. Run agents inside WSL and use WSL filesystem paths.

## Core Commands

- `localsetup plan`
- `localsetup doctor`
- `localsetup configure`
- `localsetup context --markdown`
- `localsetup migrate`
- `localsetup install --yes`
- `localsetup verify`
- `localsetup rollback`
- `localsetup adapters`
- `localsetup catalog`
- `uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog` (source checkout)
- `localsetup scan-migration`
- `uv run --locked python ls/tools/localsetup.py hook-gate`
- `uv run --locked python ls/tools/localsetup.py generate-docs`
- `uv run --locked python ls/tools/localsetup.py package --out dist/localsetup-public.tar.gz`

## Platform Adapters

The adapter contract is generated from `ls/config/platforms.yaml`.
Each selected platform gets a repo-local attachment path that points at the
shared global library. Omitting platform selectors is global-only and creates no
repo adapters. Verification without an explicit selector checks the recorded
install state rather than assuming every declared platform was attached.

## Portable Mode

Portable mode vendors managed copies into the repo adapter paths:

- `uv run --locked python ls/tools/localsetup.py install --mode portable --platforms codex --apply`

Use portable mode for repos that must carry their selected packages without relying on
the user's global library.

## Packs And Migration

The pack contract lives in `ls/config/pack.yaml`. The default install
uses `core`; optional packs can be requested with `--packs`.

Pack entries can select both skills and workflow packages. Workflow packages are listed under `workflow_packs` and may auto-include required capability skills through their `workflow.yaml` manifests.

Migration scanning reports remaining actionable `localsetup-*` references outside
the source skill corpus so maintainers can decide whether each reference should
stay as historical documentation or move to the current `ls-*` name. Use
`localsetup scan-migration --include-expected` when auditing intentional
compatibility surfaces and private backup snapshots.

In the source-only active tree, expected remaining scan findings are limited to
generated alias metadata, the skill alias map, and ignored private backups.
Those entries are intentional historical references used by migration reports,
compatibility docs, or local recovery evidence; new source skills and
user-facing setup docs should use `ls-*` names.

`migrate` is conservative: it backs up first, renames only known managed
`localsetup-*` global skill artifacts through the skill alias map, and
refuses unmanaged adapter collisions with remediation commands in the report.
