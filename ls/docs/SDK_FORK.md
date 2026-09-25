---
status: ACTIVE
version: 4.44
owner_skill: ls-architecture
---

# LSCli SDK source ownership

LocalSetup owns one canonical SDK source tree at `vendor/lscli`. It retains the
upstream `pydantic_ai`, `pydantic_graph`, and `pydantic_ai_harness` namespaces and
MIT license texts. These are private implementation dependencies for LSCli.
Never add this tree to the supervisor's import path. Only the isolated SDK worker
may import it after the installed runtime's integrity and authority gates pass.

Ordinary wheel builds verify the source and generate private SDK data under
`ls/_sdk_payload`. The build does not install upstream namespaces at the wheel
root or depend on installed upstream SDK distributions. Editable framework development remains available, but supplies no private SDK
payload and cannot qualify LSCli execution.
Protected runtime bootstrap, coding and tool-free completion are implemented
through the [LSCli public commands](LSCLI.md) and their
[runtime authority contracts](LSCLI_RUNTIME.md). Installed checks retain the
bounded [candidate qualification](LSCLI_QUALIFICATION.md) scope; source or payload
integrity alone does not qualify an arbitrary wheel environment or public release.
Optional upstream modules remain in the source for attribution and fork
maintenance. Their presence does not qualify providers, MCP, browser tools,
subagents, or other integrations.

## Provenance and changes

`vendor/lscli/manifest.json` identifies each distribution, version, immutable
repository commit, source archive digest, license, namespace, and every retained
file. `upstream_sha256` records original runtime bytes; `sha256` records the local
bytes. Every changed runtime file has a retained patch. The initial patch makes
Slim's version lookup static so it does not require the original distribution's
installed metadata. It does not change the framework version or request identity.

The selected baselines are Pydantic AI Slim and Graph **2.38.0** and Pydantic AI
Harness **0.28.1**. Downloaded PyPI sdist digests and publish-attestation subjects
were cross-checked with immutable GitHub source: all 564 retained runtime files
matched before patching. The recorded verification uses HTTPS registry
attestations and source-byte comparison; it is not independent Sigstore
certificate-chain verification. Upstream tests, build tooling, and unrelated
repository documentation are not part of the runtime payload.

The dated dependency inventory covers the Slim OpenAI extra, base Graph/Harness,
and upstream plus LocalSetup build tools resolved for Python 3.12 on Linux.
OSV, GitHub Advisory Database, and deps.dev/PyPI returned no advisories for those
40 exact versions on 2026-09-05; none was yanked on PyPI. This inventory is evidence
for source adoption, not a supported-platform lock or a guarantee of safety.
Managed runtime locks, combined framework dependency compatibility, installed
imports, and ordinary wheel compatibility require separate executable evidence.

The documentation inventory identifies byte-identical retained manuals as
upstream documents with immutable source URLs. Their examples and links retain
the original repository context and are not LocalSetup operational guidance.
LocalSetup lifecycle, capability-count, and local-link rules apply to owned
documents; payload integrity and source ownership still apply to every retained
manual. New or modified files cannot acquire upstream status through a directory
name alone.

## Verify and refresh

Run the standard-library verifier without installing or importing the SDK:

```bash
uv run --locked python ls/tools/validate_sdk_payload.py --root vendor/lscli
```

It rejects missing, extra, modified, symlinked, special, and unsafe-path files,
invalid component ownership, missing licenses, and unrecorded local changes.
The manifest is an input to the release's trust boundary, not a signature. An
attacker who can replace both files and manifest can replace this evidence.
Run verification on a stable tree under the owning build/runtime lock; the
installed runtime must protect and authenticate its payload separately.

For a refresh:

1. Resolve the explicitly selected extras and complete runtime/build dependency
   closure. Audit exact versions against independent advisory sources before
   adoption; record platform markers, artifact hashes, and unresolved limitations.
2. Download exact source archives, verify registry digests and provenance, and
   compare retained runtime bytes to immutable upstream source. Never execute an
   archive to discover metadata or use unreviewed extraction paths or links.
3. Retain all namespace runtime resources and original MIT licenses. Reapply
   reviewed patches, recording original and resulting hashes and patch rationale.
4. Review the actual upstream and patch diff. Run payload, import-origin,
   packaging, installed-artifact, provider, and recovery checks for the affected
   behavior before accepting a new runtime. Refresh its SBOM and locked external
   dependencies through their owning build tools when those surfaces are present.
