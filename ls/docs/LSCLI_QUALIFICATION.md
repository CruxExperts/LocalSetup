---
status: ACTIVE
version: 4.44
owner_skill: ls-architecture
---

# LSCli candidate qualification record

These are historical checks of candidate artifacts and bounded fixtures, retained
from the implementation record. They do not assert that the eventual published
wheel has passed a final release audit. An unnamed candidate below has no more
specific public artifact identity than its original record; do not infer one from
the document version or from a later scenario. Retain explicitly named source
commits, candidate versions, byte checks, operation counts and failures as stated.

Use the [operations guide](LSCLI.md) for current commands and the
[runtime reference](LSCLI_RUNTIME.md) for component contracts and inline fixture
limits. Earlier component checks do not limit later implemented command discovery,
and later implementation does not retroactively widen earlier qualification.
No fixture grants credentials, host configuration, recurring activation or
permission to replay an uncertain operation.

## Candidate verification

A Python 3.12/Linux wheel installation was checked outside the source checkout.
Its installed `lscli` command returned help and version successfully; doctor
reported `sdk_payload: verified` and `execution_available: false` with exit 3.
The check used a nonexistent temporary user home and confirmed that no home or
state directories were created. A separate isolated Python invocation confirmed
the installed module origin and absence of SDK, provider, HTTP, and YAML imports.
This evidence qualifies the provider-free bootstrap only.

## Installed static-doctor qualification

The doctor extension was checked from a wheel built at source commit
`f9ada80a` (candidate framework version 4.4.1), installed offline into a temporary
protected runtime outside the checkout. Both `lscli doctor` and
`localsetup agent doctor` returned `static_verified` with exit 0 for an intact
selected runtime and valid explicit profile file, while keeping
`execution_available: false`. Default inspection with an absent home returned
exit 3 without creating that home. No SDK/provider modules were imported and no
credentials or provider calls were used. Six installed CLI/diagnostic/runtime
modules matched both wheel bytes and source bytes; post-check runtime inventory
verification passed.

Installed checks also covered explicit missing/malformed payload fixtures,
exclusive-upgrade contention reporting `busy`, and a full output pipe returning
2 within its write deadline without stderr disclosure. The malformed payload
fixtures were separate from the selected protected runtime. These are Linux,
Python 3.12 candidate checks, not published-release acceptance, live provider
qualification, or proof of native sandbox functionality. Repeat artifact checks
against the actual released wheel after publication.

## Installed controller crash qualification

`ls/tests/installed_recovery_fixture.py` is an explicit integration fixture for a
sealed installed runtime and qualified cgroup delegation. Run it with that
runtime's isolated Python, the runtime root and delegation path; it is not a
provider credential or host-configuration setup tool. It creates private temporary
projects and uses only its local HTTP provider.

```bash
"$RUNTIME_PYTHON" -I -B ls/tests/installed_recovery_fixture.py "$RUNTIME_ROOT" "$CGROUP_PARENT"
"$RUNTIME_PYTHON" -I -B ls/tests/installed_recovery_fixture.py "$RUNTIME_ROOT" "$CGROUP_PARENT" responses
```

All three paths must identify the already-qualified installation and delegation;
the fixture does not select or repair them.

The fixture exercises controller termination in three process-result windows:

- After durable receipt persistence but before SDK acknowledgement: the orphaned
  SDK worker exits, a fresh owner reconstructs history, and a newly authorized
  coding continuation consumes the recovered result without another operation.
- After journal settlement but before receipt persistence: missing output refuses
  recovery; the process is not repeated to reconstruct it.
- After process return but before journal settlement: uncertainty refuses recovery
  and prevents new dispatch.

It checks the original checkpoint and journal, unchanged workspace content after
recovery/continuation, exact operation counts, worker exit, revoked-owner refusal
and every captured request's runtime-resolved user-agent. This qualifies the
selected Chat Completions and Responses paths for these crash windows; power-loss
durability and broader interactive acceptance remain separate checks.

## Installed setup and registration qualification

A built candidate wheel was exercised outside the checkout on Linux with
Python 3.12. The fixture used the locked offline dependency artifacts and two
managed runtime identities from the same framework wheel, one with the qualified
native sandbox bundle. Runtime selection and inventory verification used the
installed implementation, without selection mocks.

