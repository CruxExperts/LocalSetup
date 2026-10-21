# Omni Route Update Workflow

## List Or Check

1. Run `scripts/omniroute_update.py check` against the desired pinned source.
2. Save the Markdown or JSON report to the run ledger or a private maintenance path if it is long.
3. Verify the release tag, commit, source tree, skills tree, and actual `skills/*/SKILL.md` inventory before using the report for decisions.
4. Compare the refreshed report with the last accepted ledger and inventory. Record every added, removed, and source-hash-changed skill before updating ledger facts or coverage.
5. Keep newly discovered skills as `missing-local` until an owner or converted package is reviewed; never infer coverage from a name or neighboring skill.
6. Retain the prior accepted report and immutable receipt as rollback evidence.

For rollback verification of the previously accepted `v3.8.48` source, run this command from the repository root and compare its source object IDs and canonical inventory digests with the tracked immutable fixture:

```bash
python3 ls/skills/ls-omniroute-skills-update/scripts/omniroute_inventory.py --git-dir <bare-mirror> --localsetup-root <repo-root>
```

The local root is required to read retained LocalSetup claim references; immutable skills and OpenAPI evidence still come from the pinned historical Git objects. Refreshing that fixture is a separate reviewed inventory wave, not part of an ordinary source-ledger update.

## Freshness Check

Run `scripts/omniroute_update.py freshness` before validating any converted or consolidated OmniRoute skill wave against the upstream repository.

- Default blockers: `stale-local` and `local-only`.
- Add `--strict-untracked` when every local OmniRoute skill must declare conversion metadata.
- Add `--require-all-upstream` when the task requires every upstream OmniRoute skill to have converted or native LocalSetup coverage.
- A non-zero exit means the skill set is not fresh enough for the chosen validation policy.

For consolidated native waves, use both `--require-all-upstream` and `--strict-untracked`. At OmniRoute `v3.8.50`, the expected report has 46 upstream skills: 44 `covered-native` or `current`, `cli-skill-collector` and `ponytail` as `missing-local`, and the four LocalSetup-native OmniRoute skills as `local-native`. The strict command must remain non-zero until those two gaps receive reviewed ownership; do not weaken the gate or claim a healthy full-coverage state.

## Import

1. Confirm the upstream skill is desired in LocalSetup.
2. Use `ls-import-external-skills` to obtain the upstream skill content.
3. Use `ls-skill-security-vetting` before trusting scripts, references, or instructions.
4. Use `ls-normalize-skill-format` to adapt documentation and tooling to LocalSetup conventions.
5. Use `ls-skill-sandbox-tester` before registering or deploying the converted skill.
6. In a normal LocalSetup installation, write the converted result as a physical repo-local copy under the initiating repo's `ls/skills/`. Do not modify global LocalSetup packages by default, even when the active adapter uses symlinks or global skill packages.
7. Add `extensions.omniroute` metadata to the converted skill frontmatter.
8. Register the converted skill through the standard LocalSetup skill registration process for that repo.
9. Ask whether the user wants to promote the repo-local skill copy to the machine/global LocalSetup installation. This requires explicit approval.
10. Ask whether the user wants help opening a PR upstream for consideration. If yes, collect a preferred attribution string, such as a name, handle, email address, or other credit text.

## Strict Replacement

Use strict replacement only when the maintainer explicitly wants LocalSetup to mirror the current upstream skill set and remove older converted entry points.

