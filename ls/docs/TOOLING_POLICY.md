---
status: ACTIVE
version: 4.70
owner_skill: ls-script-docs-authoring
---

# Tooling policy

Purpose: define project-wide tooling language and dependency rules.

## Core rule

- Python is the primary and only language for framework tooling after installation.
- Shell is limited to bootstrap and tiny platform entrypoints:
  - install bootstrap (`install`)
  - minimal wrappers/delegation for host compatibility
  - environment orchestration outside framework runtime
- Native PowerShell wrappers are not active LocalSetup surfaces; use WSL2 plus Bash on Windows.

Python architecture: new and substantially refactored Python tooling follows ls/docs/PYTHON_ARCHITECTURE_STANDARD.md; keep entrypoints thin, package responsibilities explicit, and existing debt baseline-managed.

## Shared signed configuration lifecycle

`ls.core.live_configuration` owns complete signed configuration revisions.
`ConfigurationAuthority` requires an explicitly selected signer, exact recipient
set and current authority check. It reuses the OpenPGP envelope implementation;
it never generates a key or treats an installed key as permission to configure
another owner. Configuration signing and recipient decryption remain distinct
capabilities. The configuration authority is included in the recipient set.

`ConfigurationStore` keeps immutable encrypted revisions and publishes one
atomic `HEAD` for the complete set of owner values. Writers compare the expected
prior digest under the shared local lease. Each revision binds its store,
sequence, predecessor, cadence and optional extensions. Unknown required features
are rejected, while bounded optional extensions survive later writes. Exact
signed bytes remain unchanged. Payloads are limited to 256 KiB, 64 owners and
bounded JSON depth/count; the store stops at 1,024 retained objects rather than
deleting history implicitly.

An interrupted write retains its exact candidate and intent. `recover` verifies
that candidate against the current predecessor and finishes or observes its
publication without signing a replacement. If intent persistence failed, the
caller can supply the opaque candidate digest returned with the uncertain result.
Conflicting revisions are rejected. Readers can require both a minimum revision
and a verified ancestor, preventing a valid older revision or competing branch
from replacing their accepted state. Local private filesystem isolation protects
metadata against rollback; this is not distributed freshness or protection
against a malicious process with the same filesystem authority.

`ls.core.live_configuration_runtime.ConfigurationRuntime` stages validated owner
values in memory and uses one caller-driven `poll` cadence for writes and reloads.
The cadence is an integer from 1 through 3,600 seconds, defaulting to 60.
`flush` writes all staged owners together and reloads the signed revision after
publication. All registered validators must accept before the effective snapshot
changes. Invalid signatures, values, persistence failures and external signed
revision conflicts preserve the last accepted values and pending proposals.
An explicit `discard_staged` abandons proposals, but cannot discard an uncertain
write. Read access alone never authorizes flushing changes.

Owners consume independent values from the accepted snapshot; native effects,
grants, identities and confinement retain their existing authority checks. The
runtime provides a bounded 128-event in-memory provenance view for proposed,
persisted, applied and rejected changes. Signed revisions carry caller-supplied
opaque actor, machine and operation identifiers. Events omit configuration values
and secrets; identifiers are not independent proof of machine identity. Event
history does not survive a process restart unless an owning recorder persists it.