The installed command evidence covers:

- Profile plan without parent creation, private file creation, and refusal to
  overwrite an existing profile document.
- Fresh command registration, branded version output, and protected dispatch
  despite a workspace module and ambient `PYTHONPATH`.
- Stale-launcher refusal after installing the second identity, followed by
  receipt-backed refresh through the selected release's full entrypoint.
- Explicit reselection of the prior compatible identity, pending publication
  produced by an in-memory write-boundary fault, a newly reviewed recovery plan,
  and successful recovered dispatch.
- Preservation of profile contents, an absent home directory, and successful
  verification of both installed runtime inventories after all operations.

Eight setup/registration module files matched the source and wheel bytes.
The wheel's private SDK payload and embedded SBOM passed artifact verification.
This is candidate fixture qualification, not an exact published-release audit,
arbitrary cross-version compatibility, or proof of every interruption window.
The fixture parent loaded no provider modules; child-process imports were not
measured by that assertion. No provider or sandboxed tool call was requested.
The native bundle supplied a second installed identity; this test does not
extend its existing host qualification.

## Installed target and session continuity qualification

A subsequent built candidate wheel was installed offline over the retained
fixture runtime on Linux/Python 3.12. Its framework plan reported the caller's
directory when the target was omitted, honored explicit CLI and configuration
targets, and proposed no repository adapter actions beneath the installed
release. Absent plan targets and the fixture home remained absent.

The installed doctor verified all 41 applicable external runtime/build
dependencies. It reported `missing` for a selected runtime without the native
bundle and `present_unprobed` for the retained bundled identity, always with
`execution_tested: false`. Six changed framework/diagnostic module files matched
their source and wheel bytes; SDK payload and embedded SBOM verification passed.

Before upgrade, an isolated installed SDK fixture serialized a two-message
conversation into a real leased session checkpoint. After upgrade, prior-artifact
reselection, and reselection of the new artifact, each installed SDK decoded the
same checkpoint and the public sessions command reported it as settled. Session
file hashes and existing profile bytes remained unchanged, and all three retained
runtime inventories verified afterward. No selection mocks or provider requests
were used; SDK imports occurred only in the isolated serialization fixture worker.

This establishes continuity between the tested candidate artifacts with their
shared session schema, not arbitrary cross-version compatibility or permission
to replay saved operations. It does not replace current-permission checks on
resume, the separate uncertain-operation recovery tests, or exact-release
verification after publication.

## Installed heartbeat planning qualification

The candidate wheel was installed from the audited offline dependency set
outside the checkout. The installed framework resolved a real owned registration
into the selected protected dispatcher for the typed heartbeat profile. Planning
kept the explicit target workspace, omitted prompt text, preserved configuration
bytes, and created no missing provider, grant, state, or home paths.

A changed registration launcher was refused and then restored in the temporary
fixture. With an intentionally broken typed profile, an installed --no-agent
transaction completed without importing the provider SDK or OpenAI client in
that inspection process. Existing saved profile and session file bytes remained
unchanged, and the selected runtime inventory and seven shipped implementation
files matched the wheel and source. Artifact SDK payload and embedded SBOM
validation also passed.

This qualifies installed planning, registration binding, and agent-free
transactions. It does not qualify a coding provider call, native sandbox
execution, compaction, a recurring job, or the eventual exact published release.

## Installed reserved heartbeat qualification

An installed candidate built from the public reserved-action implementation was
qualified outside the checkout with the audited offline dependency set and the
bundled native sandbox. The controller and coding/compaction children ran from
the installed artifact. A deterministic loopback Chat Completions fixture
provided all model responses; no paid provider or authentication was used.

The public harness read and edited a fixture project, then ran its test in the
resource-constrained native sandbox. A second authorized action compacted an
explicit settled checkpoint and continued from the owner-verified destination.
The original checkpoint remained byte-identical. Both actions shared accounting:
the recorded charge was two attempts, six requests, three tool calls, 27000
allocated tokens, 160 allocated seconds and one compaction. An earlier standalone
request prepared the explicit history and was recorded separately from that
accounting policy.

Controller progress review permitted the compound action. A subsequent deliberate
no-progress test disposition stopped another authorized attempt without a model
request. No-agent, disabled and overlapping runs also made no model requests.
These are checks of disposition enforcement; they do not establish an automatic
semantic evaluator or acceptance of an external issue.

