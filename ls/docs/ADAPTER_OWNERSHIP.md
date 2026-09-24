---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-compliance
---

# Adapter ownership

**Purpose:** Define the ownership boundary for agent adapter-shaped directories. This is the canonical rule for install, repair, verify, detach, rollback, conversion, and migration planning.

## Principle

Adapter-shaped directories are shared agent workflow surfaces, not exclusive LocalSetup-owned surfaces.

This includes repo-local and global paths such as:

- `.agents/skills` (current Codex/shared adapter)
- `.codex/skills` (historical Codex transition surface only)
- `.claude/skills`
- `.cursor/skills`
- `.kilo/skills`
- `.openclaw/skills`
- `.opencode/skills`
- historical `.agents/skills`
- equivalent future adapter paths with the same package-directory shape

A repo, user profile, or other agent tool may intentionally keep custom skills, files, symlinks, generated outputs, or mixed managed and repo-owned content in those directories.

An adapter path may also be a repo-local symlink to another adapter-shaped directory in the same target repo. Historical `.codex/skills` links are never adopted as a compatibility alias: LocalSetup transitions only proven managed entries to `.agents/skills`, preserves custom content, and requires review for unproven links.

## LocalSetup-Owned Content

LocalSetup owns only the entries it explicitly creates and records. LocalSetup may create:

- a marker file such as `.localsetup-adapter.json`
- symlink-mode entries for selected LocalSetup-managed packages
- portable-mode copies for selected LocalSetup-managed packages
- lock, registry, report, and journal metadata under `.localsetup/` or the managed home library

The presence of a supported adapter path, a `skills` directory, or agent-compatible package names does not make the whole directory LocalSetup-owned.

## Required Behavior

Install, update, repair, conversion, detach, verify, rollback, and cleanup code must treat adapter directories as mixed by default.

Required handling:

- preserve custom adapter content in place
- mutate only LocalSetup-managed entries that are recorded or otherwise proven LocalSetup-owned
- preserve ordinary repo-owned files, custom skill directories, and repo-local symlinks that are not selected LocalSetup package targets
- report selected same-name collisions as decisions before mutation
- report unsafe adapter nodes, dangling external symlinks, and symlinks outside managed or repo-local safe roots as decisions before mutation
- preserve repo-local adapter symlinks whose targets stay inside the target repo and contain valid custom skill packages
- avoid moving, renaming, deleting, or normalizing repo-owned content out of an adapter path unless the repo owner explicitly chooses that migration

Custom skills, benign files, repo-local symlinks, and same-directory mixed content are evidence that the path is shared. They are not evidence that LocalSetup should claim or clear the directory. `adapter_content` and `adapter_collision` decisions are reserved for cases that would overwrite selected LocalSetup package names or touch unsafe filesystem nodes.

## Repair Planning

`localsetup doctor --target-directory <repo>` without a platform selector checks
the adapters recorded in that target's lockfile, including legacy adapter-state
records. A missing recorded adapter is a blocker; run `verify` and review a repair
plan. Diagnosis does not recreate adapters or change neighboring custom content.
The global-only warning applies when no adapters are recorded or selected.

An explicit platform selector checks those planned targets. An absent path is
allowed for a fresh target, but a selected path already recorded in the lockfile
must exist. An explicitly empty platform list checks no adapters and retains the
global-only warning when a target directory was provided.

Repair plans must describe the managed entries they intend to change. A plan that targets an entire adapter directory is safe only when every entry in that directory is proven LocalSetup-owned or the operator explicitly approved a full-directory migration.

When ownership is unclear or unsafe, the safe repair output is a migration or preservation prompt. Benign repo-owned adapter content does not require a prompt; the default repair path is to leave that content where it is and refresh only the LocalSetup-managed entries around it.

## Documentation Rule

Framework docs should say "managed adapter entries", "selected adapter links", or "LocalSetup-managed entries inside the adapter" when describing LocalSetup-owned content. Avoid wording that implies `.agents/skills`, historical `.codex/skills`, `.cursor/skills`, or any other adapter directory is exclusive to LocalSetup.

## Recorded installation owners

New installation lock adapter records include `owners`, a list of objects with
`scope`, `root`, and `client`. Repository attachments record `scope: repo`, the
absolute resolved target root, and each selected client ID. A shared physical
adapter retains every logical client owner. Partial detach updates this list in
both the target lock and registry receipt while preserving remaining clients
and custom neighboring content.

The existing version-2 `platform`, `platforms`, `path`, and package fields remain
compatible. Older records without `owners` remain readable; partial detach
records the remaining repository owners from their existing client membership.
This metadata describes managed installation membership, not ownership of the
entire adapter directory or permission to modify vendor configuration/state.
The typed owner model also distinguishes `personal` roots. Public
`--skill-scope` selection is available on plan, install, and update.

## Scope planning boundary

