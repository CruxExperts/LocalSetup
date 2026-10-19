---
status: ACTIVE
version: 4.52
owner_skill: ls-herdr
---

# Herdr operations

LocalSetup supplies one shared deterministic Herdr core and five focused skills
in the normal installation's `ops` pack. Start with `ls-herdr`; it resolves
context and provenance and routes to layout, tasks, remote operations, or
provenance maintenance. The skill suite does not install Herdr automatically
merely because LocalSetup is installed.

## Entry points and ownership

```bash
localsetup herdr --help
localsetup herdr pane --help
localsetup herdr task --help
localsetup herdr remote --help
localsetup herdr --json provenance check
localsetup herdr --json provenance check --network
```

The thin direct entrypoint is `ls/tools/herdr_ops.py`. Domain logic lives in
`ls/core/herdr_ops/`; skills share that implementation. This is a dedicated
Herdr domain, not an extension of the heartbeat/finalizer harness, dashboard,
trusted queue, or model router.

The framework smoke matrix checks the master and specialist CLI help routes.
These checks do not open an endpoint or dispatch a native operation.

Use an explicit existing socket/session or the verified inherited caller
context. Use a saved machine for remote commands, or an explicitly named SSH
target for setup. Never use a focused pane, sidebar position, or example ID as
authority. Repository restrictions and active client modes remain effective.

Only the controller may select, read, or dispatch through a reserved `sudo`
pane. Other Herdr operations must also honor any stronger repository policy.
The core does not manufacture controller authority through an option.

## Layout and command lifecycle

Temporary work areas are owned panes below the caller, using a 30% split by
default without changing focus. Requested dimensions/direction take precedence
within valid geometry. The core tracks returned identifiers, including changed
pane IDs after moves. Cleanup affects only verified owned resources. Geometry
restoration must not overwrite intervening user layout changes.

Normal cleanup checks the pane's current occupants even after a task finishes.
Its completion receipt does not authorize interrupting a new command or agent
that later starts in the same pane.

Commands have operation-specific completion records. Queue admission, a prompt,
or an output substring is not an exit receipt. A wait timeout does not mean the
command failed or can be resent. A lost mutation response remains uncertain
until read-only reconciliation proves the outcome. Cancellation targets the
exact owned operation, never the whole Herdr server.

## Remote lifecycle and configuration

Inspect the exact SSH target, effective configuration, approved shared
connection, remote session, socket, and installation owner. Native machine
forwarding serves prepared servers. A narrow authenticated SSH lifecycle helper
fills the upstream noninteractive bootstrap gap; it uses official verified
artifacts and native Herdr controls after preparation. Package-manager-owned
installations must retain their owning update mechanism.

Translate portable master settings through the allowlist; preserve remote-only
configuration, resolve paths and shell on the target, and validate staged
configuration. Do not copy credentials, machine profiles, experimental/security
settings, or arbitrary commands. Local keybindings remain the default for remote
attach. Restart and standalone configuration resolve the existing binary's
absolute path before changing the remote. Restart reuses it for stop and start;
configuration uses it for native validation. They find a user-local binary even
when the SSH user's non-interactive PATH
omits its directory, and otherwise uses the existing executable on PATH.
Named sessions share global configuration, so configuration and restart
impact includes other affected sessions.

Deployment authority covers the named install/configure/start/connect outcome.
Do not repeatedly ask for routine substeps. If installation is discovered as a
new prerequisite, offer it once. A full restart stops processes: idle-first
maintenance proceeds automatically only when work will not be interrupted;
otherwise one decision covers the affected work. Existing authorization also
covers its scoped recovery. Deploy and upgrade operations retain their phase and backup
receipts in `remote-operations/<operation_id>.json` under the selected private
state directory; returned operation IDs identify the record for reconciliation. Authentication and materially changed scope remain
real boundaries.

If native profile registration times out, reconcile the exact target, session,
and label with one read-only lookup. A matching saved profile confirms success;
an unconfirmed result remains unknown and registration is not automatically
repeated.

Task-owned tunnels reuse SSH routes, bind TCP listeners to loopback by default,
and clean up only verified owned processes. Unix-socket relays support published
API methods without CLI wrappers. Resolve custom remote socket paths explicitly;
there is no documented socket-discovery API.

## Source provenance and daily watch

The master skill's `references/upstream/provenance.json` pins the official
release, full source commit, annotated tag, hashes, license, and coverage map.
Preserved upstream skill and schema evidence retain their original licensing.
The initial reviewed baseline is v0.9.5, not an everlasting claim about latest.

Keep source pin, installed client, bundled schema, and running server distinct.
Endpoint compatibility can permit different versions. A bundled client schema
does not establish remote capabilities. Unsupported operations return a specific
limitation rather than blocking unrelated work.

The provenance workflow observes upstream daily at 08:17 UTC. Its existing
documentation generation remains weekly on Mondays, plus push/manual runs.
The separate Herdr job emits a read-only summary/artifact, never a host upgrade,
source rewrite, branch, issue, or publication. The source watch compares the upstream skill, API schema, schema module, and
layout implementation against pinned hashes; it is evidence for review rather
than a complete semantic compatibility proof. Network failure reports unknown
freshness. Accepted updates use `ls-herdr-provenance-maintenance` and normal
source review, validation, documentation, and signed local checkpoints.

Primary upstream references: [pinned skill](https://github.com/herdrdev/herdr/blob/v0.9.5/skills/herdr/SKILL.md),
[socket API](https://github.com/herdrdev/herdr/blob/v0.9.5/docs/next/website/src/content/docs/socket-api.mdx),
[machine setup](https://github.com/herdrdev/herdr/blob/v0.9.5/docs/next/website/src/content/docs/connecting-machines.mdx).