Additional installed checks verified cancellation, refusal to replay the
cancelled attempt, malformed compaction output skipping continuation while
retaining both phase allocations, and uncertain session history refusing work
before reservation or provider dispatch. All captured coding and compaction
requests carried the exact runtime framework user-agent. Private evidence
excluded model output, and existing saved profile/session files outside the
fixtures remained byte-identical. Runtime inventory, ten implementation-file
source/wheel/installed hashes, SDK payload and embedded SBOM checks passed.

This evidence qualifies the tested Linux native sandbox and local Chat
Completions interface for these reserved-action flows. It does not establish
live-provider behavior, every endpoint, recurring activation, financial accuracy,
or acceptance of the eventual exact published release. The public action and
controller commands are documented in the [heartbeat configuration reference](../skills/ls-codex-heartbeat/references/config.md#running-a-reserved-action).

## Installed continuation authorization and result recovery

A subsequent installed Linux native-sandbox fixture qualified the public
accounting authorize and reconcile commands with actual protected SDK children
and deterministic loopback Chat Completions. Its initial policy authorized only
the first coding action. After that action created a checkpoint, the controller
recorded progress, planned a compound continuation from the new checkpoint, and
added its authorization to the same accounting chain. No preparatory provider
request or replacement policy was needed.

The compound action compacted that history, then ran a sandboxed test recipe.
That recorded operation advanced the journal beyond the compaction checkpoints.
The fixture inserted an unexpected accounting inventory entry after reservation
to make result recording fail, then removed only that injected entry. The public
reconcile command verified the retained completion and recorded its digest with
zero additional provider requests. The historical source remained non-resumable;
recovery required the current final checkpoint and historical compaction evidence.

The two actions charged two attempts, seven requests, four tool calls, 28000
tokens, 160 seconds and one compaction. Reconciliation preserved those charges,
the original policy and source checkpoint bytes; a subsequent explicit controller
no-progress disposition stopped further work. The twelve relevant installed
module files matched the wheel and source, the runtime inventory and SDK payload
checks passed, and prior qualification profiles and session files were preserved.
Every captured request had the exact framework user-agent identity. This proves
the stated installed fixture behavior; live providers, other host environments,
recurring activation and the final published release remain separate qualification
surfaces. See [heartbeat recovery](../skills/ls-codex-heartbeat/references/recovery.md)
for original-input requirements and missing-evidence handling.

### Protected startup profile binding

A subsequent installed Linux candidate repeated the coding, native test recipe,
compaction and retained-result reconciliation flow above with the bootstrap
profile binding enabled. All seven captured requests retained the exact framework
user-agent identity; fourteen relevant installed modules matched the wheel and
source. Runtime inventory checks passed and existing qualification profiles and
session files were preserved.

Six additional installed cases replaced the selected endpoint, model or
credential-variable name between bootstrap and protected execution, for both
run and compaction. Filesystem notifications confirmed the bootstrap read the
original file and the protected child read its replacement. An exclusive runtime
lease made the replacement deterministic. Each command exited with code 2 before
opening its grant, consuming prompt input, changing session state or contacting
the fixture provider. The run input pipe remained open; compaction's real session
lease remained held until child exit, distinguishing early rejection from a
later missing-session failure. These cases exercised installed entry points and
ordinary filesystem/process interfaces without runtime or provider mocks.

This verifies binding across startup reads. Integrity of configuration before
its first read is a separate trust boundary; a matching digest does not establish
who may modify that configuration. Exact-release verification remains required.

### Runtime configuration integrity

The candidate with trusted profile loading repeated the seven-request installed
coding and compaction flow and verified sixteen source/wheel/installed module
matches, runtime inventory, SDK payload and preserved prior profiles and sessions.
The six startup replacement cases also passed with readable mode-0644 bootstrap
profiles. Six further cases supplied a writable profile or a replaceable parent
directory to run, compaction and direct completion. All refused at bootstrap with
exit code 3, without reading prompt/request input, opening a grant, creating
session state or contacting the deterministic provider. These ordinary POSIX
permission cases qualify the installed Linux reader; unit tests separately cover
opened-inode substitution and simulated ownership. They do not establish support
for foreign filesystem permission models or an exact published release.