The internal `build_install_plan(..., skill_scope=...)` API accepts `repo`,
`personal`, or `both`. Omission retains `skill_scope` from the current target
lock, falling back to the legacy lock only when the current lock is absent.
An older lock without the field and a fresh target default to `repo`; invalid
recorded scopes fail instead of silently changing intent. Explicit scope
replaces the recorded value. Scope does not select clients: an empty client
selection creates no adapter actions in any scope.

Personal plans enumerate the selected clients' manifest `global_paths`, retain
all typed logical owners on shared paths, and use the selected adapter packages.
These discovery paths are distinct from the shared canonical package library.
Planning creates no configuration or adapters. Internal personal symlink and portable actions
use the preservation path below, also used by public scope selection.
Repository installations persist the effective scope in the existing lock.

## Personal package retention records

The registry records explicit personal adapter owners in `personal_owners`, keyed
by a deterministic `personal:` reference derived from their typed root/client
identity. Each record contains the owner, selected package names, and adapter
paths. Package references retain that identity independently of repository target
references. Multiple clients sharing a path remain separate logical owners.

Updating an explicit personal owner replaces its package selection; a
repository-only update or repository removal preserves that personal owner.
An empty personal selection still retains an ownership record. Referenced
packages participate in the existing other-owner and pruning checks. Records may
reference only packages supplied by the installation. Registry updates use the
existing caller-held package-root lock and atomic registry save.