`localsetup config` exposes this owner through a controller-selected private
authority binding. The binding pins a current trust-store owner and revision,
recipient set, secret-provider reference, target store and registered owners.
It has a separate write permission and is never part of the editable settings.
Application-profile and container-runtime validators remain the source of truth
for their values. See [signed configuration commands](COMMAND_REFERENCE.md#signed-configuration)
for the binding shape and commands.

The editable adapter exports a new private directory containing all registered
owner files, a cadence file and a base-revision manifest. A flush holds anchored
input files through signing and publication, rejects changed or missing input,
and never rewrites the working files. Repeated unchanged flushes do not sign
another revision. A signed operation digest binds the exact working set and
values, allowing later edits through that same set; another writer's revision
produces a conflict instead of silently rebasing or overwriting it. Unknown
optional owner values and extensions are retained in the complete signed set.
Recovery uses the original candidate and validates registered owners before
publication. Status and error output omit configuration values and credentials.

These commands persist and reload the shared signed set; they do not replace
legacy files, enroll keys, start background polling, flush another process's
memory, or apply native terminal/container effects by themselves. The native
terminal renderer can be explicitly bound to a `terminal-runtime` owner and
consumes signed revisions through this same cadence. Its existing session and
native identity checks remain authoritative; live tuning cannot change protected
settings. Native uncertainty retains an exact pending receipt for explicit
read-only reconciliation. See [terminal runtime configuration](TMUX_TERMINAL_MODE.md#runtime-configuration).
Container reads can explicitly bind a `container-runtime` owner for bounded
resource tuning at operation boundaries, preserving legacy configuration and
in-flight read budgets. Scheduled indexing accepts dynamic tuning only when its
enrollment includes that pinned binding. Protected changes retain the runtime's
existing lifecycle and grant checks. See [container tuning](OPENPGP_RUNTIME.md#signed-live-resource-tuning).
Existing application-profile and container-runtime configuration stays readable
through its current owners.

## Python runtime target

- Minimum supported version: Python 3.12.
- Baseline rationale: LocalSetup uses the Python 3.12 standard library as the supported floor while staying forward-compatible with newer LTS runtimes.

## Dependency policy

- Keep third-party libraries minimal.
- Add dependency only when it materially reduces complexity, risk, or maintenance cost.
- Prefer mature libraries with:
  - active maintenance
  - broad adoption
  - clear license
  - recent release activity
- Pin human-maintained dependency intent in `pyproject.toml`, and document why each dependency exists.
- Keep `uv.lock` as the committed dependency lock. Automation must run `uv lock --check` and frozen or locked `uv sync` / `uv run` commands.
- Dependency-update PRs must update `pyproject.toml` and `uv.lock` together when dependency intent changes.
- Keep framework dependency installs isolated. The default dependency mode is `prompt-only`; explicit `--dependency-mode uv-sync` or root `--sync-env` creates or updates the source checkout `.venv` with uv and does not mutate system Python.
- Treat `~/.local/share/localsetup/venv` and target `.localsetup/venv` as legacy LocalSetup runtime state. Diagnostics may warn about corrupt legacy environments in `prompt-only` mode, but current dependency setup must not execute them or depend on them.
- Explicit sync paths may quarantine corrupt LocalSetup-owned environments by rename before uv rebuilds. Eligible paths are source checkout `.venv`, legacy global `~/.local/share/localsetup/venv`, and legacy target-local `.localsetup/venv`. A target project's own `.venv` is application-owned and must never be modified by LocalSetup repair.
- Use `pipx` for app-style CLI tools and future wheel-based LocalSetup command installs. Do not use `pipx` as the mechanism for libraries imported by framework Python modules; those belong in the uv project environment.
- Treat old `managed-venv` and `user-pip` dependency-mode values as migration aliases only. `managed-venv` maps to `uv-sync`; `user-pip` maps to `prompt-only`. New configuration should use `uv-sync` or `prompt-only`.

For CLI-based skills that depend on external binaries (for example, Scrapling), see also the CLI skills environment policy in `CLI_SKILLS_ENV.md` for user-first `pipx` installs, PATH handling, and health checks.

## Skill risk metadata and policy modes

Skill frontmatter may declare:

- `risk: low|medium|high`
- `permissions: [...]`

Missing risk metadata is treated as `low` with no permissions. `plan` and `install` include policy warnings when selected skills declare medium or high risk, or when permissions are listed. `--policy-mode strict` and `--policy-mode ci` block high-risk selected skills during non-interactive installs. `permissive` and `standard` report warnings without blocking.

Use strict policy in CI or unattended provisioning when high-risk skills must be explicitly approved before they can alter a target:

```bash
localsetup install --tools codex --policy-mode strict --yes
```

The policy gate is metadata-based. It complements, but does not replace, skill vetting, schema validation, and release artifact verification.

## Approved libraries (mandatory use)

These libraries are pre-approved, listed in `pyproject.toml`, locked in `uv.lock`, and available after `uv sync` or an install run with `--sync-env`. When writing new tools or refactoring existing ones, **use these libraries** instead of reimplementing their functionality. Reinventing them (custom HTTP clients, bespoke YAML parsers, ad-hoc frontmatter splits) is explicitly prohibited when one of these covers the need.

| Library | Import name | Project dependency | Use for |
|---------|-------------|-------------|---------|
| PyYAML | `yaml` | `PyYAML>=6.0` | All YAML parsing and serialization. Never use `json` as a workaround or parse YAML by hand. |
| requests | `requests` | `requests>=2.28` | All outbound HTTP. Use `requests.Session` for multi-request tools. Never use `urllib.request` for new code. |
| python-frontmatter | `frontmatter` | `python-frontmatter>=1.1` | Parse YAML front matter from skill and PRD markdown files. Never split frontmatter by hand. |
| cryptography | `cryptography` | `cryptography>=50.0.0` | Framework cryptographic primitives (AES-GCM, HKDF, PBKDF2, secure random). Use for encryption/decryption and key derivation. |
| PGPy | `pgpy` | `PGPy>=0.6.0` | Pure-Python OpenPGP encryption and decryption in framework tooling. |
| jsonschema | `jsonschema` | `jsonschema>=4.0` | Draft 2020-12 validation for LocalSetup manifests and Agent Q payloads. |

The managed environment resolves `cryptography==50.0.2` in `uv.lock`; the
distribution requirement remains `cryptography>=50.0.0`. The pyca
[50.0.2 changelog](https://raw.githubusercontent.com/pyca/cryptography/50.0.2/CHANGELOG.rst)
notes that its wheels bundle OpenSSL 4.0.3. Dependency update validation
includes the mail protocol crypto tests and
`ls/tools/agentq_transport_client/tests/test_agentq_pipeline.py`, which lives
outside the central `ls/tests/` suite. A passing central suite alone does not
cover those consumers.

**Shared dependency helper:** Import `lib.deps` at the top of every tool and call `require_deps()` before using any approved library. This gives users an actionable error message instead of a bare `ImportError` if the library is missing.

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
from deps import require_deps
require_deps(["requests", "frontmatter"])  # list only the ones this tool uses
```

The `require_deps()` call always does a live `importlib` check. It never reads the `.deps-missing` sentinel file written by the install script, so a stale sentinel from an older install cannot block tools on a system where the packages are present.

**For AI agents:** When generating or refactoring framework Python tooling, check whether the task involves HTTP requests, YAML parsing, or frontmatter parsing. If yes, use the approved library from the table above. Do not generate custom implementations of these.

## Lint and quality

- Python tooling must be lint-clean before merge. Ruff is the preferred configured lint, format, and import-sort tool, but it is not a hard installed requirement unless the project declares Ruff in `pyproject.toml` or equivalent tool configuration. Single source of truth for commands and expectations:
  - **Import sort:** In projects that declare Ruff, run `ruff check --select I --fix`.
  - **Format:** In projects that declare Ruff, run `ruff format` for style; no trailing whitespace, consistent quotes.
  - **Linter:** In projects that declare Ruff, run `ruff check` from repo root or script directory. Fix all reported issues or document an explicit exception. If a project declares a different linter or formatter, use that configured equivalent.
  - **Types:** Use type hints for public functions and module boundaries. Use `pyright` or `mypy` in strict or standard mode if the project enables it; resolve type errors or add targeted ignores with a short comment.
  - **Best practice:** Prefer explicit error handling and small functions; avoid broad `except` and untyped `**kwargs` in public APIs. See [INPUT_HARDENING_STANDARD.md](INPUT_HARDENING_STANDARD.md) for input handling; this section covers static checks and style only.
- Audit and CI scripts may read this section to determine which commands to run for tooling/lint.

## Markdown output (reports and tool output)

All framework tooling that produces reports or structured output intended for human or agent consumption **must** emit markdown that is **GitHub Flavored Markdown (GFM) compatible**. This ensures output renders correctly in GitHub, in editor previews, and in any GFM-capable viewer. Apply these rules globally to every script or tool that writes a report, log summary, or formatted output to a file or stdout.

**Required:**

- **Sectioning:** Use heading levels to separate parts of the report. Use `#` for the document title, `##` for major sections, `###` for subsections, `####` for sub-subsections. Do not skip levels (e.g. do not go from `##` to `####`).
- **Code blocks:** Use fenced code blocks for any program or terminal output, log snippets, or code. Prefer `~~~text` / `~~~` (or ` ```text ` / ` ``` `) so that output containing backticks does not break the fence. Add a language hint when useful (e.g. `~~~text` for terminal output, `~~~python` for code). Do not rely on indented-only code blocks; use explicit fences.
- **Emphasis:** Use **bold** for labels and important terms (e.g. **stdout**, **Errors**, **Summary**). Use *italic* for secondary emphasis or "(no output)"-style notes where appropriate.
- **Lists:** Use `-` or `*` for unordered lists and `1.` for ordered lists so they render as proper list structure.
- **Tables:** Use GFM table syntax (`| col | col |` with a header separator row) when presenting tabular data so it renders in a readable table.

**Recommended:**

- Use horizontal rules (`---`) sparingly to separate major report sections if it improves scanability.
- Use inline `code` for file paths, command names, and literal values so they stand out.
- Keep line length and paragraph length reasonable so that diff and preview UIs remain readable.

**Avoid:**

- Raw triple backticks inside content that is inside a fenced block (use a different fence, e.g. `~~~`, for the outer block so inner backticks do not close it).
- Non-GFM or non-CommonMark markdown that GitHub does not render (e.g. custom HTML for layout; prefer standard markdown).
- Unstructured walls of text; break content into sections and use the formatting above so importance and hierarchy are visually clear.

Scripts and skills that generate reports (e.g. [framework audit](WORKFLOW_REGISTRY.md), PR review, validation summaries) must follow this section. New tooling must adopt it by default.

## External input rule

- All Python tooling that consumes external input must follow:
  - hostile-by-default handling
  - input sanitization
  - schema and bounds validation
  - actionable STDERR error output
  - no silent failure
- See [INPUT_HARDENING_STANDARD.md](INPUT_HARDENING_STANDARD.md) for mandatory controls.

## Migration direction

- New tooling and significant refactors should be implemented in Python.
- Existing shell/PowerShell tooling may remain temporarily where needed for bootstrap and compatibility, but should not expand in scope.
