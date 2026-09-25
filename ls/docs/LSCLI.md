---
status: ACTIVE
version: 4.44
owner_skill: ls-architecture
---

<a id="lscli-bootstrap-and-diagnostics"></a>

# LSCli operations and command reference

LSCli is the integrated CLI for LocalSetup (LS). Its command is `lscli`; the
existing framework command and Python distribution remain `localsetup`.
The CLI provides read-only diagnostics, explicit offline setup and registration,
headless or interactive coding, session branches, recovery and compaction.
`localsetup llm complete` provides the separate tool-free completion interface. A verified SDK payload alone does not establish
per-run sandbox, resource, provider or task-authority readiness. Interactive
terminal input and per-tool approvals use the same protected execution boundary.


Use this guide for public commands, exact inputs and outcomes. The
[runtime reference](LSCLI_RUNTIME.md) owns broker, sandbox, journal, worker and
completion schemas. The [qualification record](LSCLI_QUALIFICATION.md) preserves
historical installed-candidate checks; it is not published-release acceptance.

| Operation | Complete contract |
| --- | --- |
| Inspect payload, runtime and profiles | Diagnostics below; [static details](#static-dependency-and-native-capability-details) |
| Create profiles and install audited artifacts | [Profiles](#explicit-profile-configuration-setup), [offline runtime](#explicit-offline-runtime-setup), [native bundle](#optional-native-sandbox-artifact-delivery) |
| Register, refresh or recover PATH command | [Registration](#public-fresh-command-registration), [refresh/recovery](#refreshing-or-recovering-the-registered-command) |
| Run with explicit authority | [Headless run](#explicit-headless-coding-runs), [interactive input](#interactive-terminal-input), [owner control](#inherited-owner-control-socket) |
| Read bounded plaintext or verified encrypted pages | [Granted file reading](#granted-file-reading) |
| Select context and media | [Context/skills](#explicit-context-and-skill-files), [images](#local-image-attachments) |
| Continue, recover, branch or compact history | [Continuation](#session-listing-and-explicit-continuation), [native branch](#native-session-branches), [portable branch](#portable-branches-and-model-changes), [compaction](#compact-a-checkpoint) |
| Request tool-free JSON completion | [Command](#tool-free-completion-command), [request/result schema](LSCLI_RUNTIME.md#direct-completion-contract-foundation) |
| Run typed heartbeat actions and controller accounting | [Owning skill](../skills/ls-codex-heartbeat/SKILL.md#lscli-coding-runs), [configuration and commands](../skills/ls-codex-heartbeat/references/config.md#typed-lscli-profile), [recovery](../skills/ls-codex-heartbeat/references/recovery.md#reserved-result-acknowledgement-recovery) |
| Qualify a compact local task worker through the existing LSCli runner | [Compact worker workflow](../workflows/ls-workflow-lscli-compact-worker/SKILL.md) |

SDK provenance, private payload, dependencies and build/SBOM procedures belong to
[SDK source ownership](SDK_FORK.md#provenance-and-changes). Installation scopes
and preservation of mixed custom/managed adapter content belong to
[adapter ownership](ADAPTER_OWNERSHIP.md#principle). Selecting an optional skill
or typed heartbeat profile does not activate a recurring job or grant new tool,
provider-disclosure or global-policy authority.

## Granted file reading

```bash
lscli read --workspace ./project --grant /private/read-grant.json --path src/example.txt
lscli read --workspace ./project --grant /private/read-grant.json --path src/example.txt --max-pages 8
```

The command requires the same private grant format used by `run`, including
explicit `read` and `disclose` scopes. It makes no provider call. Each stdout
line is one compact JSON page with `content`, `encoding`, `bytes`, `revision`
and `next_cursor`. The default is one page: at most 200 lines or 8 KiB of content
and 16 KiB of serialized JSON. `encoding` is `utf-8` or lossless `base64`.
`--max-pages` selects 1–2048 pages in the same invocation. Reaching that limit
returns success for the emitted pages and reports remaining content on stderr;
denial or unavailable input returns 2. A CLI cursor expires when the process
exits. Rerun with a larger page limit to read more from the beginning.

The SDK tool `read_file(path, cursor=None)` returns the identical page object.
Continue within the live session by passing the returned `next_cursor` unchanged
until it is null. Authority and source revision are checked for every page;
encrypted continuations expire after 30 seconds and cannot be replayed.

This changes the former whole-file SDK result: consumers must loop over pages,
decode `encoding`, and stop at a null cursor. The old `sha256` result field is
replaced by an opaque `revision`; it is not a write precondition. Plaintext
editors needing an original-byte digest must reconstruct and hash the complete
input before calling `write_file`. The reader does not add encrypted writes.

Encrypted files require an `openpgp` object in the private grant, with exact keys:

| Field | Value |
| --- | --- |
| `home` | Absolute private GnuPG home outside the workspace, containing selected decryption material |
| `expected_signers`, `expected_recipients`, `trusted_fingerprints` | Lists of full 40-hex primary fingerprints |
| `owner_fingerprint`, `publisher_fingerprint` | Explicit role fingerprint or null |
| `passphrase` | `{ "provider": "env", "name": "OPENPGP_PASSPHRASE" }` or an `envman` reference |

An Envman reference may include `executable`, an absolute executable outside
the workspace; otherwise it selects `envman` on the protected PATH. Configure
the selected GnuPG home's agent for use before reading. The trusted supervisor
alone receives this authority; model tool arguments cannot change it. ENV
passphrases cross protected startup under a fixed internal name. Envman children
receive only standard path, home/config-location and locale settings, excluding
provider credentials and other environment secrets. Installation and reading do
not enroll keys or change local trust. See the [runtime contract](LSCLI_RUNTIME.md#task-bound-file-broker)
for cache, source-change, error and disclosure behavior.

## Bootstrap and diagnostics

```bash
lscli --help
lscli --version
lscli doctor
lscli doctor --format json
localsetup agent --help
```

The framework entry point `localsetup agent` forwards its remaining arguments
to LSCli. For example, `localsetup agent run --help` and `lscli run --help`
show the same run contract; `localsetup agent sessions --format json` lists
local session metadata. Runs use the same protected runtime, grants, input,
events and exit codes. Place `agent` immediately after `localsetup`; framework
global options before it are rejected. Use LSCli options such as `--workspace`,
`--state-root` and `--runtime-root` on the relevant subcommand. The forwarding
entry point does not translate framework installation selectors into agent authority.

Help and version return 0. Doctor returns 0 when static payload, selected runtime
inventory, dependency metadata, and nonempty profile configuration checks pass; otherwise it returns 3.
Static success does not qualify a run grant, credentials, or resource delegation.
Output failures return 2 and cancellation returns 130. Calling
`lscli` without a subcommand returns 3 with diagnostic guidance on standard error.
Invalid arguments return argparse's status 2. Doctor text or JSON goes to standard
output. None of these commands loads provider/SDK modules, discovers credentials,
makes provider calls, or creates configuration or state directories.

The JSON diagnostic is versioned with `schema_version: 1`. It includes `product`,
`application`, `framework_version`, `status`, `sdk_payload`,
`execution_available`, `execution_implemented`, `runtime`, `profiles`, `locations`, and `issues`. Payload status is `verified`,
`missing`, or `invalid`. Verification inspects the installed private payload's
manifest and files without importing them. Source/editable development has no
installed private payload and never falls back to the canonical vendor tree.
A missing or damaged payload calls for a verified framework wheel installation.
Diagnostic integrity evidence is not artifact authentication.


Doctor accepts `--runtime-root /path/to/runtimes` and
`--profiles /path/to/profiles.json`, independently of the invoking package.
`status` is `static_verified` or `not_ready`; `execution_available` remains false
because doctor authorizes no run. `runtime.status` is `missing`, `incomplete`,
`busy`, `invalid`, or `verified`. Missing roots are absent installations; missing
locks, selection pointers, or completion records are incomplete installations. Invalid records, unsafe
paths/permissions, or changed installed bytes are invalid. Use explicit setup
from verified artifacts or inspect retained recovery records; doctor never
repairs or reselects a runtime. A busy upgrade requires a later inspection.
`profiles` reports only `status` (`missing`, `invalid`, `empty`, `verified`) and
`count`, without names, endpoints, credential references, or credential lookup.
Doctor validates the captured profile document through the same trusted path
reader used for runtime loading. Unsafe file or ancestor ownership/write
permissions therefore report `invalid`, even when schema-only profile inventory
can display the document. Safe readable profiles remain valid. Inspect contents
and filesystem ownership before repairing permissions; doctor changes neither.

Runtime inspection takes an existing shared lease with a one-second acquisition
limit; it never creates a lock or verifies through an active exclusive upgrade.
This is a contention timeout, not an overall inspection deadline: installed
inventory hashing retains its existing entry/byte bounds and filesystem I/O
latency. Selection and completion JSON records are limited to 64 KiB each. Output has a separate five-second write deadline. No worker, provider,
native sandbox probe, or authentication starts during doctor. Functional
sandbox, dependency compatibility, and exact-release qualification remain
separate gates; static integrity alone is not evidence of them.

## State locations

The new CLI follows the existing global framework home under the user's home:

| Purpose | Path relative to the user home |
| --- | --- |
| Durable CLI state and sessions | `.local/share/localsetup/state/lscli` |
| Explicit provider profile configuration | `.local/share/localsetup/config/lscli/profiles.json` |
| Managed release runtimes | `.local/share/localsetup/runtimes/lscli` |

Diagnostics reads these locations without creating them. Offline setup, session persistence,
sandbox protection and supervised headless dispatch are implemented as described
in this guide. Profile creation and receipt-backed PATH registration use explicit
plan/apply operations; refresh and interrupted-registration recovery preserve
owned records and refuse unknown edits.
A reported location does not qualify a run or authorize file/provider access.
Existing framework state, adapter ownership, and stored heartbeat identifiers
are unchanged. See [SDK source and dependency maintenance](SDK_FORK.md) for the
private payload, dependency lock, build, and artifact boundaries.

## Static dependency and native capability details

Doctor reports `runtime.dependencies` after verifying and leasing the
selected runtime. It reads that runtime's sealed runtime/build dependency locks
and distribution metadata, evaluates their environment markers, and compares
installed versions without importing the packages. `verified` includes the
expected dependency count; `mismatch` lists missing or mismatched package names.
Missing, malformed, ambiguous, or oversized metadata reports `unavailable`.
Use the matching verified wheel and locked offline dependency artifacts to repair
the environment; doctor does not install or repair anything. Metadata is limited
to 1 MiB per file, 256 required packages, and 512 distributions.

The static overall success result additionally requires verified dependency
metadata. This checks the selected managed runtime, not arbitrary compatibility
of an ordinary wheel environment. It does not replace artifact authenticity,
installed inventory, or execution qualification.

`runtime.native_sandbox` distinguishes `missing`, `invalid`,
`unsupported_platform`, and `present_unprobed`. It uses the existing bundle
platform contract and inspects the inventoried binary's type and executable bit.
`execution_tested` remains false: no native process, resource delegation probe,
or provider request is launched. Absence of the optional native bundle does not
prevent static success for tool-free setup; tool-enabled runs still require their
qualified backend and resource preflight. Text output shows both check statuses
and actionable guidance. Existing lease and output bounds remain unchanged;
doctor makes no overall inventory-hashing deadline claim.

`runtime.capabilities` statically inspects the selected installed release under
the same read lease. It reports shipped OpenPGP schema identifiers and exported
lifecycle interfaces, bounded reader page fields and limits, Agent Q carrier
files, and the OpenPGP lifecycle and compact worker workflow files. Missing,
malformed, linked or oversized installed metadata reports an affected capability
as missing or invalid. `runtime.capabilities.gnupg` distinguishes a GnuPG
executable found on `PATH` from missing; `present_unprobed` and
`crypto_execution.not_tested` never claim that keys, trust, a provider or an
actual encrypted consumer have been exercised. See the
[shared OpenPGP runtime](OPENPGP_RUNTIME.md) for the current consumer contract.

## Explicit provider profiles and transport

Provider configuration reads a named profile from an explicit JSON file without
creating state or looking for other credentials. Schema version 1 uses this shape:

```json
{
  "schema_version": 1,
  "profiles": {
    "example": {
      "base_url": "https://provider.example/v1/",
      "api": "chat_completions",
      "model": "MODEL_ID",
      "credential_env": "LSCLI_EXAMPLE_KEY",
      "timeout_seconds": 60,
      "capabilities": ["streaming"],
      "allow_loopback_http": false
    }
  }
}
```

Replace the illustrative endpoint and model with an explicitly qualified service.
`api` is `chat_completions` or `responses`. Capabilities are an explicit subset of
`streaming`, `tools`, `images`, `native_schema`, `temperature`, and per-value `reasoning:VALUE`
capabilities in the [completion contract](LSCLI_RUNTIME.md#direct-completion-contract-foundation); declarations do not establish
endpoint qualification or grant tool/disclosure authority. The named credential
variable is resolved only from the environment supplied by the owner. Credential
values are not stored in profile JSON. Credential-bearing loads require a POSIX
path with no symlink components, owned by the current user or root, with no
group/other write permission on the opened regular file or its ancestors.
Root-owned sticky temporary directories are permitted when their selected
children retain trusted ownership. Readable nonsecret files such as mode 0644
are supported. The reader checks the actual opened inode before resolving a
credential; unsupported platforms refuse the load. An unsafe profile requires
owner review of its contents and location before use; the CLI does not change
permissions automatically. Profile inventory and reviewed setup input perform
schema validation without granting runtime trust. Missing credentials fail before transport
construction. Unknown profile fields, duplicate JSON keys, invalid capabilities,
and nonpositive/nonfinite timeouts are rejected. Configuration is limited to 1 MiB;
timeouts are limited to 3600 seconds.

Coding runs and compaction bind the bootstrap credential to a digest of the
complete selected profile before entering protected Python. The protected process
rejects a missing or changed binding before prompt input, session creation or
provider dispatch. If configuration changed during startup, inspect the intended
profile and start a new invocation; the child does not rediscover credentials or
silently use the changed endpoint. This check also covers model, credential
selector, transport and declared-capability changes.

Inspect configured choices without credentials, SDK initialization, network access,
or configuration/state creation:

```bash
lscli profiles --profiles /path/to/profiles.json --format json
lscli run --help
```

Omit `--profiles` to inspect the default profile configuration path shown by
`lscli doctor`. Inventory validates every profile and lists only its name, model,
API, and sorted declared capabilities; it omits endpoint URLs and credential
variable names/values. JSON uses `schema_version: 1` and a `profiles` array sorted
by name. Text output quotes names/models and escapes terminal controls. Inventory is capped at 256
profiles, 256 characters per name, and 1 MiB serialized output. Invalid or missing
configuration returns exit 2 with a sanitized diagnostic and no partial inventory.
No credential-presence or endpoint-readiness claim is made. Select a configured
model with `run --profile NAME` and the required task-grant/runtime options below;
there is no implicit default selection or model override. Incompatible history
still requires an explicit branch; this inventory does not change sessions.

HTTPS uses the locked certifi trust store. HTTP requires explicit opt-in and a
literal loopback IP. URLs cannot carry credentials, query strings, fragments,
escaped paths, or traversal segments. The client disables ambient proxy/CA
settings, implicit SDK credential and organization/project discovery, redirects,
and SDK/transport retries. The adapter clears the pinned SDK's merged custom-header
field before body serialization; dependency updates must retain the ambient-header
regressions because this uses an SDK implementation detail. The final transport permits only POST to the selected
API endpoint and rebuilds its six wire headers (Host, authorization, user agent,
content type, accept, and content length) after SDK request construction. Serialized
JSON requests are limited to 16 MiB; ambient custom headers cannot pass through. Identity is exactly `LocalSetup/<framework_version()>`; SDK
telemetry-identification headers are removed. There is no fallback endpoint.

Deterministic transport fixtures cover both APIs, final headers, a single attempt
on rate limiting, and redirect refusal. No live provider compatibility is implied.
The configured timeout is an HTTP operation timeout; supervisor wall-clock budgets,
stream/output bounds, cancellation, and task-bound disclosure grants remain
enforced by the [coding controller](LSCLI_RUNTIME.md#supervisor-owned-coding-controller)
and [completion supervisor](LSCLI_RUNTIME.md#protected-completion-worker). The shared
client serves their public commands and the QC compatibility wrapper.

## Explicit profile configuration setup

Profile setup accepts an explicit version-1 profile document using the same
schema as runtime profile loading. It does not infer a provider, model,
capability, endpoint, or credential. Supply credential environment variable
names, never credential values. The input must contain 1–256 valid profiles;
duplicate JSON keys, unknown fields, and oversized documents fail validation.
See the [provider profile schema and example](#explicit-provider-profiles-and-transport).

```bash
lscli setup --plan --profile-input ./profiles-input.json --profiles /path/to/config/profiles.json
lscli setup --apply --profile-input ./profiles-input.json --profiles /path/to/config/profiles.json --profile-sha256 DIGEST_FROM_PLAN
```

Omitting `--profiles` uses the default profile location in the state table.
These profile-only operations require no wheel, runtime root, credentials, or
provider calls; combining them with runtime installation options is invalid.
Plan emits version-1 JSON with `operation: create_profiles`, target path,
canonical `sha256`, `profile_count`, and `expected_target: absent`. It creates
no parent directories, configuration, backups, or lock files and emits no profile
names or endpoint/credential-reference values. Equivalent canonical input has
the same digest; apply validates it again against the supplied plan digest.

Apply creates missing private parent directories through anchored, no-follow
traversal. It writes a complete mode-0600 temporary file, flushes it, and publishes
with atomic no-replace creation. An existing target—including a dangling symlink,
FIFO, or competing writer's file—is a conflict and remains untouched. Unsafe
parent ownership or permissions fail. The create-only operation never merges,
updates, renames, or deletes existing profiles. Use a separate target for a new
configuration and select it explicitly; existing configuration changes require
an independently reviewed edit and recovery procedure.

Success returns 0; validation, collision, or output failure returns 2;
cancellation returns 130. Output writes have a five-second deadline. Failure or
interruption after publication can leave a complete target despite a failed
command outcome: inspect its contents before retrying. A retry will not overwrite
it. Interrupted creation may leave newly created empty parents or a private
`.profiles-*` temporary file; preserve and inspect these artifacts before manual
cleanup. This does not install a runtime or register a shell command.

## Explicit offline runtime setup

Setup can plan or apply a runtime installation from a specific verified framework
wheel and a local directory containing its audited external artifacts. It does
not select a release from the network, download Python, discover credentials, or
make provider calls. Obtain the expected SHA-256 from the trusted release source;
a hash computed from an untrusted download does not authenticate it.

```bash
lscli setup --plan --wheel /path/to/framework.whl --sha256 EXPECTED_SHA256 --wheelhouse /path/to/dependency-artifacts
lscli setup --apply --wheel /path/to/framework.whl --sha256 EXPECTED_SHA256 --wheelhouse /path/to/dependency-artifacts --timeout 300
```

Replace all placeholders. Both commands emit JSON to stdout and return 0 on
success; setup errors produce diagnostics on stderr and return 2. Keyboard
interruption returns 130 after command teardown. Plan validates
artifact identity, SDK payload, local inputs, and workspace separation without
creating persistent runtime state. The result records the effective workspace,
framework version, artifact digest, runtime root, and release path. The default
root is the managed runtime path above; `--runtime-root` selects an explicit
alternative. The runtime must lie outside the current directory and every
enclosing repository, and cannot contain that workspace. Wheel input is limited
to 256 MiB; ambiguous or oversized distribution metadata is rejected.

Apply acquires an exclusive runtime lease, creates a digest-named release slot,
and writes an incomplete installation record. It creates the environment at its
final path, avoiding virtual-environment relocation. Build and runtime dependencies
come from the wheel's hashed exports and local artifacts only. Build tools are
preloaded; build isolation is disabled so no implicit build dependencies are
resolved. The framework wheel installs without dependency resolution. Dependency
compatibility and installed SDK payload checks must pass before activation.
The managed CLI launcher invokes the absolute installed Python with `-I -B`,
preventing inherited Python path settings, the working directory, or user site
hooks from selecting checkout code at startup. This rewrites only the framework
CLI launcher; the isolated SDK worker and sandbox still require separate checks.

Only a completed installation replaces `current.json`, using an atomic regular
file replacement and filesystem flushes. The record retains the previous digest,
and prior release directories and sessions remain untouched. Installation
commands share one deadline after preflight, suppress output, and terminate their
process groups on completion or interruption. A failed command reports failure
and retains the incomplete slot for inspection; it does not replay or remove it.
If the process stops near activation, inspect `current.json` and the slot's
`status.json` before deciding what happened. Existing slots are refused, including
completed slots. Use explicit verified re-selection to recover a prior slot:

```bash
lscli setup --reselect RELEASE_SHA256 --runtime-root /path/to/runtimes
```

Re-selection holds an exclusive lease, verifies the completed record and installed
inventory, and atomically changes selection without replaying installation. It
refuses incomplete, unsealed, or altered slots and preserves the current pointer
on validation failure. Do not manually edit records to bypass validation.

Installation copies dependencies without cache hardlinks and runs build commands
with a private umask. Before activation it seals the environment's exact paths,
file hashes, modes, allowed interpreter aliases, and host interpreter bytes.
Selection verifies this inventory under the shared lease; added files, changed
content, unsafe permissions, and unexpected symlinks or hardlinks are refused.
The managed launcher disables bytecode writes to keep the inventory stable.
A host interpreter update invalidates that qualification and requires a new
verified runtime installation. This integrity check detects drift; it does not
provide artifact authentication or prevent a same-user writer from racing it.

The internal `selected` context holds a shared lease and checks the selected
completion record for the entire caller-owned operation. Runtime installation, integrity-checked selection, worker supervision, and
sandbox protection are integrated. Successful setup does not supply task grants,
validate credentials, or establish per-run sandbox/resource readiness.
The installer assumes the caller trusts the uv executable and supplied release
digest; artifact authenticity and platform qualification remain external gates.

The Python 3.12/Linux candidate also passed offline installation outside the
checkout, inventory verification before and after managed doctor execution with
an inherited checkout `PYTHONPATH`, explicit re-selection, and rejection of a
tampered installed file while preserving the selection pointer. This qualifies
candidate integrity and recovery behavior only; it is not released-artifact or
sandbox qualification.

## Optional native sandbox artifact delivery

Offline setup accepts `--sandbox-bundle /path/to/native.zip` together with
`--sandbox-sha256 TRUSTED_SHA256` on both `--plan` and `--apply`. These inputs
cannot accompany `--reselect`. The expected outer digest must come from the
trusted artifact publisher. Setup does not discover or substitute a host
`bwrap`, download native code, or execute the bundle during planning.

The schema-1 ZIP contains exactly four regular, nonempty entries: `bwrap`,
`bubblewrap-COPYING`, `libcap-copyright`, and `manifest.json`. Compressed and
expanded content are each limited to 16 MiB; the manifest is limited to 16 KiB.
Duplicate keys, extra entries, symlink entries, mismatched hashes and unsupported
component identities are rejected. The manifest has exactly these fields:

- `schema_version`: integer `1`.
- `target`: `{"os":"linux","machine":"x86_64","libc":"glibc","minimum_libc":"2.39"}`.
- `components`: exact bubblewrap and static libcap version/input-digest records
  defined by `ls/core/agent/native_bundle.py`.
- `files`: SHA-256 values for the three payload entries, keyed by their exact names.

This delivery contract accepts bubblewrap 0.12.0 and the specified patched Ubuntu
libcap build input. It requires Linux x86_64 with glibc 2.39 or newer; those checks
are installation prerequisites, not a claim that every such host is functionally
qualified. The authenticated bundle supplies the executable's provenance;
manifest strings and an ELF header alone do not establish trustworthy code.
Native release production must retain corresponding source, licenses, build
provenance and SBOM evidence and satisfy redistribution requirements before
publication. No native release artifact is published by this setup operation.

Produce the external CycloneDX 1.6 sidecar from an independently trusted ZIP
without executing it or requiring the inspecting host to support the runtime:

```bash
uv run --locked python ls/tools/native_sbom.py emit --bundle native.zip --sha256 TRUSTED_SHA256 --out native.cdx.json
uv run --locked python ls/tools/native_sbom.py verify --bundle native.zip --sha256 TRUSTED_SHA256 --sbom native.cdx.json
```

Emission validates the complete bundle before creating a new output file and
refuses to overwrite an existing file. Verification revalidates the ZIP and
compares the complete component, hash, license, target and dependency records;
extra or missing records fail. The deterministic sidecar binds the outer ZIP
and all four entry hashes. It stays outside the ZIP, preserving its schema and
runtime identity. Runtime installation still enforces the host platform guard.

Bubblewrap and statically incorporated libcap are separate components. The
supported source and notice baseline supplies the license expressions; both
notice hashes are pinned by the SBOM owner. Source-archive and distro `.deb`
input digests are identified separately from executable output hashes. The
libcap `.deb` digest is not corresponding source, the linked static archive hash,
or an output-library hash. Linux, architecture and minimum host glibc are
requirements, not components claimed to ship in the ZIP.

This verifies inventory and supported input declarations, not the actual build
process, complete toolchain closure, independent reproducibility, advisory
status or redistribution compliance. Public source and build provenance evidence
and qualification of the exact released bytes remain separate release gates.
The framework/SDK SBOM does not cover this optional native ZIP.

With a bundle, the runtime slot identity is the SHA-256 of the UTF-8 string
`lscli-runtime-v1\nWHEEL_SHA256\nBUNDLE_SHA256\n` with placeholders replaced by
lowercase digests and `\n` represented by newline bytes. Plan and installation
records expose this identity as `sha256`, retain the framework digest separately
as `wheel_sha256`, and record `sandbox_sha256`. Without a bundle, existing
wheel-digest slot identities and records remain compatible. Re-selection takes
the runtime slot identity, so two native variants never overwrite one another.

Apply rechecks and holds the authenticated bytes in memory, writes the payload
under `venv/lscli-native`, and seals binary, manifest and license content and
permissions with the environment inventory before activation. Changed native
files invalidate selection and recovery just like changed Python files. The
binary is private and executable; other entries are private regular files.
A bundle-less runtime remains usable for provider-free diagnostics. Bundle delivery does not by itself qualify tool execution. Every public coding
run still performs [native/resource preflight](LSCLI_RUNTIME.md#provider-free-tool-preflight)
and uses the supervised file/process broker contracts.

## Public fresh command registration

Use an explicit private bin directory already present in PATH. The command does
not edit shell startup files or adopt an existing command. Review the JSON plan
and use its `plan_sha256` unchanged for application:

```bash
lscli setup --plan --bin-dir /path/to/private/bin --runtime-root /path/to/runtimes
lscli setup --apply --bin-dir /path/to/private/bin --runtime-root /path/to/runtimes --registration-sha256 PLAN_SHA256
lscli setup --registration-status --bin-dir /path/to/private/bin
```

Plan and apply use the established default runtime root when omitted, or the
registered dispatcher's bound root. Status uses the receipt's root and rejects a
runtime override. All registration modes require an explicit `--bin-dir`; they
cannot mix profile creation, runtime artifact inputs, reselection, or custom
timeouts. Application requires the reviewed digest. Plan and status create no
configuration or command state. A plan can report `path.ready: false`; inspect
this field before applying, which refuses an ineffective PATH position.

Output is one JSON object. Successful plan/apply returns 0; status returns 0 only
for `registered`, or 3 for other reported states. Errors return 2 with a generic
diagnostic, and cancellation returns 130. Output writes have a five-second bound;
existing owner lease bounds apply, with no overall inventory-hashing deadline
claim. A failed output write after application can leave a successful registration:
inspect status and retained records before attempting recovery. Status checks
registration integrity and release selection, not current PATH precedence.

The public fixture checks call the real CLI and filesystem owner with a mocked
runtime selection. Installed candidate qualification is retained separately. Use the explicit
owned refresh/recovery modes below for an existing or stale launcher; fresh
registration never replaces one.

## Refreshing or recovering the registered command

After installing or explicitly selecting another verified runtime, invoke that
selected release's full entrypoint path. The stale registered launcher refuses
dispatch. Plan the refresh first, then apply its `plan_sha256`:

```bash
/path/to/runtimes/RELEASE_SHA256/venv/bin/lscli setup --plan --refresh-registration --bin-dir /path/to/private/bin
/path/to/runtimes/RELEASE_SHA256/venv/bin/lscli setup --apply --refresh-registration --bin-dir /path/to/private/bin --registration-sha256 PLAN_SHA256
```

For a pending fresh registration or refresh, use `--recover-registration` in
place of `--refresh-registration` to inspect a new recovery plan, then apply
that plan's digest. Recovery inspects the current files and refuses unknown edits;
do not reuse the original fresh/refresh plan digest as a recovery grant. If the
intended runtime is no longer selected, reconcile selection through the explicit
verified-runtime recovery procedure first. These commands do not switch runtimes.

Both modifiers require `--plan` or `--apply`, an explicit bin directory, and
the receipt's recorded runtime root. Runtime overrides, registration status,
profile/artifact inputs, and combining the modifiers are invalid. Apply requires
`--registration-sha256`. JSON output, exit codes, bounded output, and uncertainty
handling follow the fresh registration interface. Review retained evidence after
an interrupted apply, including when the command succeeds but output fails.

## Framework commands invoked from a wheel

The framework's installed `localsetup` entrypoint keeps wheel resources as its
source and defaults repository-targeting commands to the caller's Git root, or
the current directory outside Git. It identifies a wheel invocation by matching
the running CLI module to the distribution's recorded file, rather than treating
an installed package directory as a source checkout. Editable/source-checkout
invocations retain their existing source-root default.

An explicit `--target-directory` takes precedence. For wheel invocations, a
target in an explicit configuration file is retained as well. This behavior
applies to the existing target-aware plan, install, update, verify, rollback,
adapter, doctor, migration, context, conversion, harness, context-index,
provenance, and health commands. Other commands retain their existing behavior.

Use `localsetup plan --preset core --platforms codex` from the intended project
and inspect the reported attachment root before application. Planning must not
propose repository adapter writes into the installed package tree merely because
that tree supplies the framework resources. This target default does not change
the separate LSCli runtime, profile, session, or registration location contracts.

## Explicit headless coding runs

```bash
lscli run --profile coding --profiles /private/config/profiles.json \
  --grant /private/config/task-grant.json --resource-parent "$CGROUP_PARENT" \
  --workspace "$PROJECT" --format jsonl --prompt-stdin < prompt.txt
```

The profile uses the [explicit provider schema](#explicit-provider-profiles-and-transport).
Only the selected credential value is passed to sealed Python through a fixed
internal environment key. The configured source variable name is never forwarded
as a loader or interpreter option. There is no credential search or provider fallback. Public coding currently
supports the qualified Chat Completions and Responses interfaces. Prompt input itself authorizes
disclosure of that prompt to the selected profile. No workspace context is loaded
automatically. File reads, writes and provider disclosure remain distinct grants.

The grant is an explicit private, owned, regular JSON file outside the workspace:

```json
{
  "schema_version": 1,
  "read": ["src", "tests"],
  "write": ["src"],
  "disclose": ["src", "tests"],
  "recipes": {
    "test": {
      "command": ["/usr/bin/python3", "-I", "-B", "-m", "unittest", "discover", "-s", "tests"],
      "files": ["src/main.py", "tests/test_main.py"],
      "seconds": 30
    }
  }
}
```

Scopes are canonical relative paths; `.` explicitly covers the workspace subject
to protected-path restrictions. Recipe input files are exact snapshot paths, and
commands use canonical `/usr/bin` executables. The [file broker](LSCLI_RUNTIME.md#task-bound-file-broker)
refuses protected/private paths and writes to `AGENTS.md`; these defaults do not
identify every possible secret. Scope grants accordingly. The
[sandbox contract](LSCLI_RUNTIME.md#process-sandbox-invocation-foundation) exposes
the trusted host `/usr` tree read-only, so it must contain no private material.
Process writes are disposable;
workspace edits use the file broker. Empty recipe inventories permit file-only
work; an ungranted command request is refused. Configurations and credentials are
not included in saved task authority.

`--runtime-root`, `--state-root` and `--profiles` override the documented default
locations. `--resource-parent` must identify an already-qualified delegation;
there is no implicit host repair. New run state creates private sessions, leases,
snapshots and scratch directories. Run configuration must stay outside the
workspace, and runtime/state trees must be separate. The bootstrap replaces itself
with the selected sealed interpreter; a changed runtime selection is refused
before provider dispatch. No SDK installation or download occurs during a run.

`--task` and `--session` optionally provide bounded identifiers; omitted values
are generated. These identify a new run's durable evidence, not permission to
resume old history. `--timeout` bounds input and execution (default 300 seconds,
maximum 3,600); request/tool/token caps default to 8/16/32,768 and may be lowered or
explicitly changed with `--request-limit` (1–64), `--tool-limit` (0–256) and
`--token-limit` (1–1,048,576). Reported token counts can expose an excess only
after a response; they are not a provider billing guarantee. Stdin must reach EOF and
contain at most 128 KiB of UTF-8 prompt text. SIGINT/SIGTERM cancel current work.

Text mode emits tool progress and safely rendered final text. JSONL emits ordered
schema-version-1 `start`, `progress` and terminal `result` records with a monotonic
`sequence` and `data` object. Progress contains native SDK events; consumers must
treat their contents as untrusted data. JSON escapes terminal controls. Results
include status, task/session and, on success, output and a checkpoint reference.
Standard error carries bounded generic diagnostics without credentials or provider
exception text. Slow/closed output is bounded and stops the run; absence of a
terminal record is not success. Cancellation/deadline terminal delivery has a
separate one-second window and may fail if the consumer is unavailable.

Successful runs exit 0; cancellation exits 130, deadline 124, process failure 1,
output-limit 5, validation/runtime failure 2, and bootstrap readiness failure 3.
Argument errors use argparse's status 2. Each coding run still performs actual
sandbox/resource preflight before contacting a provider. The explicit grant is
the normal tool authority; optional approvals narrow it. Interactive input, owner
control and explicit context loading are documented below.

Use `--require-new-session` to atomically require an absent session. This option
is mutually exclusive with `--resume` and `--recover-from`, and excludes explicit
context, skills and images. It does not make an existing session new or restore
its authority; typed heartbeat fresh actions use this same boundary.

## Session listing and explicit continuation

`lscli sessions --format json` lists metadata under the default state root;
`--state-root` selects another explicit root. Listing does not initialize a
provider, read credentials, return conversation content or create missing state.
It reports `settled`, `uncertain`, `busy` or `invalid` entries, with task/session
and operation counts when a consistent private identity can be read. Busy entries
are identified by their storage digest. Custom non-session entries are counted
and preserved. Inventory is capped at 1,000 entries and uses a five-second
inspection/output budget with deadline checks between journal records.

To continue a compatible complete checkpoint, add `--task`, `--session` and
`--resume CHECKPOINT` to the explicit headless run command. Use the recorded task,
session and original workspace. The flag authorizes disclosure of that exact
history, together with the new stdin prompt, to the selected compatible profile.
It does not restore old file grants or command permissions: the current grant
file and current limits still govern every new tool operation. Changed profiles
are refused; portable branching is a separate interface.

`--recover-from CHECKPOINT` is mutually exclusive with `--resume`. It first
reconstructs the interrupted checkpoint locally using settled journal evidence
and durable tool receipts, then continues with a fresh grant covering the
recovered history and new prompt. Missing output, uncertain operations and
incompatible evidence refuse continuation rather than replaying a tool.
Unknown sessions fail without creating a new session. No checkpoint or journal
is overwritten. The successful terminal result carries the new checkpoint.

Both continuation forms require explicit recorded task and session identifiers;
a supplied session name alone does not select history. Session listing does not
choose a checkpoint automatically. Use explicit branching or compaction commands below when needed;
there is no automatic checkpoint selection UI.

## Inherited owner control socket

`lscli run --control-fd FD` accepts an explicitly inherited, connected Unix
stream socket with descriptor number at least 3. A parent creates a socket pair,
passes one end through its process launcher (for Python, `pass_fds=(fd,)`), then
closes its copy of that end. Standard input still supplies the initial prompt;
standard output still carries run results/events. The protected supervisor
consumes the descriptor and prevents inheritance by its worker/tool children.

Send UTF-8 JSON objects, one per newline, with exactly these fields:

```json
{"schema_version":1,"id":1,"method":"status"}
{"schema_version":1,"id":2,"method":"cancel"}
```

IDs start at 1 and increase by one. Responses retain the ID and schema version:

```json
{"schema_version":1,"id":1,"status":"active"}
{"schema_version":1,"id":2,"status":"cancellation_requested"}
```

`active` means the cancellation flag is unset; it is not a readiness or progress
claim. Cancellation acknowledgement is not a terminal result. Read the normal
result event and process exit code to determine the actual outcome. Requests
never grant file access, provider disclosure, or shell execution. Approval decisions require the opt-in per-tool gate described below. Steering
accepts only the explicit text-disclosure contract below.

Each request is at most 16 KiB before its newline, with at most 1,024 requests
and 1 MiB total input per run. Replies have a 250 ms deadline capped by the run
deadline. Invalid schema, duplicate keys, skipped/repeated IDs, oversized input,
broken replies, and EOF request cancellation. The supplied control channel is
an owner-liveness contract: closing it stops new dispatch through the shared
cancellation state. Keep reading replies to avoid cancellation on backpressure.
The run deadline retains timeout classification. A run without `--control-fd`
continues to use its normal signal and deadline controls.

## Task-bound steering

After the run's `start` event, the owner may send:

```json
{"schema_version":1,"id":3,"method":"steer","task":"TASK","session":"SESSION","profile":"PROFILE","text":"Please also explain the test result.","disclose":true}
```

Use the next consecutive control ID and the exact task, session and selected
profile name from the start event. `disclose: true` authorizes sending this text
to that run's provider; it grants no additional workspace reads, writes or shell
recipes. The reply status `queued` acknowledges receipt, not provider delivery.
The supervisor checks current authority when the SDK polls immediately before a
model request. It drains messages in order into native user-prompt parts, which
are included in subsequent SDK checkpoints. An in-flight model request or tool
operation is not interrupted by steering; use cancellation to stop work.

Text is limited to 8 KiB per message, 32 messages and 128 KiB cumulative per run,
in addition to control-frame and run usage limits. Unbound or mismatched identity,
missing disclosure consent and exceeded bounds invalidate the control channel
and request cancellation. Queued messages are memory-only until incorporated
into checkpointed history. Messages arriving after the last poll may remain
undelivered; failed or cancelled runs do not automatically replay them. Inspect
session history before explicitly resubmitting uncertain input. Steering text in
a resumed conversation remains context and never restores its former authority.

## One-use tool approvals

Add `--approve-tools --format jsonl --control-fd FD` to require owner confirmation
before each file read, listing, search, context refresh, replacement or process recipe request. This mode
narrows existing grants; approving an otherwise forbidden request does not make
it executable. Without this flag, explicit grant-file authority remains the
normal tool policy. Approval mode requires JSONL output and a control socket.

An `approval_request` event contains the task, session, profile, a fresh
`challenge`, a `sha256`, and a complete `request` object. The request includes the
broker method and arguments. Process requests also include the named recipe's
exact command, input files and time limit. File replacements include their full
new content and expected previous hash. The preview is limited to 128 KiB;
larger requests fail before execution rather than asking for approval of a
truncated preview. Normal output and run limits still apply.

Inspect that concrete request, then send the next consecutive control ID:

```json
{"schema_version":1,"id":4,"method":"approve","task":"TASK","session":"SESSION","profile":"PROFILE","challenge":"CHALLENGE","sha256":"SHA256","allow":true}
```

Use `allow: false` to deny it. The response status `decision_recorded` confirms
receipt, not execution. A denial fails the run without dispatching that tool.
Unknown, repeated or mismatched decisions invalidate the channel and request
cancellation. Approval is consumed once; it cannot authorize a later request,
session or resumed run. Current grants, lease state, cancellation and deadline
are checked while waiting and again before dispatch. No response waits beyond
the run deadline; owner EOF cancels the wait. Read the normal terminal event and
process exit status for the actual outcome. Approval requests are local owner
output, not additional instructions sent to the model.

## Interactive terminal input

Use `lscli run --interactive` instead of `--prompt-stdin`, retaining the same
explicit profile, grant, workspace and resource-parent options. Both standard
input and output must be terminals. Interactive mode uses text output, excludes
an external control descriptor, and always requires per-tool approval. It does
not discover credentials, load ambient context or broaden the grant file.

Enter multiple lines, then type `/send` on a separate line to submit. A line
containing `/cancel` aborts entry. During execution:

| Input | Effect |
| --- | --- |
| `/steer TEXT` | Explicitly disclose additional text to this run's provider at its next request boundary. |
| `/approve CHALLENGE` | Approve the exact pending tool request with that displayed challenge. |
| `/deny CHALLENGE` | Deny that pending request and fail the run before executing it. |
| `/cancel` | Request cancellation of active work. |

Tool requests appear as complete escaped JSON data followed by their approval
and denial commands. Inspect the request before choosing; a response merely
records the decision, and current grants still govern execution. Unknown commands
or nonmatching challenges do not approve anything. There is no implicit approval
on Enter. Output escapes terminal control characters in model text and request
data. Terminal input retains the terminal's normal line-editing behavior.

The initial prompt retains its 128 KiB limit. Terminal input is bounded to
256 KiB for the whole run and 16 KiB in its line buffer; the host terminal may
impose a smaller line limit. Steering and approval limits remain as documented
above. The run deadline includes time spent typing and approving. EOF, terminal
disconnection and Ctrl-C stop work; expiry remains a timeout. Input reader shutdown
is bounded, and no new background job survives the command. This interface runs
one task; session continuation uses explicit `--resume` or `--recover-from` with
fresh grants on the next invocation.

## Explicit context and skill files

Use repeated `--context PATH` and `--skill PATH/TO/SKILL.md` options on `run` to
select workspace-relative files. For example, append these to either headless
or interactive run arguments:

```bash
--context AGENTS.md --context src/CONVENTIONS.md --skill skills/review/SKILL.md
```

The current grant must cover each path in both `read` and `disclose`. Selection
does not add either permission. Files are read under a fresh session owner and
broker lease before provider dispatch. Protected paths, symlinks, unsafe file
types and uncertain sessions retain the existing broker refusal behavior. A
failed load makes no provider request, though explicit run state may already
have been created. Context reads do not create mutation journal entries.

At most 16 distinct files may be selected: 16 KiB per file and 64 KiB total UTF-8
content. The combined prompt, including escaped resource metadata, remains at
most 128 KiB. A skill selection must name `SKILL.md`; the file is loaded as text,
without executing scripts, validating a vendor-specific skill dialect, following
links or loading adjacent resources. Select supporting files separately. There
is no ambient skill discovery or automatic context-directory traversal.

Each snapshot records its kind, relative path, content hash and exact text in the
user prompt, so successful SDK checkpoints preserve the context actually sent.
The snapshot is fixed for that invocation. Current grants remain external to
context: instructions in a selected file cannot authorize a tool or additional
disclosure. Resuming with selected files requires fresh permissions and takes
new snapshots; previous snapshots remain historical conversation content.
Use `refresh_context(directory)` during a run to reload nested instructions as
described below; initial selections remain fixed snapshots.

## Broker text search

The coding agent can call `search_files(paths, text)` to search an explicit list
of workspace-relative UTF-8 files. Matching is literal and case-sensitive, without
regular expressions, shell execution, glob expansion or implicit directory
traversal. The current grant must cover every selected path for both reading and
provider disclosure. Existing symlink, protected-path, ownership and uncertain
session restrictions apply; a rejected search supplies no partial result.

The tool accepts 1–32 distinct paths and a nonempty single-line query of at most
1 KiB. Total searched content is at most 4 MiB. Its result contains:

- `files`: selected paths and the SHA-256 of each content snapshot.
- `matches`: path, one-based line number, up to 512 characters of line text, and
  `text_truncated` for longer lines.
- `truncated`: whether the match count or output budget omitted further matches.

At most 100 matches and 256 KiB of encoded result are returned. A truncated line
may omit the matching portion; use a separately granted read to inspect the full
file. An empty match list with `truncated: false` means the literal text was absent
from those snapshots, not from the whole project. Search results are snapshots;
normal file preconditions still govern subsequent writes. Search uses the same
per-tool approval gate when enabled and consumes a tool call, but creates no
mutation journal entries. Directory discovery is not provided by this tool.

## Directory discovery

The agent can call `list_files(path)` for direct children of an explicitly
readable and disclosable directory. Use `.` for the workspace root only when
both grants cover `.`. Permission for an individual file does not permit listing
its parent. The tool does not recurse: select a returned directory explicitly
for another listing or pass returned file paths to `search_files`.

Results contain `path`, sorted `entries` with each child's relative `path` and
`kind` (`file` or `directory`), and `truncated`. The broker scans at most 4,096
entries and returns at most 256 KiB. If either limit is reached, `truncated` is
true; the partial selection follows filesystem enumeration order before sorting.
Protected paths, symlinks, hard-linked files, special files, entries owned by
another user, and entries with special permission bits are omitted. No omitted
names or counts are returned. Such omissions mean an empty result is not proof
that the physical directory is empty.

Listing uses an anchored directory descriptor and the existing read lease. A
change to directory metadata during enumeration refuses the result. Current
read/disclosure and session authority are checked before returning either a full
or truncated result. Approval mode shows the exact selected directory before
listing. The operation consumes a tool call and creates no mutation journal
entry. Returned names do not grant permission to read or execute those entries.

## Refreshing nested instructions

The agent can call `refresh_context(directory)` when entering a directory or
when repository instructions may have changed. Use a canonical workspace-relative
directory, or `.` for the workspace root. The tool examines `AGENTS.md` at the
root and at each successive directory level through the selected directory,
returning present files in root-to-leaf order. The response contains `directory`,
`resources` (path, content and SHA-256), and `missing` candidate paths.

Every candidate path, including one whose file is absent, requires current read
and disclosure permission. This prevents existence checks from bypassing grants.
The tool accepts at most 16 instruction levels, 16 KiB per present file, 64 KiB
total content and 256 KiB encoded output. Missing files are reported; symlinks,
unsafe types, invalid UTF-8, permission failures or uncertain session operations
refuse the result. All candidate permissions are checked again before delivery.
Individual files are coherent snapshots; the collection is not a filesystem-wide
atomic snapshot.

Refresh is an explicit tool call, not a filesystem watcher. It reads current
bytes each time and consumes a tool call. Approval mode displays the complete
candidate path list before access. Returned instructions enter the model's normal
conversation and checkpoints; they cannot modify grants or install hooks, run
scripts, or load sibling resources. Older snapshots remain historical messages.
The agent should refresh before acting in a newly selected directory and after
learning that its instruction files changed. LocalSetup does not infer new
provider-disclosure authority from the presence of an instruction file.

## Local image attachments

Repeat `--image PATH` on `run` to attach explicitly selected workspace-relative
PNG or JPEG files to the initial user prompt. Both headless and interactive input
support attachments. The profile must declare `images` in its capabilities, and
the grant must permit reading and disclosing every selected image. Selecting an
image does not grant either permission or enable a provider capability.

At most four distinct files may be attached, each at most 512 KiB, for 2 MiB
total. The broker reads owned, regular, single-link files through anchored paths;
protected paths, symlinks, uncertainty and expired authority retain their normal
refusals. File bytes, relative path, media type and SHA-256 are bound into the
explicit coding request. The isolated worker passes decoded bytes as native SDK
binary content, and message checkpoints retain the actual image data.

Validation checks the transport envelope, byte bounds, hash, and PNG/JPEG
signatures. It does not decode pixels or prove codec validity; malformed content
may still be rejected by the provider. No remote image URLs, downloads, format
conversion, local image viewer or image-generation service are involved. Provider
support is explicit configuration and must be qualified for the selected endpoint.
Deterministic Chat Completions fixtures qualify binary request serialization;
they do not establish visual understanding by a live model.

Explicit attachments are owner-selected initial context, separate from model
requested tool reads and their optional approval prompts. A missing image grant
or capability fails before any provider request. Resume retains historical images
and may add newly selected attachments under fresh authority; changing to an
incompatible profile still requires a portable branch rather than altering the
original session history.

## Native session branches

Copy a settled checkpoint into a new session without a provider call:

```bash
lscli branch --source-task TASK --source-session SESSION --checkpoint SHA256 --task NEW_TASK --session NEW_SESSION --profile example --profiles /path/to/profiles.json --workspace /path/to/project --state-root /path/to/private/state
```

The destination session must never have existed, including as an empty directory.
The source must have no uncertain operations and its checkpoint must match the
current journal frontier. The normalized profile must match exactly; changing
provider, model, API, capabilities, credential-variable name or timeout is refused.
Use explicit `--portable` for supported history when changing profiles, as described below.

A successful command returns a schema-1 JSON receipt with `mode: "native"`,
source task/session/checkpoint, destination task/session/checkpoint, and profile
digest. The same receipt is retained as private `branch.json` in the destination.
SDK history bytes are preserved exactly; the new checkpoint binds them to the
new session and its empty operation journal. Original history is unchanged.
No file grants, approvals, credentials or operation journal are copied. Resume
with the returned checkpoint, destination identities and a fresh `run` grant.
Saved conversation text and historical tool results confer no authority.

Branching uses the source lease and an exclusively reserved destination; target
lock acquisition never waits while holding the source lease. The default deadline
is 30 seconds (`--timeout`, maximum 300). Invalid input, incompatible/stale history,
existing destinations or failed writes return exit 2; cancellation returns 130.
A failed or interrupted command can leave destination evidence, including a saved
checkpoint. Inspect that evidence before another attempt; an existing destination
is never overwritten or automatically retried. Output failure does not roll back
a completed branch. Unknown source state is not created.

## Portable branches and model changes

Add `--portable` to `lscli branch` and choose the destination `--profile NAME`
to change model/provider configuration. `--runtime-root` selects the protected
runtime used for local conversion (default: the standard LSCli runtime location).
No credentials are resolved and no provider is contacted during conversion.
The original checkpoint's profile is read from its verified evidence; the new
checkpoint binds the selected target profile. The receipt identifies both
`source_profile` and destination `profile`, with `mode: "portable"`.

The isolated SDK worker validates native messages and serializes one new user
context containing an ordered JSON transcript. User and assistant text, historical
system text, tool-call names/arguments/IDs, tool results/outcomes, and retry text
remain historical data. Tool calls are not executable messages and historical
system text is not installed as system instructions. Message/provider metadata,
request instructions, timestamps and provider response identifiers are not copied.
The original checkpoint retains that native evidence. No conversion infers grants,
replays operations, changes files, or copies the source operation journal.

PNG/JPEG binary attachments retain their bytes with transcript references and
hashes; at most four images of 512 KiB each are accepted, and the target profile
must declare `images`. URLs, audio/video/documents, reasoning parts, and other
unsupported message/content types cause conversion to fail before destination
creation. There is no silent truncation or fallback. Both native input and
converted history are capped at 8 MiB; JSON escaping can make conversion exceed
the limit even if its input fits. The worker's acknowledgement and successful
process outcome must agree before a destination checkpoint is accepted.

Resume the returned destination checkpoint with a fresh `run --profile NAME`
grant. That explicit run authorizes disclosure of its complete history to the
selected provider; historical permissions are never restored. API qualification
still applies: portable conversion does not qualify an endpoint, and the public
coding command supports qualified Chat Completions and Responses. Compaction is separate from
this loss-aware format conversion.

## Compact a checkpoint

```bash
lscli compact --profile example --checkpoint SHA256 --task TASK --session SESSION --disclose-history --profiles /path/to/profiles.json --workspace /path/to/project --state-root /path/to/private/state --runtime-root /path/to/runtimes --keep-messages 8 --token-limit 32768
```

`--disclose-history` explicitly authorizes the selected checkpoint's history for
this profile's provider. The supervisor binds the exact history, normalized
profile, retained-tail count and token budget to a live task/session grant. The
checkpoint must be complete, current with the operation journal, and compatible
with the profile. Uncertain operations block compaction before provider dispatch.
History and summary text supply no file access, tool permissions or approvals.
The command is tool-free and requires no process sandbox; it uses the protected
runtime and qualified streaming Chat Completions or Responses transport. Credentials arrive
in the isolated worker over the inherited owner socket, not command arguments.

The default deadline is 120 seconds (`--timeout`, at most 3600); keep 0–256 tail
messages and set a total token limit of 1–1,000,000. The SDK can retain additional
messages to avoid splitting tool pairs. The supervisor verifies the returned
summary as user context, original leading system parts, an unchanged sufficient
native tail, bounded reported usage, exact request identity, acknowledgement and
successful worker exit. It rejects unsupported result shapes rather than relaxing
these checks; SDK pinned-message rearrangements are not qualified here.

Success returns schema-1 JSON with `source_checkpoint`, new `checkpoint`, profile
digest and usage (`requests`, `tool_calls`, `input_tokens`, `output_tokens`). The
new checkpoint and matching private compaction receipt are durable; the original
checkpoint remains unchanged. Resume the new digest explicitly with `run --resume`
and a fresh run grant. Failed, cancelled or timed-out workers do not promote their
results. Interrupted checkpoint/receipt writes or final output delivery can leave
new evidence without a success response: inspect it before retrying, and never
infer that a provider request was not billed from a missing acknowledgement.

Exit codes are 0 for accepted compaction, 2 for validation/operation failure,
3 for protected-runtime bootstrap failure, 124 for deadline expiry and 130 for
cancellation. Diagnostics are bounded and exclude history and credentials. Help
and argument validation do not initialize providers. Compaction is explicit;
no automatic schedule or background provider request is enabled.

## Responses coding qualification

A profile with `api: "responses"` and explicit `tools`/`streaming` capabilities
can use `lscli run`. The shared transport sends only the selected `/responses`
POST endpoint and the framework runtime user agent. Qualification covers streamed
function calls and outputs, native-history continuation, and bounded local binary
image serialization in deterministic fixtures; it makes no claim about every
Responses-compatible endpoint or live visual understanding.

The pinned SDK can expose tool-call parts from a failed or unfinished response.
LSCli therefore checks each Responses result before tool dispatch or final-output
acceptance: it must be a completed foreground response with the expected terminal
status. The raw event stream must have one creation event, increasing sequence
numbers and exactly one successful terminal event with no subsequent events.
Failed, incomplete, missing-terminal, contradictory, refused or background results are
rejected; streamed partial arguments alone never authorize a tool. SDK-native
response/item/call identifiers remain in compatible history. Changing the profile
requires an explicit portable branch, and prior evidence remains immutable.

Responses server-side background jobs, continuation polling, hosted tools, remote
media fetches and provider-managed conversation discovery are not qualified. The
existing transport rejects alternate methods/endpoints, and no fallback request
is enabled. Compaction supports the qualified Chat Completions and Responses
interfaces. Installed Responses fault injection covers the same three process
[recovery windows](LSCLI_QUALIFICATION.md#installed-controller-crash-qualification):
after the saved tool receipt, before that
receipt, and before a durable process outcome. Native `call_id` values match
exactly once across recovered function outputs; the settled case continues with
no additional operation, while missing receipts and uncertain outcomes refuse
continuation. These checks use deterministic loopback responses and do not
establish power-loss durability or every compatible endpoint.


The Responses stream guard wraps the pinned SDK's private `_response` event source
and preserves its `.source` close path. SDK upgrades must requalify that adapter
contract, malformed terminal ordering, tool dispatch, images and native-history
continuation before enabling the interface in a new runtime.

Responses compaction is also checked through the installed protected command:
a valid summary creates a new checkpoint and receipt, preserving the original.
Failed, incomplete, missing-terminal and contradictory streams each make one
request and create no checkpoint. Cancellation and deadlines stop acceptance;
no-op histories and uncertain journals refuse before a provider request. Every
captured request carries the framework runtime user agent. These deterministic
checks qualify the selected protocol path, not every provider endpoint.

## Tool-free completion command

```bash
localsetup llm complete --profile example --request request.json --profiles /path/to/profiles.json --runtime-root /path/to/runtimes
localsetup llm complete --profile example --request - < request.json
```

The command accepts the [version-one request](LSCLI_RUNTIME.md#direct-completion-contract-foundation)
from a regular non-symlink file
or stdin. Supplying it authorizes disclosure of that request to the selected
profile; no workspace files, context, sessions or tools are loaded. File input is
limited to 1 MiB and stdin waits within the overall timeout. Defaults use the
existing LSCli profile/runtime locations. `--timeout` caps execution at 120 seconds
by default and accepts positive values up to 3600; the request deadline can shorten
worker execution further. Help and argument validation initialize no provider.

The bootstrap selects the protected runtime and passes only the explicit selected
credential through a fixed internal environment key. A profile digest must match
after protected startup; changes cannot redirect that credential to a different
profile. Runtime selection is rechecked before worker dispatch.

Stdout contains one versioned result envelope; diagnostics never contain request
contents, credentials or raw provider errors. The [request/result contract](LSCLI_RUNTIME.md#direct-completion-contract-foundation)
lists every result status and exit code. SIGINT/SIGTERM produce cancellation (130); parent-enforced request or overall
timeouts produce deadline (124). A worker failure with possible dispatch returns
uncertain (10), and never triggers a retry. Missing credentials/runtime return
unavailable (3). Output writing is bounded to one second; a closed or blocked
output descriptor returns transport_failed (9), and a partially written envelope
is not a successful delivery. No configuration or sessions are created.

## Optional completion parameters and QC callers

Profiles may include explicit `organization` and `project` strings (at most 256
printable ASCII characters without whitespace). They are placed in final
`OpenAI-Organization` and `OpenAI-Project` headers; ambient values remain ignored.
Empty values are omitted from canonical profile serialization, preserving existing
profile identities. Nonempty additions change identity like other profile changes.

Requests may supply `schema_name` (1–64 ASCII letters, digits, `_` or `-`; default
`completion`) and `temperature` (finite 0–2). Temperature requires the profile's
`temperature` capability and is omitted when not requested. Explicit capabilities
record endpoint/model qualification; they do not prove every provider supports
every combination. Native fixtures verify the fields on both API formats.

Optional `session_id` accepts exactly 32 lowercase hexadecimal characters and
sets `X-Session-Id` at final send. Use one opaque random identity for requests
belonging to the same run, and different identities for independent runs. It is
transport metadata, not conversation history or authorization. Omission sends
no session header. QC manages this identity for each client run, including its
deadline-limited documentation calls, without changing gateway limits.

QC's existing string-returning client delegates to the protected worker while
retaining its default review schema, schema name and prompt redaction. It never
installs a runtime or retries a possibly delivered request. See the
[QC configuration and compatibility guide](https://github.com/CruxExperts/localsetup/blob/main/.ai/qc/README.md#protected-completion-compatibility)
for runtime selection, optional parameter declarations, retained legacy settings
and sanitized failure behavior.

Completion qualification also covers oversized JSON numeric parameters returning
`invalid_request` without a traceback or provider call. Responses output must
contain completed assistant messages; a completed response with an incomplete
message returns `incomplete`, and a non-assistant message returns `malformed`.
Deterministic native adapter fixtures verify ordered text across multiple parts
and messages after reasoning output, and verify that `validate_only` omits native
schema parameters while still rejecting output that fails local validation.

## Runtime and qualification section links

Earlier links to sections of this guide remain available below. Each link opens
the full canonical contract or historical candidate record.

| Earlier section anchor | Canonical content |
| --- | --- |
| <a id="candidate-verification"></a>Candidate verification | [Candidate verification](LSCLI_QUALIFICATION.md#candidate-verification) |
| <a id="installed-static-doctor-qualification"></a>Installed static-doctor qualification | [Installed static-doctor qualification](LSCLI_QUALIFICATION.md#installed-static-doctor-qualification) |
| <a id="runtime-lease-foundation"></a>Runtime lease foundation | [Runtime lease foundation](LSCLI_RUNTIME.md#runtime-lease-foundation) |
| <a id="isolated-sdk-import-qualification"></a>Isolated SDK import qualification | [Isolated SDK import qualification](LSCLI_RUNTIME.md#isolated-sdk-import-qualification) |
| <a id="sdk-model-adapter"></a>SDK model adapter | [SDK model adapter](LSCLI_RUNTIME.md#sdk-model-adapter) |
| <a id="bounded-worker-supervision"></a>Bounded worker supervision | [Bounded worker supervision](LSCLI_RUNTIME.md#bounded-worker-supervision) |
| <a id="task-bound-file-broker"></a>Task-bound file broker | [Task-bound file broker](LSCLI_RUNTIME.md#task-bound-file-broker) |
| <a id="process-sandbox-invocation-foundation"></a>Process sandbox invocation foundation | [Process sandbox invocation foundation](LSCLI_RUNTIME.md#process-sandbox-invocation-foundation) |
| <a id="bounded-sandbox-process-capture"></a>Bounded sandbox process capture | [Bounded sandbox process capture](LSCLI_RUNTIME.md#bounded-sandbox-process-capture) |
| <a id="authorized-process-snapshots"></a>Authorized process snapshots | [Authorized process snapshots](LSCLI_RUNTIME.md#authorized-process-snapshots) |
| <a id="durable-operation-evidence-and-reconciliation"></a>Durable operation evidence and reconciliation | [Durable operation evidence and reconciliation](LSCLI_RUNTIME.md#durable-operation-evidence-and-reconciliation) |
| <a id="recorded-file-replacement-and-read-only-reconciliation"></a>Recorded file replacement and read-only reconciliation | [Recorded file replacement and read-only reconciliation](LSCLI_RUNTIME.md#recorded-file-replacement-and-read-only-reconciliation) |
| <a id="exclusive-session-ownership-and-recovery-dispatch"></a>Exclusive session ownership and recovery dispatch | [Exclusive session ownership and recovery dispatch](LSCLI_RUNTIME.md#exclusive-session-ownership-and-recovery-dispatch) |
| <a id="sdk-iteration-and-continuable-snapshots"></a>SDK iteration and continuable snapshots | [SDK iteration and continuable snapshots](LSCLI_RUNTIME.md#sdk-iteration-and-continuable-snapshots) |
| <a id="durable-checkpoint-evidence-and-journal-joins"></a>Durable checkpoint evidence and journal joins | [Durable checkpoint evidence and journal joins](LSCLI_RUNTIME.md#durable-checkpoint-evidence-and-journal-joins) |
| <a id="inherited-worker-acknowledgement-channel"></a>Inherited worker acknowledgement channel | [Inherited worker acknowledgement channel](LSCLI_RUNTIME.md#inherited-worker-acknowledgement-channel) |
| <a id="acknowledged-harness-snapshot-adapter"></a>Acknowledged Harness snapshot adapter | [Acknowledged Harness snapshot adapter](LSCLI_RUNTIME.md#acknowledged-harness-snapshot-adapter) |
| <a id="sdk-file-tools-and-durable-tool-call-identity"></a>SDK file tools and durable tool-call identity | [SDK file tools and durable tool-call identity](LSCLI_RUNTIME.md#sdk-file-tools-and-durable-tool-call-identity) |
| <a id="sdk-process-recipes-and-isolated-test-commands"></a>SDK process recipes and isolated test commands | [SDK process recipes and isolated test commands](LSCLI_RUNTIME.md#sdk-process-recipes-and-isolated-test-commands) |
| <a id="delegated-resource-group-lifecycle"></a>Delegated resource-group lifecycle | [Delegated resource-group lifecycle](LSCLI_RUNTIME.md#delegated-resource-group-lifecycle) |
| <a id="resource-membership-before-sandbox-dispatch"></a>Resource membership before sandbox dispatch | [Resource membership before sandbox dispatch](LSCLI_RUNTIME.md#resource-membership-before-sandbox-dispatch) |
| <a id="bounded-disposable-command-storage"></a>Bounded disposable command storage | [Bounded disposable command storage](LSCLI_RUNTIME.md#bounded-disposable-command-storage) |
| <a id="provider-free-tool-preflight"></a>Provider-free tool preflight | [Provider-free tool preflight](LSCLI_RUNTIME.md#provider-free-tool-preflight) |
| <a id="broker-service-in-the-worker-supervisor"></a>Broker service in the worker supervisor | [Broker service in the worker supervisor](LSCLI_RUNTIME.md#broker-service-in-the-worker-supervisor) |
| <a id="installed-coding-worker-exchange"></a>Installed coding worker exchange | [Installed coding worker exchange](LSCLI_RUNTIME.md#installed-coding-worker-exchange) |
| <a id="supervisor-owned-coding-controller"></a>Supervisor-owned coding controller | [Supervisor-owned coding controller](LSCLI_RUNTIME.md#supervisor-owned-coding-controller) |
| <a id="durable-tool-results-for-local-recovery"></a>Durable tool results for local recovery | [Durable tool results for local recovery](LSCLI_RUNTIME.md#durable-tool-results-for-local-recovery) |
| <a id="native-history-reconstruction-boundary"></a>Native history reconstruction boundary | [Native history reconstruction boundary](LSCLI_RUNTIME.md#native-history-reconstruction-boundary) |
| <a id="supervisor-recovery-acceptance"></a>Supervisor recovery acceptance | [Supervisor recovery acceptance](LSCLI_RUNTIME.md#supervisor-recovery-acceptance) |
| <a id="installed-controller-crash-qualification"></a>Installed controller crash qualification | [Installed controller crash qualification](LSCLI_QUALIFICATION.md#installed-controller-crash-qualification) |
| <a id="compaction-worker-foundation"></a>Compaction worker foundation | [Compaction worker foundation](LSCLI_RUNTIME.md#compaction-worker-foundation) |
| <a id="direct-completion-contract-foundation"></a>Direct completion contract foundation | [Direct completion contract foundation](LSCLI_RUNTIME.md#direct-completion-contract-foundation) |
| <a id="direct-model-worker-adapter-foundation"></a>Direct-model worker adapter foundation | [Direct-model worker adapter foundation](LSCLI_RUNTIME.md#direct-model-worker-adapter-foundation) |
| <a id="protected-completion-worker"></a>Protected completion worker | [Protected completion worker](LSCLI_RUNTIME.md#protected-completion-worker) |
| <a id="read-only-runtime-lease-primitive"></a>Read-only runtime lease primitive | [Read-only runtime lease primitive](LSCLI_RUNTIME.md#read-only-runtime-lease-primitive) |
| <a id="release-bound-registration-dispatch-primitive"></a>Release-bound registration dispatch primitive | [Release-bound registration dispatch primitive](LSCLI_RUNTIME.md#release-bound-registration-dispatch-primitive) |
| <a id="fresh-registration-plan-primitive"></a>Fresh registration plan primitive | [Fresh registration plan primitive](LSCLI_RUNTIME.md#fresh-registration-plan-primitive) |
| <a id="receipt-backed-fresh-registration-owner"></a>Receipt-backed fresh registration owner | [Receipt-backed fresh registration owner](LSCLI_RUNTIME.md#receipt-backed-fresh-registration-owner) |
| <a id="owned-refresh-and-explicit-reconciliation-primitives"></a>Owned refresh and explicit reconciliation primitives | [Owned refresh and explicit reconciliation primitives](LSCLI_RUNTIME.md#owned-refresh-and-explicit-reconciliation-primitives) |
| <a id="installed-setup-and-registration-qualification"></a>Installed setup and registration qualification | [Installed setup and registration qualification](LSCLI_QUALIFICATION.md#installed-setup-and-registration-qualification) |
| <a id="installed-target-and-session-continuity-qualification"></a>Installed target and session continuity qualification | [Installed target and session continuity qualification](LSCLI_QUALIFICATION.md#installed-target-and-session-continuity-qualification) |
| <a id="supervised-registration-binding"></a>Supervised registration binding | [Supervised registration binding](LSCLI_RUNTIME.md#supervised-registration-binding) |
| <a id="installed-heartbeat-planning-qualification"></a>Installed heartbeat planning qualification | [Installed heartbeat planning qualification](LSCLI_QUALIFICATION.md#installed-heartbeat-planning-qualification) |
| <a id="installed-reserved-heartbeat-qualification"></a>Installed reserved heartbeat qualification | [Installed reserved heartbeat qualification](LSCLI_QUALIFICATION.md#installed-reserved-heartbeat-qualification) |
| <a id="installed-continuation-authorization-and-result-recovery"></a>Installed continuation authorization and result recovery | [Installed continuation authorization and result recovery](LSCLI_QUALIFICATION.md#installed-continuation-authorization-and-result-recovery) |