1. Verify the upstream commit, tag, package version, and actual `skills/*/SKILL.md` count from primary sources.
2. Save the report, the source delta from the last accepted baseline, and restorable rollback evidence before proposing mutations.
3. Obtain the repo owner's explicit replacement decision; the report does not authorize deletion or promotion.
4. Run the importer scan when the validation pattern file is fresh. If the scanner blocks on stale pattern metadata and no non-mutating use-existing path is available, record that blocker and perform a static vetting pass instead.
5. Delete only local skills whose frontmatter declares `extensions.omniroute.source_kind: upstream-converted`. Do not glob-delete OmniRoute-named directories.
6. Preserve all `extensions.omniroute.source_kind: localsetup-native` skills.
7. Generate each converted skill from the pinned upstream `SKILL.md` with complete `extensions.omniroute` provenance and LocalSetup safety wording before source-derived commands.
8. Update `pack.yaml`, smoke-command metadata, tests, source ledger, generated docs, and aliases in the same wave.
9. Validate with strict freshness, catalog checks, focused updater/manifest tests, native helper help commands, generated-doc drift checks, and final suite checks proportionate to the surface changed.

## Consolidated Native Coverage

Use consolidated native coverage when the maintainer wants a smaller, better-routed LocalSetup pack instead of one LocalSetup skill per upstream OmniRoute skill.

1. Verify the upstream commit, tag, package version, and actual `skills/*/SKILL.md` count from primary sources.
2. Compare the refreshed inventory with the last accepted inventory and record additions, removals, and changed source hashes.
3. Group upstream skills by LocalSetup operator workflow, not by upstream folder prefix alone.
4. Keep each native skill narrow enough to route accurately but broad enough to cover the related API/CLI surface.
5. Review every `missing-local` row and document the owner or conversion decision before changing either coverage map.
6. Update the human coverage map in `../../ls-omniroute-task-triage/references/upstream-skill-coverage.md` and the machine coverage map in `scripts/omniroute_update.py` together only after that review.
7. Remove converted package registrations and taxonomy rows only after preserving source provenance and rollback evidence in the ledger.
8. Validate with `freshness --require-all-upstream --strict-untracked`, catalog checks, focused updater/manifest/API helper tests, generated-doc drift checks, and final suite checks proportionate to the surface changed.

## Update

1. Treat `stale-local` as an update proposal.
2. Inspect the upstream diff from the local metadata commit to the target source commit.
3. Vet new or changed scripts and instructions before merging.
4. Preserve LocalSetup-specific safety, path, and testing adjustments unless the maintainer explicitly chooses otherwise.
5. In a normal LocalSetup installation, apply the accepted update to the repo-local physical skill copy, not to global LocalSetup packages.
6. Update `extensions.omniroute` metadata only after the converted content reflects the verified source.
7. Offer explicit global promotion and upstream PR assistance as separate follow-up choices.

## Modify

LocalSetup-specific changes may be appropriate after conversion, but keep the provenance clear:

- Preserve upstream source metadata.
- Explain the LocalSetup-specific delta in the skill body or a reference when it affects behavior.
- Do not present modified content as a byte-for-byte upstream copy.
- In normal LocalSetup installations, make the modified version repo-local first. The user may later choose to promote it globally or submit it upstream.

## Scope Boundary

`ls-omniroute-task-triage` remains the ambiguity router for unclassified runtime requests; this package owns only source inventory, coverage comparison, freshness, and change planning after routing. The repo-local physical-copy rule applies to LocalSetup installations that consume the framework. It does not apply when the task is maintenance of the LocalSetup framework source repository itself; in that case, changes belong in the framework source tree and follow the repository's normal validation and publishing workflow.

## Remove

1. Confirm whether upstream removed, renamed, or replaced the source skill.
2. Confirm whether LocalSetup still needs the converted skill independently.
3. Record the repo-owner decision in the run ledger.
4. Remove registration and files only after explicit approval and validation.

## Status Handling

- `missing-local`: candidate reviewed ownership or import; never automatic native coverage.
- `covered-native`: upstream skill is covered by a consolidated LocalSetup-native skill.
- `current`: no import/update action needed.
- `stale-local`: candidate update after diff review.
- `local-only`: candidate removal, rename handling, or LocalSetup-native preservation.
- `untracked-local`: candidate metadata repair or intentional LocalSetup-native classification.