5. Commit accepted source changes and regenerate documentation through its owners
   in the required separate receipt. Keep machine evidence and private audit
   records outside public source and artifacts. Retain compatible prior release
   artifacts for recovery; never rewrite stored sessions during a source refresh.

## Wheel build boundary

The setuptools `BuildSDK` command owns the mapping from canonical source to wheel
data. It validates before copying, includes every listed runtime resource,
license, patch, and manifest, and verifies the resulting tree. Extra files,
symlinks, or a different retained manifest in a reused build directory stop the
build; use a fresh build output directory after changing the SDK source.

The build command loads its sibling verifier directly because setuptools loads
custom commands before the source package is importable. The core package also
resolves its `main` compatibility export lazily. The build helper imports only
the standard library and setuptools. No framework
runtime dependencies, provider credentials, or SDK imports are needed to build
the payload. Build from the source archive with the pinned setuptools backend;
source distributions retain the canonical vendor input through `MANIFEST.in`.
The public release archive also retains the framework root `LICENSE`, so a
wheel built from that archive preserves the same license file and metadata.
The installed worker's import-origin and runtime-protection checks are separate
requirements; a successful wheel build does not authorize agent execution.

Build and inspect candidates using the repository's pinned backend:

```bash
uv sync --locked --all-groups
uv run --locked pytest ls/tests/test_sdk_build.py ls/tests/test_sdk_payload.py -q
uv build --wheel --no-sources --out-dir dist
uv build --sdist --no-sources --out-dir dist
uv build --wheel dist/<source-distribution>.tar.gz --out-dir dist/from-sdist
```

Replace the source-distribution placeholder with the exact artifact just built.
The development group includes the same pinned setuptools version as the build
backend so the build-command regressions run in the normal project environment.
Python 3.12/Linux candidate checks established exact payload bytes for a source
wheel, sdist, and sdist-built wheel. A dependency-free installation also verified
the payload from outside the checkout without importing the SDK. These checks
qualify the packaging boundary; runtime external dependencies, supported provider
interfaces, protected worker execution, and other environments remain separate
qualification gates.

## Vendored-component SBOM verification

