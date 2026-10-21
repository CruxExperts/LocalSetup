# OmniRoute Source Ledger

## Upstream

- Repository: `https://github.com/diegosouzapw/OmniRoute`
- Skills root: `skills/`
- Current Localsetup source version: `v3.8.50`
- Annotated tag object: `6f5d4e00e817bc01b2ac16fdd66db3840c296416`
- Current Localsetup source commit: `5458026c216f77a3da68ea49152dc33470cfe2cb`
- Current Localsetup source tree: `b57cf75da22d11dccc937e4415bb0449be97c774`
- Current Localsetup skills tree: `54bb150598c745737286014512dd24ed9ff98a17`
- Current Localsetup source commit date: `2026-08-26T19:29:05Z`
- Upstream package version: `3.8.50`
- npm `latest` at verification on 2026-09-03: `3.8.50`
- Release/package commit: `5458026c216f77a3da68ea49152dc33470cfe2cb` for tag/npm `v3.8.50`
- Upstream `main` at verification: `93265eede34d5666784aca474ccc41ce3c68140b`; its skills tree matched the release.

## Source Priority

1. A local OmniRoute checkout supplied with `--source-path`, when the operator knows it is the intended source.
2. The upstream Git repository at a pinned commit, tag, or branch supplied with `--ref`.
3. The upstream default branch only for discovery reports, never for final provenance.

## Refresh Rules

- Refresh this ledger before importing, updating, consolidating, or removing any OmniRoute skill coverage.
- Prefer a pinned commit for conversion or coverage metadata.
- Record the commit SHA, commit date, source path, source SHA-256 when applicable, and access date for every converted skill or native coverage wave.
- Treat the actual `skills/*/SKILL.md` inventory as authoritative. For `v3.8.50`, the pinned tree contains 46 skills; `cli-skill-collector` and `ponytail` are new relative to the accepted `v3.8.48` inventory.
- If upstream disappears or a skill is renamed, classify the local coverage as `local-only` or `missing-local` until a maintainer confirms removal or migration.

## Four-Skill Native Pack

Localsetup retains four native OmniRoute skills instead of shipping one Localsetup skill per upstream document. At `v3.8.50`, the existing map covers 44 of 46 upstream skills; the two newly discovered skills remain explicit gaps pending ownership review.

Current native pack:

- `ls-omniroute-task-triage` with `local_role: main-router`
- `ls-omniroute-proxy` with `local_role: proxy-discovery`
- `ls-omniroute-admin-automation` with `local_role: admin-automation`
- `ls-omniroute-update` with `local_role: update-workflow`

Authoritative coverage map:

- Human-readable: `../../ls-omniroute-task-triage/references/upstream-skill-coverage.md`
- Machine-readable: `../scripts/omniroute_update.py` `NATIVE_COVERAGE`

Historical immutable source inventory:

- Extractor: `../scripts/omniroute_inventory.py`
- Pinned source: `v3.8.48` at `7ee5bbc64dbb03e967521227f2afffeb7c9dad1e`
- Immutable inputs: that exact Git object, its `skills/*/SKILL.md` blobs, and `docs/openapi.yaml`
- Required local input: retained Localsetup claim references rooted by the explicit `--localsetup-root`; these claims are compared with the immutable source inventory and are not upstream Git blobs
- Outputs: full skill, OpenAPI endpoint, and `omniroute_*` tool inventories with canonical SHA-256 digests
- The extractor does not emit the local mirror or retained-claim root path. Keep its fixture unchanged as rollback evidence until a separately reviewed inventory refresh replaces it.
- For package renames, tests project only the fixture's local retained-claim package names and paths through the authored aliases. They verify its original digest first and preserve the upstream inventory and recorded fixture bytes.

Current coverage result for `v3.8.50` is:

- 46 upstream skills discovered from the pinned source tree.
- 44 upstream skills reported as `covered-native` or `current`.
- `cli-skill-collector` and `ponytail` reported as `missing-local`.
- Strict freshness with `--require-all-upstream` must fail until those gaps receive reviewed native ownership or converted packages.

The rollback baseline for `v3.8.48` remains reproducible from tag object `4f00f84b5a12f90fca2f1d72a60404cf6f5bf059`, commit `7ee5bbc64dbb03e967521227f2afffeb7c9dad1e`, source tree `4048504f76c6fb3dedd00ff2aa7250109308de99`, skills tree `e7b1871e0904fbdb0ff01bdc3fc1d7ea599707ff`, and the tracked immutable fixture.

## Current Boundary

The bundled updater is report-first. It produces comparison, coverage, and freshness reports and does not automatically write, remove, or replace converted skill directories.