Personal application, [explicit personal detach](#personal-detach-command), and
[recorded personal repair](#recorded-personal-repair-api) use these retention
records. [Combined-scope repair](#repairing-both-ownership-scopes) preserves shared
owners; detach still selects `repo` or `personal`, without a combined `both` mode.

## Personal adapter application

Internal personal plans apply under the existing package-root lock. The writer
rejects paths outside the supplied home, symlink or non-directory ancestors,
unsafe markers and custom entries colliding with
selected package names. Other files, skills, and neighboring vendor state remain
in place. Symlink and portable modes are supported internally.

At a shared path, the visible package set includes the current selection and
packages retained by other recorded owners. The registry records each owner's
requested selection independently. The writer journals individual managed links or portable package directories
and the marker before changing them. Failure recovery restores those entries;
it does not replace the whole adapter directory or remove unrelated neighbors
created during the operation. Empty directories created during a failed attempt
may remain. Successful receipts use `personal_adapter_targets`, separate from
repository `adapter_targets` and `adapter_state`.

Repository detach and rollback preserve independent personal installations and
their package references. Repository detach preserves the retained owner union
on shared paths; repository rollback uses the transaction described below. Repository
updates on a personal adapter preserve its owners through the shared writer
below; conflicting modes still fail preflight. A single install coalesces
repository and personal actions targeting the same path when their modes and
package library agree.
They do not implicitly remove personal adapters.
Explicit personal detach is available through the command below.
Personal repair is qualified through the recorded-owner route below.

Portable packages are copied with their provenance and internal symlinks intact.
Recovery backs up only the managed package node, so a failed copy restores its
prior contents without replacing the shared parent directory. A selected owner
can change between symlink and portable mode. At a shared path, a mode change
that conflicts with an unselected personal or repository owner fails preflight.
Personal registry records retain the mode; older personal records default to
symlink, the only mode available before this field was introduced.

## Personal inventory and filesystem verification

Inventory includes a `personal` section from current registry ownership records.
It reports logical owners, their requested packages, and the expected visible
package union at each physical path. Client filtering limits requested owners;
other owners' packages remain part of the shared-path verification expectation.
An explicit empty client list inspects no personal adapter paths.

Verification for recorded `personal` or `both` installations checks the adapter
marker, owner mode agreement, complete visible package union, managed library
packages, exact symlink targets, and portable content including symlink targets.
Missing expected owner records, unsafe recorded paths, missing links, and changed
portable contents fail verification. Personal-only checks do not invent repository
adapters or historical repository transitions. These are read-only filesystem
checks; they do not claim that an external client discovered or loaded a skill.

## Recorded personal repair API

`repair_personal(source_root, home, clients=None, apply=False)` plans repairs for
recorded personal owners. Omission considers recorded owners; an explicit empty
list performs no repair, and a named client without a personal record fails
without installing it. Plans do not write configuration, journals, or locks.

Repair reuses the recorded package selections and modes. It repairs missing
managed links and drifted portable copies from the managed library. Unsafe
paths, ambiguous/custom collisions, invalid ownership metadata, and missing
library packages produce blockers; reinstall the missing library before retrying.
The API does not change registry ownership or repository lock records.

Apply rebuilds the plan under the package-root lock, journals individual managed
nodes, and verifies the result before accepting it. Failure restores prior nodes
and reports whether recovery succeeded; unrelated neighbors remain in place.
Repair journals are stored under the managed home state directory. Standard
doctor repair routes personal-only target receipts through this API.

## Scope configuration contract

The internal `InstallConfig.skill_scope` field and install configuration schema
accept `repo`, `personal`, `both`, or `null`. Omission and `null` remain unset
through loading and serialization; the planner resolves an unset value from the
recorded target scope, with `repo` as the fresh-target default. Merging an omitted
CLI value preserves the config value; an explicit scope replaces it. Scope does
not populate `platforms` or package selectors, including an explicit empty client
list. For example, `{"skill_scope": "personal", "platforms": []}` describes no
client adapters.

Plan, install, and update merge `--skill-scope` over the config value and pass
the result to the planner. Other lifecycle commands use recorded ownership.

### Doctor routing for recorded personal targets

`localsetup doctor repair --target-directory PROJECT` reads the recorded scope.
For a personal-only receipt, omitted client selectors use that receipt's clients;
an explicit empty client list selects none. Repair retains the registry's
per-client package selections and does not infer or migrate repository adapter
paths. Report-only and migration-plan modes suppress application even when apply
was requested. Invalid repair modes or unreadable lock metadata prevent writes.
The existing `--repair-mode safe-repair --yes` application flow uses per-entry
transactional recovery and verifies personal adapter contents. Resolver issues
are reported separately; this route repairs personal adapters only.

Automatic selector-free plan/install/update uses recorded personal update
planning when adapters are healthy. If adapters need repair, the command reports
or applies that repair first; rerun update to refresh packages afterward.
Combined `both`-scope doctor repair uses the recorded transaction below. Combined-scope operations are not
implied by personal repair support.

### Distinct selections on a shared personal path

Internal personal actions and their lock receipts may include `owner_packages`,
a mapping from canonical personal owner keys to requested package-name lists.
Its keys must match the action's owners exactly, and the union of its lists must
equal the action's `packages`. Each owner must request the same set and mode across all of its action paths.
Validation runs before adapter writes and again when recording ownership. An empty list retains an owner with no requested
packages. Without this field, existing actions continue to assign their package
list to every selected owner.

The adapter exposes the physical union, including retained unselected owners;
the registry records each selected owner's own list and package references.
This representation supports coalesced updates without broadening selections.
The internal recorded-update planner below consumes this representation.

### Recorded personal update planner

`build_recorded_personal_plan(source, home, target)` prepares a package refresh
for a healthy personal-only installation. It selects the clients recorded in
the target receipt, reads their current registry selections, retains recorded
paths and modes, and coalesces writes with distinct `owner_packages`. It retains
the recorded global baseline and refuses packages absent from the update source.
It does not reinterpret presets or discover new client write paths. Repair
unhealthy personal adapters before planning an update.

The normal apply transaction refreshes source packages and adapters. Receipt and
registry byte hashes are checked under its package-root lock before mutations;
if ownership changes after planning, rebuild the plan. Custom adapter neighbors
remain in place. Automatic selector-free CLI routing uses this planner for
healthy recorded personal targets. Qualification covers selected clients with
shared paths in symlink and portable modes; it does not establish host application behavior.

For an existing personal target, preview with
`localsetup plan --target-directory PROJECT`, then refresh with
`localsetup update --target-directory PROJECT`. Omit client/package selectors
to retain recorded selections; `auto_mode: recorded_personal` identifies this
route in JSON output. Preview does not change the receipt or registry. A
`repair_required` result handles adapter drift before package refresh. These commands retain an existing scope.

## Public scope selection

Preview and apply a personal installation:

```bash
localsetup plan --target-directory PROJECT --tools cursor --skill-scope personal --skills ls-context
localsetup install --target-directory PROJECT --tools cursor --skill-scope personal --skills ls-context --apply
```

Use `repo` for repository adapters or `both` for both sets of paths. These scope
choices do not change the canonical shared package library.

On a fresh target, scope alone selects no clients, even when existing adapter
directories could otherwise trigger automatic discovery. Omission retains the
recorded scope; repeating that scope with no selectors keeps automatic recorded
updates. To change personal package selections, name clients explicitly.
Adding the missing scope and retiring repository scope use the flows below.
Conversion between repository-only and personal-only scope uses the two-step
workflow below. Same-plan repository/personal actions on one path
require matching modes and package libraries.

## Repository updates on personal adapter paths

A repository update may target a directory already exposed by personal owners.
When modes agree, the writer combines the new repository selection with retained
personal selections and other repository owners, excluding the updating
repository's old selection. The original repository receipt records only its
requested packages; personal owner records remain unchanged. Legacy repository
receipts retain their existing client-membership interpretation.

These shared writes use the home-bound path checks and per-entry journal used
by personal adapters. A failed write restores managed entries while preserving
custom neighbors, including files created during the failed operation. Mode
changes that conflict with a personal owner fail before mutation. Shared-path rollback follows the transaction below.

Repository filesystem verification checks the full recorded owner union on a
shared path and reports the repository request separately from that union.

## Coalescing both scopes in one plan

When a `both` plan contains repository and personal actions for the same path,
preflight pairs them only if their modes and package-library paths agree.
Duplicate actions within one scope are rejected. Apply writes the directory
once through the personal entry journal, using the union of both requested
sets and retained owners. It excludes the updating repository's old selection.

The repository and personal actions remain separate in the plan and receipts;
coalescing physical writes does not merge their logical ownership or selections.
Filesystem verification checks the visible union. Conflicting modes or library
paths fail before writes, and the home-bound path and custom-content protections
remain effective.

## Detaching repository owners from shared paths

Repository detach removes only repository exposure on a path retained by personal
owners. It rewrites the managed union with an empty repository request, preserving
personal and other repository owners. Shared directories use per-entry journal
backups, including receipt backups, rather than whole-parent restoration. If a
receipt write fails, recovery restores prior managed nodes and receipts while
keeping custom neighbors, including files created during the failed operation.

A `both` receipt retains its personal targets and clients. Removing its last
repository target changes its recorded scope to `personal`; personal owner
records and package references remain intact. Detach preserves the canonical
package library. Personal detach uses the explicit scope command below.

Backup cleanup runs after transaction commit. A cleanup failure returns a warning
and the committed journal path; it does not restore obsolete ownership receipts
or attempt rollback using partially deleted backups.

## Rollback serialization and path preflight

Rollback holds the same package-root lock as installation and detach. Before
removing any package or adapter, it validates every recorded package and adapter
path. A malformed later entry therefore cannot cause partial deletion of earlier
valid entries. Recorded package paths must resolve to direct children of the
managed library; the library root itself is never a package removal target.
Adapter parents must resolve beneath the attachment target. Lock contention
follows the existing package-root timeout contract.

## Rolling back a repository with shared personal paths

When recorded repository adapters overlap personal owners, rollback preflights
retained unions and package pruning, then journals adapter entries, package
nodes, registry, and current/legacy repository receipts as one transaction.
Shared adapters retain personal and other repository selections. Packages with
remaining references stay installed. The repository registry target and receipts
are removed; independent personal ownership remains recorded and verifiable.

A failure restores managed entries and receipts without replacing adapter parent
directories, preserving custom neighbors created during the attempt. Empty
adapter directories may remain. Recovery errors are recorded in the journal.
Backup cleanup follows commit; cleanup failure reports a warning and journal
path without reverting committed ownership. This transaction applies to rollback
with personal overlap; the nonshared rollback path retains its existing behavior.
Explicit personal-owner removal is not implied by repository rollback.

## Personal detach engine

`ls.core.personal_detach.detach_personal(source, home, clients, apply=False)`
plans removal for explicit recorded personal clients. An empty client list is a
no-op; unknown clients are blockers. The engine uses recorded paths, preserves
other personal and repository selections, and leaves the package library intact.
The CLI exposes this engine through explicit personal scope.

Plan output lists affected owner IDs, adapter paths, and current target receipts;
it does not create state. Missing or unsafe affected receipts block the operation
before writes. Apply replans under the package-root lock and journals managed
entries, affected receipts, and registry together. It removes selected personal
references, updates remaining receipt clients and personal selections, and keeps
repository ownership. Removing the last personal selection from a `both` receipt
changes its scope to `repo`. A personal-only receipt retains its scope with an
empty client list after its last owner is detached.

Failure recovery preserves custom neighbors and restores managed entries and
receipts. Post-commit backup cleanup errors return warnings without reverting
committed ownership. This engine detaches exposure; it does not uninstall host
applications, remove vendor state, or prune the canonical library.

## Personal detach command

```bash
localsetup detach --skill-scope personal --platforms cursor --plan
localsetup detach --skill-scope personal --platforms cursor --apply
```

Personal scope defaults to a read-only plan when neither mode flag is given.
`--plan` and `--apply` are mutually exclusive. Use `--home` before the command
when managing an explicitly selected home. The operation reconciles every
recorded affected target receipt; `--target-directory` does not restrict a
personal owner's home-wide identity. Output is JSON; success exits zero and
ownership/preflight/recovery failures exit two with their blockers.

`detach` without `--skill-scope personal` retains repository detach behavior,
including its existing immediate application. Installation configuration does
not implicitly change detach scope. Repository `--plan` currently fails before
mutation; combined `both` detach is not yet exposed. Client selection remains
required through `--platforms` (alias `--tools`).

## Adapter entry writer boundary

The internal `personal_adapter.write_entries` primitive journals individual
managed entries and marker changes beneath a caller-supplied root. It rejects
paths outside that root, symlinked ancestors, and unsafe package names before
writing. The caller must validate ownership, modes, selected package sources,
and collisions and hold the package-root lock. Personal operations continue to
run their existing selection checks and pass the home directory as that root.
Combined doctor repair uses this primitive with the recorded repository root
or home boundary, as appropriate.

## Repairing both ownership scopes

Doctor repairs a current `both` receipt from its recorded adapter paths, modes,
and package selections. It preserves other recorded owners sharing each path,
coalesces physical writes, and leaves receipts and registry selections unchanged.
Client filters apply to recorded membership; omitted filters use recorded clients.
Unknown clients block repair. A read-only report lists the selected repairs.

Apply replans under the package-root lock, writes managed entries through one
journal, and verifies that the selected adapters no longer need repair before
commit. Failure restores prior entries across both scopes while preserving
custom neighbors. Cleanup failures after commit return warnings in the combined
result. Missing library packages, unsafe paths or markers, conflicting owner
modes, and custom selected-name collisions require resolution before repair.
This route repairs exposure; it does not refresh the library, migrate ownership,
or certify host application loading. Resolver diagnostics remain separate.

## Updating recorded combined installations

A healthy `both` installation uses the `recorded_both` automatic update route.
It retains each repository path, client set, mode, and package request, and each
personal owner's distinct selection. The refreshed canonical library contains
the union of these requests and the recorded global baseline. Omitted scope and
selectors retain recorded ownership; this route does not infer fresh clients.

```bash
localsetup plan --target-directory /path/to/project
localsetup update --target-directory /path/to/project
```

The first command previews without writes; `update` applies immediately. Existing adapter drift must
be repaired before package refresh. Apply checks receipt and registry hashes
under the package-root lock; changed ownership invalidates the prepared plan.
Historical adapter-transition receipts survive refresh as evidence and are not
replayed. This route does not migrate scope. Explicit mode requests follow the rules below.

## Explicit modes on recorded updates

### Unavailable recorded packages

For a healthy installation using the recorded personal, combined, or retained
repository update route, packages absent from the selected source stop planning
before changing the lock, registry, or adapters. Existing repair and ownership
blockers take precedence over this diagnostic. The diagnostic lists every
unavailable identifier in sorted order. Known consolidated workflows name their
owning skill; unknown identifiers explicitly report that no supported replacement
is recorded. An owning skill absent from that source is also reported as unavailable.

| Retired workflow | Owning skill |
| --- | --- |
| `ls-workflow-audit-framework` | `ls-framework-audit` |
| `ls-workflow-audit-markdown-references` | `ls-markdown-reference-validator` |
| `ls-workflow-codex-heartbeat` | `ls-codex-heartbeat` |
| `ls-workflow-context-index-query` | `ls-context-index` |
| `ls-workflow-context-index-refresh` | `ls-context-index` |
| `ls-workflow-documentation-alignment` | `ls-documentation-alignment` |
| `ls-workflow-skills-index-refresh` | `ls-skill-discovery` |
| `ls-workflow-transport-handoff` | `ls-agentq-transport` |

This is a migration diagnostic, not an automatic rewrite of recorded selections.
Review the named owning skills for the required capability and preserve custom
adapter contents. Do not edit the lock or ownership registry by hand to bypass the
check. A selector-free `localsetup plan --target-directory /path/to/project`
provides the report without applying changes; repeating `update` with the same
source and selections remains blocked. Fresh explicit selections use current
skill identifiers; retired workflow names are not reintroduced as aliases.

### Adapter mode changes

For selector-free recorded `personal` and `both` plans and healthy inferred
repository updates, an explicit `--mode`
changes adapter mode while retaining recorded paths, clients, and package
selections. An explicitly present config `attach_mode` also requests a change;
`--mode` takes precedence. Omitting both retains each recorded mode.

```bash
localsetup plan --target-directory /path/to/project --mode portable
localsetup update --target-directory /path/to/project --mode portable
```

Preview and apply both preflight the requested mode against retained shared
owners. A conflicting unselected personal or repository owner blocks the change
before writes; it is not silently converted. Normal journaled application remains effective. Receipt/registry hash checks
apply to recorded personal and combined plans; inferred repository plans retain
their existing preflight checks. Repository-only updates retain the existing inferred selection while applying
the explicit mode; personal and combined updates retain recorded selections.

## Recorded repository detach authority

Repository detach uses `adapter_targets` paths and typed repository owners from
the installation receipt. Legacy rows without `owners` use their recorded
`platforms` or `platform` membership. Explicit-empty owners or client lists remain
empty; current catalog paths never supply missing mutation authority.

All recorded paths and owner roots are validated before removal. Duplicate or
out-of-target records block detach. A catalog path change therefore does not
redirect removal: the old recorded exposure is detached, while a newly catalogued
path remains untouched. Unselected owners and custom neighbors remain in place.
When no recorded owner matches, detach is a no-op. Receipts lacking client/path
ownership require reconciliation before removal; discovery alone cannot authorize
cleanup. Normal partial-owner receipt updates and package retention still apply.

## Historical paths with current personal owners

A path can be historical for repository exposure while remaining a current
personal adapter. For example, when the repository target is the home directory,
OpenClaw's historical repository path and current personal path coincide.
Retirement distinguishes those owners before removing managed entries.

If the same plan writes the personal adapter, the transition records
`delegated-current-personal` and leaves physical work to that journaled write.
The write excludes the updating repository’s obsolete historical selection,
while preserving other owners and any paired current repository request.
Otherwise, recorded personal ownership is validated and retained through the
shared writer with an empty retiring repository request. The transition records
`preserved-current-personal`; custom neighbors and other current owners survive.
Preflight and application both evaluate current ownership.

Verification permits historical-path exposure only when the visible package set
matches the retained owner union and the current personal adapter verifies.
Unowned residual repository exposure still fails the historical check. These
rules do not rename historical identifiers or remove their evidence receipts.

## Installed adapter views and discovery

Inventory uses recorded repository adapter paths when an installation receipt
exists and labels that view `adapter_source: recorded`. Without a receipt it
retains catalog discovery and labels the view `discovery`. Personal ownership
continues to come from its registry records.

Filtered and unfiltered installed verification use the same recorded paths.
Client filters select rows by typed repository owners, or recorded legacy client
membership when typed owners are absent. Empty filters select no adapters;
explicit-empty ownership never falls back to the catalog. An explicit empty
`adapter_targets` list also suppresses fallback to historical `adapter_state`.
Legacy receipts lacking that field can use their recorded adapter-state paths
and target-level client membership. Relative recorded paths resolve against the
installation target. Catalog changes do not move installed-state checks to new
paths; current discovery remains a separate view.

## Additive migration engine

The internal `build_additive_scope_plan` planner can add the missing scope to a
healthy recorded `repo` or `personal` installation. It preserves existing paths,
package requests, modes, and historical receipts, and discovers paths only for
the added scope. It does not infer additional clients. Receipt and registry
hashes reject stale application; the existing locked installation transaction
writes the combined receipt and ownership registry.

A client with differing requests or modes across recorded paths requires
reconciliation. A new shared repository path requires identical selections and
modes for its owners, since repository receipts do not encode per-owner package
selections. An already registered personal owner also requires reconciliation;
adding scope cannot silently replace that owner's request. Custom collisions and
retained-owner mode conflicts fail preflight. Planning performs no writes.

For an existing repository-only or personal-only installation, add the missing
scope with no client or package selectors:

```bash
localsetup plan --target-directory PROJECT --skill-scope both
localsetup install --target-directory PROJECT --skill-scope both --apply
```

The result reports `auto_mode: additive_scope`. `plan` and `install` without
`--apply` only preview; `update` applies immediately. An explicit `--mode` (or
config `attach_mode`) changes adapter mode through the recorded-mode preflight;
omission retains recorded modes. A migration combined with client or package
reselection is rejected before writes. Convert between `repo` and `personal`
through the two-step workflow below.


## Retiring repository scope

For a healthy `both` installation, retain its personal installation and retire
all recorded repository owners with:

```bash
localsetup plan --target-directory PROJECT --skill-scope personal
localsetup install --target-directory PROJECT --skill-scope personal --apply
```

The response uses `auto_mode: retire_repository_scope` and lists the selected
clients, recorded paths, retained personal targets, and receipt/registry hashes.
`plan` and unapplied `install` preview without writes; `update` applies. Scope
retirement does not refresh the package library, discover new paths, or change
personal requests. Combine neither package/client reselection nor explicit mode
changes with retirement; perform those as separate operations.

The package-root lock protects revalidation and the recorded detach transaction.
A changed preview fails its state check; application failures restore affected
receipts and individual managed entries through detach recovery. Retirement
keeps adapter parent directories in place, so newly created custom neighbors
survive failure recovery as well as successful retirement. Shared physical paths
retain personal packages, other targets and personal ownership remain intact,
and custom neighboring content stays in place. Historical transition evidence
is retained. Every repository adapter must have recorded ownership, and the
receipt must retain personal targets; ambiguous or unhealthy state requires
reconciliation before retirement. The resulting scope is `personal`.


## Retiring personal scope

For a healthy `both` installation, retain repository ownership with:

```bash
localsetup plan --target-directory PROJECT --skill-scope repo
localsetup install --target-directory PROJECT --skill-scope repo --apply
```

The response uses `auto_mode: retire_personal_scope`. It lists
`detached_associations`, globally removed `owners`, and `retained_owners` still
referenced by other targets. Another target's recorded reference keeps that
personal owner and its exposure intact; retirement does not rewrite the other
target's receipt. When the retiring target is the only recorded reference, its
personal owner is removed. Physical writes retain repository requests and other
personal owners on shared paths. Packages and custom neighbors are preserved.

Planning is read-only; `install --apply` and `update` apply. Reselection and mode
changes must be separate operations. Receipt and registry hashes detect stale
plans, and application revalidates under the package-root lock. One per-entry
transaction updates the target receipt, registry, and affected personal paths,
including the case where only association metadata changes. Failed writes
restore receipts and managed entries without replacing adapter parent
directories. The resulting target scope is `repo`; its repository adapters and
historical transition evidence remain unchanged. A single-command `repo` to
`personal` or reverse conversion is not exposed; use the two-step workflow below.

## Updates using the default target

When a receipt exists at the default attachment target, selector-free `plan`,
`install`, and `update` use the same recorded or inferred existing-installation
route as an explicit `--target-directory`. This also applies to direct framework
invocations without global-shim target injection. Omitting the target flag does
not turn an existing installation into an empty client selection. Recorded
personal/combined requests and modes are retained; repository-only updates use
the existing inferred-selection route. Fresh-target selection behavior is
unchanged.


## Repository detach recovery

Ordinary repository detach uses the same per-entry journal preservation as scope
retirement, including adapters without personal overlap. It snapshots managed
package entries and markers, then updates the receipt and registry under the
package-root lock. Failure recovery restores those entries without replacing
the adapter parent directory, preserving custom files created during the
operation. Empty adapter directories are removed only after the transaction
commits; retained entries or concurrent custom files keep their parent in place.
The package library stays installed, and recorded ownership continues to determine
which entries and owner memberships are removed.


## Converting between repository-only and personal-only scope

Conversion uses two explicit, independently committed transactions through a
valid `both` installation. Start with a healthy recorded installation. Retain
its client and package selection and mode during conversion; do not pass
selectors or an explicit mode. New-scope discovery must support the recorded
clients, and the additive preflight must pass the ownership and collision
constraints described above. In particular, an already registered personal
owner requires reconciliation before adding that scope; do not detach another
target's owner merely to bypass that conflict.

Repository-only to personal-only:

```bash
localsetup plan --target-directory PROJECT --skill-scope both
localsetup install --target-directory PROJECT --skill-scope both --apply
localsetup verify --target-directory PROJECT
localsetup plan --target-directory PROJECT --skill-scope personal
localsetup install --target-directory PROJECT --skill-scope personal --apply
localsetup verify --target-directory PROJECT
```

Personal-only to repository-only:

```bash
localsetup plan --target-directory PROJECT --skill-scope both
localsetup install --target-directory PROJECT --skill-scope both --apply
localsetup verify --target-directory PROJECT
localsetup plan --target-directory PROJECT --skill-scope repo
localsetup install --target-directory PROJECT --skill-scope repo --apply
localsetup verify --target-directory PROJECT
```

Each plan is read-only. The first apply adds the missing scope without retiring
the original; the second retires the original scope. Both steps preserve custom
content and historical receipts. If the second step fails and recovery succeeds,
the first step remains committed: the target is still a valid `both`
installation. Inspect the failure/journal and verify current ownership before
retrying or planning retirement of the added scope to return to the starting
scope. Do not blindly replay an operation with uncertain recovery.

Retiring personal scope removes this target's association. A personal owner
referenced by another recorded target remains installed and its other receipt
is unchanged; thus reaching `repo` scope does not promise disappearance of all
personal exposure on the machine. Repository retirement similarly preserves
personal exposure on a shared physical path.

## Personal ownership after repository rollback

`localsetup rollback --target-directory PROJECT` removes that repository's
receipt and target reference, including when its recorded scope was `personal`.
It does not imply global personal-owner removal. Independent personal owner
records, referenced packages, and personal adapters remain verifiable through
personal inventory; custom content remains in place. Unreferenced managed
packages may be pruned under the normal rollback rules. Use explicit personal
detach when the intended operation is removal of named global personal owners,
and review its affected receipts first.

## Mutable package baseline component

`ls.core.mutable_packages` supplies baseline checks for isolated mutable agent
projections. The component itself does not enable a client or provide a read-only
sandbox. [Writer and removal integration](#writer-and-removal-integration) and
[lifecycle preflight](#standalone-lifecycle-preflight) apply those checks; the
registered [Hermes adapter](#hermes-adapter-preflight-boundary) supplies the
mutable-copy designation. Other callers must complete their own lifecycle
integration before advertising support. This does not qualify a running host
or change ordinary portable-adapter behavior.

`capture_baselines(adapter, names)` returns a package-name to SHA-256 mapping;
store that mapping in the owning transaction/installation receipt outside the
mutable packages. `require_unchanged(adapter, baseline)` checks the recorded
copies before replacement or removal. Compare with the saved baseline, not with
the current canonical library: an upstream update must not make an unchanged
older copy look like a user edit. Missing packages also require preservation
review so that intentional deletions are not automatically undone.

The fingerprint includes every relative name, empty directory, permission mode,
file byte and package metadata file. It rejects symbolic links, multiply linked
files, special nodes, unreadable content and observed concurrent changes. Reads
use directory descriptors and no-follow opens on Linux/WSL and macOS. Limits are
10,000 entries and 512 MiB of file content per package. Hashing excludes timestamps
from the fingerprint but checks modification/change times while inspecting.
Custom neighboring packages outside the recorded name set remain untouched.

The caller owns path-boundary validation (including adapter ancestors), regular
copy creation, receipt authenticity, operation journals, target locks and
coordination with native writers. These checks detect drift; they cannot stop an
uncooperative same-user process from writing after inspection. Native writers
must be quiescent for an update/removal transaction. Never convert a detected
change into overwrite permission, refresh the baseline to hide drift, or claim
that same-user file modes establish isolation. Link-free materialized copies
protect the canonical library from edits through the projection; the copies
remain independently writable.

### Writer and removal integration

The repository writer accepts the internal `mutable=True` option; the personal
writer accepts action detail `mutable_copy: true`. These are internal integration
interfaces, not new command-line options or supported client profiles.
`ls.core.mutable_adapters` records the package baseline in `mutable_packages` in
the existing adapter transaction marker, outside the package directories. Once
present, both writers retain this mode on later writes even if the caller omits
the option. They require portable copies, reject linked source resources, check
prior drift before mutation and compare completed copies with the prepared
payload before committing the marker. Existing transaction snapshots cover the
marker and package entries together.

The shared adapter removal function checks opted-in baselines before removing
packages or their marker. Changed or deleted package contents block removal and
leave the receipt in place. An upstream library update alone does not block a
copy whose saved baseline still matches. Custom neighboring entries are outside
the recorded package selection.

Higher-level integration must retain the mutable-owner designation separately
in installation ownership records and call `check_existing(required=True)` for
established copies. Otherwise removing an entire adapter marker could erase the
opt-in indication. It must also preflight every affected mutable target before
any transaction mutation, including repair pre-actions, historical/scope
retirement and rollback. Low-level checks alone do not establish those complete
lifecycle guarantees. No Hermes profile is enabled by this writer integration.

### Apply ownership preflight

Successful apply records `mutable_copy: true` on opted-in adapter receipt rows
and `mutable_paths` on affected personal owner records. These fields live in the
repository lock and shared registry, independently of the adapter marker. Before
apply mutates state, `ls.core.mutable_ownership.require_owned_copies` checks the
physical action paths against recorded ownership. Established mutable copies
require their marker and baseline, and the baseline names must equal the recorded
physical package union. Selecting another client does not bypass this check when
it writes the same path. Drift at unrelated paths is not inspected as an affected
copy. Current personal owner records govern personal selections; historical
personal rows in other target receipts do not enlarge that union after a
cross-repository reselection. The target lock provides fallback evidence when
authoritative personal registry records are absent, with modern-lock precedence
over the legacy lock.

Receipt annotation happens inside apply's existing finalization transaction;
recovery covers both filesystem and ownership records. Plans remain provider-free
and do not invoke native clients. Standalone repair, retirement, detach and rollback enforcement is described
below; client registration and host qualification remain separate requirements.

### Standalone lifecycle preflight

Personal and combined repair check selected recorded copies before restoring
exposure. Personal detach (including its scope-retirement executor) checks before
reporting readiness and repeats the check under its package lock. Repository
detach checks paths it will physically remove. Repository rollback checks its
adapter paths before canonical package cleanup or shared rollback. Missing
markers, removed baselines, edited content and deleted files require preservation
review; they do not become fresh-install or automatic-repair candidates.

Applying repository repair acquires the package-root lock, then rebuilds and
checks the plan under that lock before its first mutation. It retains the lock
through apply using the internal unlocked executor. If the recorded scope changed
while waiting for the lock, repair stops with a retry diagnostic. A missing target
lock does not justify converting recorded mutable copies into symlinks: portable
mode is retained when surviving evidence agrees, and conflicting modes block.
Read-only repair reports do not acquire a mutation lock.

Historical retirement within apply uses its preflight over action paths before
mutations. These checks still require native writers to be quiescent; LocalSetup's
lock coordinates LocalSetup operations. They neither change native trust nor
establish a sandbox boundary. Hermes profile usage and installed-host limitations are documented in
[client integration metadata](CLIENT_INTEGRATION_METADATA.md#hermes-agent).

After personal detach or scope retirement, an empty mutable baseline is removed
only when the pending registry has no remaining mutable owner at that physical
path. This permits a fresh installation in another supported mode while retaining
protection for shared owners, including owners with an empty selection. Marker
cleanup participates in the same journal as package and ownership changes.

### Hermes adapter preflight boundary

The internal Hermes adapter preflight binds repository writes to `.hermes/skills`
and personal writes to the explicitly selected default profile `~/.hermes/skills`.
It requires portable independent copies and an explicit mutable-copy designation
for fresh writes. A nondefault `HERMES_HOME` blocks default personal writes;
this check does not resolve a running CLI or API session's selected profile.

Preflight validates selected authored skill and workflow packages before the
canonical installation can follow resource links. Symlinks, hardlinked files and
unsupported nodes therefore fail before installation changes. Existing copies
retain the baseline and independent ownership checks described above. No Hermes
process, provider, trust change or native configuration write is involved.
The registered `hermes-agent` planner supplies the mutable-copy designation;
this preflight does not qualify a running host.

## Self-refresh ownership

`localsetup self-refresh` refreshes the shared library from all configured packs by
default while retaining validated recorded adapter clients, physical paths, scope,
modes, and package exposure. Explicit empty ownership remains empty. Catalog
changes do not redirect an existing exposure or add clients that share its path.
Recorded repository, personal, and combined ownership use the existing recorded
update validation and stale-state preflight.

An unrecorded shared directory or generic portable marker cannot identify logical
clients. Self-refresh requires explicit `--platforms` or ownership reconciliation
in that case, before dependency work or adapter mutation. Explicit legacy client
selection retains normal preservation and native prerequisite checks. On validated modern
receipts, pack/global selectors affect the library; changes to client
selection, target package selectors, scope, or mode require explicit install or
the owning migration workflow rather than implicit reselection during refresh.

Without an explicit platform override, a fresh target with no recorded installation or adapter surface receives a library-only refresh; self-refresh does not select clients for it.