The optional native sandbox ZIP has a separate [external SBOM producer and
verifier](LSCLI.md#optional-native-sandbox-artifact-delivery). SDK and framework
SBOM checks do not establish native ZIP or native build-provenance coverage.

Ordinary wheels contain an SDK-only CycloneDX 1.6 document at
`ls/sdk-sbom.cdx.json`, outside the exact private payload tree. The build derives
its three components from the verified manifest. Records retain upstream names
and versions, MIT licenses, immutable source references, the source archive
hash, a digest of the component's retained file inventory, and each local patch
hash. The package URL identifies the upstream baseline; vendored and patch
properties identify the local fork. This document does not claim to enumerate
external runtime dependencies.

The existing source/public-archive SBOM includes these same vendored records
alongside the framework lock's external components. Public artifact metadata
binds the SDK manifest digest when a payload is present. Verification checks
actual archived SDK files before comparing the full vendored records; changing
licenses, provenance, patches, or inventory metadata cannot pass merely because
a component name and version still match. Historical artifacts without an SDK
remain supported by the existing release verifier.

```bash
uv run --locked python ls/tools/validate_sdk_payload.py --artifact dist/<wheel>.whl
uv run --locked python ls/tools/validate_sdk_payload.py --artifact dist/<source-distribution>.tar.gz
```

Use the exact artifact filenames. The artifact check requires an SDK payload;
wheels also require their embedded SDK SBOM. It rejects ambiguous roots,
unexpected files, duplicate SDK paths, path traversal, links, payload changes,
and malformed or stale embedded SBOMs. It materializes only bounded regular SDK
files in a temporary directory and never imports their code. Limits are 16 MiB
per file, 64 MiB of SDK payload, and 10,000 SDK files. Artifact checksums
and trusted release provenance still provide the outer authenticity boundary.
Full runtime dependency and released-artifact acceptance remain separate gates.

## External dependency boundary

The framework's `pyproject.toml` declares the external requirements selected from
Slim's base and OpenAI extra, Graph's base, and Harness's base metadata. The
stronger shared `genai-prices` minimum is retained. The three vendored SDK
distributions are excluded: installing the framework wheel must not install
another copy of them. Optional upstream provider and integration extras are not
selected. The ordinary wheel uses dependency ranges; `uv.lock` records the exact
combined framework and SDK external resolution for repository workflows.

PGPy, an existing framework dependency, supplies only a source distribution at
version 0.6.0. Its verified static build metadata requires setuptools and wheel;
wheel also requires packaging. The project's uv build constraints pin this
build closure separately from runtime dependencies. These constraints govern uv
project operations; ordinary wheel installers do not inherit project uv settings.
A managed installation must use its release's locked runtime and build artifacts.
The protected installation path remains a separate delivery gate.

The combined resolution was checked against OSV, GitHub Advisory Database, and
deps.dev/PyPI on 2026-09-05. All 42 selected runtime/build versions returned no
reported advisories and were not yanked. Artifact hashes in the changed framework
lock records matched the registry. This includes the Emscripten-only
`httpx2-jsfetch` marker dependency for inventory completeness; it does not qualify
Emscripten execution. Dependency advisory results are dated evidence, not a
security guarantee or a claim of support for every environment represented by a
universal lock.

A Python 3.12/Linux candidate wheel was installed into a fresh environment from
outside the checkout with the audited version constraints and build constraints.
All 38 external runtime versions matched the candidate inventory, the installer
reported compatible installed dependencies, the private payload verified, and
`localsetup --version` returned the framework display name and version. No
original SDK distribution was installed and no SDK module was imported by this
check. This qualifies that exact constrained candidate; it does not establish
SDK execution or compatibility with every version allowed by the wheel's ranges.


## Managed dependency lock exports

`uv.lock` is the canonical external resolution. The `sdk-build` dependency group
pins the audited build tools; it is separate from the runtime graph and the
development group. The owning exporter produces `ls/config/sdk-runtime.lock`
and `ls/config/sdk-build.lock`, both shipped as wheel data. Runtime export retains
platform markers and artifact hashes, excludes development/build-only tools, and
omits the framework itself and all three vendored SDK distributions.

```bash
uv run --locked python ls/tools/generate_sdk_dependency_locks.py --repo-root .
uv run --locked python ls/tools/generate_sdk_dependency_locks.py --repo-root . --check
```

Both operations call `uv export --locked --offline`; they never update the
resolution or install dependencies. Check mode returns 1 for missing or different
output, 2 for export/validation errors, and 0 for exact agreement. Regenerate after
an accepted dependency change, review both exports, and commit them with that
source slice. Generation requires a stable source tree and a compatible uv CLI.
The ordinary wheel build only packages these files and does not run uv.

For an isolated candidate environment, install build dependencies with
`uv pip install --python <candidate-python> --require-hashes --only-binary :all: -r ls/config/sdk-build.lock`,
then install runtime dependencies with
`uv pip install --python <candidate-python> --require-hashes --no-build-isolation -r ls/config/sdk-runtime.lock`.
Use a new, task-owned environment and the exact files from the candidate artifact.
Preloading the hashed build closure and disabling build isolation prevents an
implicit download of unpinned PGPy build dependencies. The framework wheel is
installed separately after its artifact verification. This is a qualification
procedure, not the protected installer or an instruction to alter an existing
user environment. Python 3.12/Linux candidate installation passed with these
options; other marker branches remain separate qualification requirements.


### Build-time export consistency

The exporter also owns `ls/config/sdk-dependency-receipt.json`. It binds both
exported byte streams to the canonical project and lock inputs. Ordinary wheel
builds validate this receipt using only the standard library before copying the
SDK payload. Missing inputs, changed exports, or stale source bindings stop the
build and require regeneration. Reused build directories must also contain only
matching dependency files; the build rejects stale destinations and verifies
the bytes copied into wheel output. Source distributions retain `uv.lock` so the
same check applies when building a wheel from an sdist.

The binding ignores only the framework project version and its local editable
lock record's version, allowing canonical release version synchronization without
changing external dependency exports. Other project or lock changes invalidate
the receipt. The receipt is consistency evidence, not an independent signature:
coordinated replacement of source, locks, and receipt can replace that evidence.
Release provenance and protected installed-runtime checks remain separate gates.

The provider-free [LSCli bootstrap](LSCLI.md) reports installed payload integrity
and current execution readiness. It never imports the SDK while diagnosing a
missing or invalid payload.
