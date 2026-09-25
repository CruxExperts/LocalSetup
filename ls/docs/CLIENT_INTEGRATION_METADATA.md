---
status: ACTIVE
version: 4.44
owner_skill: ls-framework-compliance
---

# Client integration metadata

`ls/config/clients.yaml` owns client families and variants.
`ls/config/platforms.yaml` is its generated installation projection; update the
owner and run `localsetup client-registry generate`, then
`localsetup client-registry check` to validate the projection.
CLI, IDE, and application variants remain separate records even when they share
skill directories. The existing `verification.classification` selects a check
method; it is not evidence that a host check succeeded.

Discovery inventory and installation targets are separate. A skill surface with
`resolution: aggregate` describes multiple discovery roots; `hierarchy`
describes ancestor discovery, while `first-existing` and `first-nonempty`
describe fallback selection. None of these values establishes duplicate-name
precedence without separate evidence.

Optional `compatibility.repo_write_paths` and `global_write_paths` select
preferred fresh-write targets from the corresponding skill discovery paths.
Each must be a nonempty, duplicate-free subset of that scope's declared paths.
When omitted, the projection retains all declared paths for compatibility.
For example, a profile may inventory both common and native roots but select:

```yaml
repo_write_paths: [.agents/skills]
global_write_paths: [~/.agents/skills]
```

These fields belong inside that profile's `compatibility` object. Generated
platform write paths and fresh rollback targets use the selected subset;
the registry retains the full discovery inventory. They do not define a
migration or authorize removing previously recorded adapters. Before changing
an existing profile's targets, qualify its recorded update, repair and detach
routes and preserve custom content. The metadata mechanism alone does not
qualify host discovery, collision handling or a historical-path migration.

When an existing repository receipt records an adapter outside a newly declared
`repo_write_paths` subset, omitted-selector plan/update uses the validated
recorded-path route. It retains scope, clients, paths and package ownership
rather than applying fresh-target inference. Repository-only automatic repair
returns a preservation blocker for that historical layout; reviewed
recorded-path recovery is required. Personal and combined scopes keep their
recorded ownership routes. A fresh installation already using the preferred
paths remains eligible for ordinary repair. Detach removes only recorded
managed entries and preserves custom neighbors. For a profile declaring preferred
repository paths, legacy selectors or path hints without modern ownership
records require manual recovery before repository update or repair. Explicit-empty
modern records do not fall back to legacy fields. Path-only hints are matched
from the recorded target root, including descendants of a declared discovery
root; a generic `skills` entry does not match another client's nested
`.cursor/skills` directory. Absolute hints outside the target and traversal
hints do not establish a client identity. Changing metadata alone never
authorizes deleting an old native adapter.

An optional `integration` object records lifecycle, installation guidance, and
qualification results separately. Existing records without it retain their
current behavior until their owning profile is reverified. A missing object
must not be interpreted as successful qualification.

```yaml
integration:
  lifecycle: active
  installation:
    method: manual
    instructions: Follow the vendor guide cited in research.sources.
  qualification:
    catalog: bounded
    filesystem: not-run
    host: not-run
    evidence:
      - kind: documentation
        reference: ls/docs/CLIENT_INTEGRATION_METADATA.md
  limitations:
    - Host authentication and functional execution have not been qualified.
```

- `lifecycle` is `active`, `retained-only`, or `unsupported`. A retained-only or
  unsupported record cannot carry `compatibility` and therefore cannot project
  a fresh-install adapter. Historical receipts remain separate ownership
  evidence; this declaration does not authorize deleting them or their content.
- `installation.method` is `managed-release`, `vendor-installer`,
  `package-manager`, `editor-extension`, `application`, `manual`, or
  `unavailable`. `instructions` describes the installation route. Neither field
  is an executable recipe or authorization to install, authenticate, or update
  a third-party application.
- `qualification.catalog` is `implemented` or `bounded`. Filesystem qualification
  is `verified`, `not-run`, or `not-applicable`; host qualification additionally
  permits `blocked`. Catalog support and filesystem fixtures do not establish
  successful host installation or functional host behavior.
- Evidence entries have a `filesystem`, `host`, or `documentation` kind and a
  repository-relative reference or HTTPS URL. Each `verified` surface requires
  matching evidence. Bounded catalog support, blocked host qualification, and
  non-active lifecycle require explicit limitations.

Validation checks declarations and reference syntax, including rejection of
private state paths and URL user information. It does not execute evidence,
prove a linked report's claim, or establish that an arbitrary HTTPS destination
is public. Review exact evidence and its tested version/environment before
marking a surface verified. Machine-specific records stay private; public
metadata references only intentionally publishable tests and documentation.
Preserve upstream attribution and immutable historical audit evidence.

## GitHub Copilot profiles

The `github-copilot` family has independent `github-copilot-cli` and
`github-copilot-vscode` selectors. Both project repository skills to
`.agents/skills` and personal skills to `~/.agents/skills`. This is LocalSetup's
choice among documented native discovery paths, not a claim of upstream path
priority. Selecting both deduplicates physical writes while retaining separate
owners. Symlink and portable filesystem fixtures cover installation, partial
repository/personal detach, and custom-content preservation.

```bash
localsetup plan --target-directory PROJECT --tools github-copilot-cli github-copilot-vscode --skill-scope both --skills ls-context
localsetup install --target-directory PROJECT --tools github-copilot-cli github-copilot-vscode --skill-scope both --skills ls-context --apply
localsetup verify --target-directory PROJECT
```

These commands install LocalSetup skill adapters. They do not install or
activate Copilot, authenticate an account, or change native settings, permissions,
credentials, or sessions. Catalog and filesystem support are implemented; host
qualification remains `not-run`. Exact minimum versions and duplicate skill-root
precedence are unverified. Existing context may be discovered through `AGENTS.md`,
but policy insertion and personal instruction/configuration management are not
qualified by these profiles.

| Profile | Discovery and installation | Context and qualification boundary |
|---|---|---|
| GitHub Copilot CLI | Executable candidate `copilot`; select and verify a pinned official release artifact before installation. | Applicable instructions combine without a general precedence guarantee. `COPILOT_HOME` redirects personal instructions and configuration; its effect on skill roots is not assumed. |
| GitHub Copilot in VS Code | Executable candidate `code`, plus the intended editor profile and official Copilot activation. Command presence alone does not establish availability. | Personal, repository, and organization instructions have separate conflict priority. Effective settings, workspace, harness, trust, and remote environment affect discovery. |

CLI installation and skill paths are described in the official
[installation guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli)
and [skills guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills).
Its [instruction guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions)
and [command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
own instruction combination and `COPILOT_HOME` behavior. Native startup may
migrate legacy configuration to `settings.json`; do not start the client merely
to treat its configuration as a read-only probe.

VS Code's [Copilot setup guide](https://code.visualstudio.com/docs/setup/copilot),
[skill guide](https://code.visualstudio.com/docs/agent-customization/agent-skills),
and [instruction guide](https://code.visualstudio.com/docs/agent-customization/custom-instructions)
own editor activation and discovery. Check `chat.useAgentsMdFile`, effective
skill-location settings, and the selected harness before claiming native context
or skill loading. These profiles do not qualify Copilot cloud agents, Visual
Studio, JetBrains, or other application variants. Local filesystem tests do not
prove that a remote editor host can access the same paths.

## Cline profiles

The `cline` family has `cline-cli` and `cline-vscode` selectors for executable
and VS Code extension installation surfaces. Both use `.cline/skills` in the
repository and `~/.cline/skills` for personal skills. Selecting both retains
two logical owners while deduplicating writes. These variants share one native
skill contract; separate identifiers do not claim different runtime behavior.
Symlink and portable fixtures cover installation, verification, partial detach,
and preservation of custom content and native settings/session fixtures. Host
qualification remains `not-run`.

```bash
localsetup plan --target-directory PROJECT --tools cline-cli cline-vscode --skill-scope both --skills ls-context
localsetup install --target-directory PROJECT --tools cline-cli cline-vscode --skill-scope both --skills ls-context --apply
localsetup verify --target-directory PROJECT
```

The CLI discovery candidate is `cline`; the IDE identity is the official
`saoudrizwan.claude-dev` extension in the intended VS Code profile and extension
host. Editor executable presence alone does not establish Cline availability.
These commands install skill adapters only. Application installation, account
authentication, and provider selection remain separate.

Use the [official installation guide](https://docs.cline.bot/getting-started/installing-cline)
with a verified release artifact. Its CLI package and Node requirements differ
from [the inspected CLI source](https://github.com/cline/cline/blob/dac3b35ba485dbab3b5a73aca239b0d07ce071cf/apps/cli/package.json);
resolve that mismatch for the selected release before installation. Do not infer
a package migration or minimum compatible version from moving source.

The [skills guide](https://docs.cline.bot/customization/skills) documents the
selected native roots. Current source also lists common skill roots in the
[IDE loader paths](https://github.com/cline/cline/blob/dac3b35ba485dbab3b5a73aca239b0d07ce071cf/apps/vscode/src/core/storage/skill-directories.ts)
and [SDK shared paths](https://github.com/cline/cline/blob/dac3b35ba485dbab3b5a73aca239b0d07ce071cf/sdk/packages/shared/src/storage/paths.ts).
Those source observations do not establish released CLI wiring or installed
host behavior. LocalSetup keeps native projections; it does not combine all
mentioned roots into a verified search order. Duplicate names, toggles, custom
roots, enterprise policy, and minimum versions require host qualification.

The [configuration guide](https://docs.cline.bot/getting-started/config) and
[CLI reference](https://docs.cline.bot/cli/cli-reference) differ on legacy
settings/skill layout. Data-directory overrides do not prove that every
customization root moves. Preserve existing legacy directories, `~/.cline/data`,
credentials, sessions, databases, settings, plugins, hooks, and custom rules.
[Instruction discovery](https://docs.cline.bot/customization/cline-rules),
including `AGENTS.md`, is separate from skill discovery; policy insertion and
personal instruction management remain unqualified.

## Windsurf / Cascade retention

The `windsurf` family records the audited `windsurf-cascade` IDE identity.
LocalSetup treats it as **retained-only** while upstream availability guidance
conflicts. This is an installation policy, not a declaration that Cascade has
been retired. No fresh-install platform projection is emitted, so
`--tools windsurf-cascade` is rejected for fresh selection. Existing receipts
retain their client identity and recorded paths for verification, package
updates, and detach. Symlink and portable fixtures exercise that lifecycle
using a synthetic historical catalog; they do not certify a desktop build.

[The Cascade skills guide](https://docs.devin.ai/desktop/cascade/skills) confirms
repository `.agents/skills` and personal `~/.agents/skills`, alongside native
`.windsurf/skills` and `~/.codeium/windsurf/skills`. Retain existing native content;
do not duplicate projections or infer cross-root collision precedence.
[AGENTS.md discovery](https://docs.devin.ai/desktop/cascade/agents-md) supports
root and nested instructions, but this profile does not manage policy insertion.
[Rules and memories](https://docs.devin.ai/desktop/cascade/memories) are separate
from skills and do not establish a durable scheduler.

The [transition FAQ](https://docs.devin.ai/desktop/devin-desktop-faq) says Cascade
remains available through July after the June 2026 desktop rename, while
[current agent-selection guidance](https://docs.devin.ai/desktop/devin-local)
still describes Cascade selection and fallback. Neither definitive removal nor
universal availability is established by those conflicting statements.
Qualification is blocked pending an exact build and account where Cascade can
be selected, with its effective settings and harmless skill discovery checked.
Do not authenticate, change agent settings, or install an old build merely to
resolve this uncertainty without target-specific authority.

[Current downloads](https://devin.ai/download) supply Devin Desktop. The
`devin-desktop`, `surf`, and `windsurf` launcher candidates establish desktop
presence only. They do not prove Cascade availability. Devin Local and
JetBrains are outside this profile; never substitute their discovery contracts.
Native settings, MCP configuration, memories, sessions, workspaces, enterprise
policy, custom rules, and skills remain vendor/user-owned.

For a healthy existing receipt, retain scope and client selection by omitting
selectors during the recorded update:

```bash
localsetup verify --target-directory PROJECT
localsetup plan --target-directory PROJECT
localsetup update --target-directory PROJECT
```

`plan` is read-only; `update` applies the recorded package refresh. This does
not upgrade the desktop application or prove host compatibility. Automatic repository-only doctor repair is not qualified for this retained
profile: it returns a preservation blocker before client inference, even when
application is requested. Preserve the receipt and custom content and use a
verified backup or reviewed recorded-path recovery under the
[ownership guidance](ADAPTER_OWNERSHIP.md). Personal and combined repair retain
their recorded ownership routes. Do not replace an unhealthy receipt with fresh
profile selection.

## Amp CLI

The `amp` family provides the `amp-cli` selector, with repository
`.agents/skills` and personal `~/.agents/skills` projections. The executable
discovery candidate is `amp`; desktop, editor, web, and remote-agent surfaces
are outside this qualification. Catalog and filesystem fixtures are implemented;
installed host qualification remains `not-run`.

```bash
localsetup plan --target-directory PROJECT --tools amp-cli --skill-scope both --skills ls-context
localsetup install --target-directory PROJECT --tools amp-cli --skill-scope both --skills ls-context --apply
localsetup verify --target-directory PROJECT
```

`plan` describes the intended layout. Application performs a fresh static
collision preflight before managed writes. A conflict leaves existing origins
in place; do not rename, relocate, or delete user skills automatically. Selecting
multiple clients that share the chosen physical paths retains their logical
owners without duplicating writes. Personal and combined repair apply the same
collision guard before restoring exposure.

[Amp's skills documentation](https://ampcode.com/docs/customize/skills) resolves
duplicates by frontmatter `name`, with first occurrence winning. Its documented
local order begins with `~/.config/agents/skills`, `~/.agents/skills`, and
`~/.config/amp/skills`, followed by project/ancestor `.agents/skills` and
`.claude/skills`, then personal `~/.claude/skills`. Thus global content can
shadow project content. The guard scans those roots and checks both directions:
an existing source masking the planned skill, or a planned source masking
existing content. Different directory basenames do not avoid a name conflict.
Jointly planned projections and existing links to the same planned canonical
library are allowed. An unchanged portable counterpart also requires recorded
ownership, matching adapter/library metadata, and matching package content and
link metadata. Ordinary ownership checks still protect custom entries. Writes
through another client also check affected recorded Amp owners sharing the
physical path or updated library packages; known repository targets are included.

Frontmatter discovery is bounded to 4,096 directory entries and 16 KiB per
skill; accepting an owned portable counterpart additionally compares its package
content and link metadata with the intended payload. Unreadable or malformed metadata, unresolved entries, and symlink
roots block qualification. Exact runtime ancestor stopping/order, configured
paths, plugins, built-ins, remote repositories, and session reload behavior are
not qualified by this filesystem scan. It does not prove equivalence to the
effective Amp catalog. No automatic Amp command is invoked: skill discovery can
connect declared MCP servers, and reload can fetch remote sources.

[Plugin documentation](https://ampcode.com/docs/customize/plugins) establishes
XDG configuration overrides for plugins, not loose skills. A nondefault
`XDG_CONFIG_HOME` blocks this preflight until the effective loose-skill contract
is qualified; the guard does not silently replace the documented roots.
[Native settings](https://ampcode.com/docs/cli/settings), including configured
skill paths, permissions, MCP configuration, and thread visibility, remain
unmodified. [AGENTS.md guidance](https://ampcode.com/docs/customize/agents-md)
has its own ancestor/subtree and personal-file behavior; policy insertion and
private guidance management remain unqualified.

Use the [official installation route](https://ampcode.com/docs/cli) only with a
verified installer and selected release artifact. Authentication and upstream
background-update behavior require separate target qualification. Installing
LocalSetup skill adapters does not install Amp, configure its updater, or grant
account, network, or provider authority.

## Goose CLI

The `goose` family exposes `goose-cli`, using repository `.agents/skills` and
personal `~/.agents/skills`. The discovery candidate is `goose`; Goose Desktop
is a separate interface outside this qualification. Resource-bearing skill
packages, repository/personal/both ownership and symlink/portable projections
have filesystem fixtures. Installed host qualification remains `not-run`.

```bash
localsetup plan --target-directory PROJECT --tools goose-cli --skill-scope both --skills ls-context
localsetup install --target-directory PROJECT --tools goose-cli --skill-scope both --skills ls-context --apply
localsetup verify --target-directory PROJECT
```

The plan describes layout without creating Goose configuration. Application,
personal/combined repair, and writes affecting recorded Goose owners require a
fresh static configuration check before managed writes. Verification reports
`goose_skills_configured` with `scope: static-configuration` and
`host_verified: false`. Passing this prerequisite does not attest that a
particular Goose build or session can load skills.

On Linux/WSL and macOS, the qualified default file is
`~/.config/goose/config.yaml`. An explicit native Skills entry has this shape:

```yaml
extensions:
  skills:
    enabled: true
    type: platform
    name: skills
```

This example describes user-owned configuration, not an instruction to overwrite
an existing file. LocalSetup never enables extensions or runs Goose's native
configuration loader, which may migrate and save state. Missing, malformed,
ambiguous, oversized (over 256 KiB), aliased, or tool-restricted configuration
returns unknown; explicit native Skills `enabled: false` returns disabled.
Only absent or empty `available_tools` is qualified. System configuration at
`/etc/goose/config.yaml`, `GOOSE_PATH_ROOT`, `GOOSE_ADDITIONAL_CONFIG_FILES`,
`EXTENSIONS`, or a nondefault `XDG_CONFIG_HOME` requires separate effective-state
qualification. The checker does not merge these layers. Session/recipe overrides
and build availability remain outside the static predicate.

The [skills guide](https://goose-docs.ai/docs/guides/context-engineering/using-skills/)
and [Summon guide](https://goose-docs.ai/docs/mcp/summon-mcp/) describe a separate
Skills extension, while the [Skills extension page](https://goose-docs.ai/docs/mcp/skills-mcp/)
contains conflicting deprecation guidance. Therefore LocalSetup assumes neither
default activation nor substitution by Summon. Qualify the selected release and
session before relying on host behavior. Additional private-home discovery,
plugins, root precedence and duplicate-name resolution remain unqualified.

[Context files](https://goose-docs.ai/docs/guides/context-engineering/using-goosehints/)
include `AGENTS.md` and `.goosehints`; hierarchy and
`CONTEXT_FILE_NAMES` overrides are distinct from skill projection, and automatic
policy insertion remains unqualified. Native settings, keyring, sessions,
databases, recipes, plugins and custom extensions remain user-owned.
[Environment overrides](https://goose-docs.ai/docs/guides/environment-variables/)
require the effective configuration to be established separately. Use the
[official installation guidance](https://goose-docs.ai/docs/getting-started/installation/)
with a verified selected artifact; installing adapters does not install Goose,
authenticate, configure providers, or authorize network calls.

## Pi coding agent

The `pi` family provides `pi-cli`, with executable candidate `pi`, repository
`.agents/skills` and personal `~/.agents/skills`. LocalSetup projects directory
packages containing `SKILL.md` and supporting resources. Filesystem fixtures
cover both projection modes, shared owners, detach, and preservation of custom
skills and native state. Installed Pi qualification remains `not-run`.

```bash
localsetup plan --target-directory PROJECT --tools pi-cli --skill-scope both --skills ls-context
localsetup install --target-directory PROJECT --tools pi-cli --skill-scope both --skills ls-context --apply
localsetup verify --target-directory PROJECT
```

These commands manage filesystem ownership. Successful verification does not
establish that Pi trusts the project or has loaded its skills. Pi's
[skills documentation](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/coding-agent/docs/skills.md)
describes common and native roots, with project/ancestor discovery conditional
on trust. Native `.pi/skills` and `~/.pi/agent/skills` can accept loose Markdown
skills; LocalSetup does not relocate or convert them into common packages.
Duplicate frontmatter names retain the first discovery with a warning. Exact
source precedence, settings filters, explicit `--skill` paths and installed
runtime discovery need separate qualification; `--no-skills` does not suppress
all explicitly supplied skills.

[Pi trust and settings](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/coding-agent/docs/settings.md)
are user-owned. Trust may allow project settings, missing package installation,
and extension execution. LocalSetup never approves trust, changes
`~/.pi/agent/trust.json`, supplies approval flags, or invokes Pi to validate an
adapter. Noninteractive Pi modes do not prompt for trust; effective saved,
parent, default and command-line decisions determine loading. Review those
controls in the intended host session rather than treating adapter installation
as consent. Settings at `.pi/settings.json` and `~/.pi/agent/settings.json`,
resource filters, packages and extensions remain unchanged.

The [CLI guide](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/coding-agent/README.md)
documents context discovery and `AGENTS.override.md` replacement within a
directory; do not overwrite overrides or equate context and skill trust rules.
Automatic policy insertion is unqualified. `PI_CODING_AGENT_DIR` and
`PI_CODING_AGENT_SESSION_DIR` can redirect native state. Auth, models, sessions,
trust records and system-prompt overrides remain outside adapter ownership.

Use the current official installation guide with a verified selected artifact.
The cited source uses `@earendil-works/pi-coding-agent`; historical package names
require release/registry reconciliation before installation. Source metadata is
not proof of package availability. Startup network behavior and authentication
also require separate target qualification; no Pi command is invoked here.
Pi, OMP and LSCli are distinct runtimes. Common skill-package shape does not
establish extension, provider, permission, session, MCP or sandbox compatibility.

## Hermes Agent

The `hermes` family exposes `hermes-agent`, with executable candidate `hermes`.
It writes independent portable packages to repository `.hermes/skills` and the
explicit default personal profile `~/.hermes/skills`. Native copies keep mutable
skills isolated from the canonical LocalSetup library; they are not read-only.
Saved package baselines and independent ownership receipts prevent automatic
replacement or removal of native edits or deletions. Preserve and reconcile those
changes before retrying update, repair, detach or rollback. Keep Hermes writers
quiescent during maintenance; LocalSetup locks coordinate LocalSetup operations.

```bash
localsetup plan --target-directory PROJECT --tools hermes-agent --skill-scope both --skills ls-context --mode portable
localsetup install --target-directory PROJECT --tools hermes-agent --skill-scope both --skills ls-context --mode portable --apply
localsetup verify --target-directory PROJECT
```

Symlink mode is refused. Personal selection explicitly targets the default
profile; a nondefault `HERMES_HOME` refuses these writes. Named profiles, sticky
CLI selection and context-local API overrides need separate qualification. These
commands do not determine or change a running session's selected profile.
[Hermes profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles/)
keep native configuration and state separate; do not use default-profile adapter
success as evidence that another profile has loaded its skills.

[Native skill discovery](https://github.com/NousResearch/hermes-agent/blob/245e48008fa814b3251f50755eb656bd9fb86cb1/agent/skill_utils.py)
requires a Git root and explicit project trust for project packages. Discovery
can be disabled or content quarantined. LocalSetup does not approve trust or add
`skills.external_dirs`. Shared-home external sources require native configuration
and can be writable; pointing Hermes at canonical LocalSetup symlinks would not
provide the copy isolation implemented here. Resource precedence, categorized
name ambiguity and effective session loading require host verification.

Native configuration, profiles, `SOUL.md`, sessions, databases, memory, cron,
gateway state, plugins, vendor manifests and custom skills remain user-owned.
Hermes-specific context and `AGENTS.override.md` can affect instruction loading;
context discovery does not establish skill trust. `/reload-skills` or
`/reload_skills` rescans skills; credential reload and MCP reload are separate
operations. Use the official installation guide with a verified selected artifact.
The adapter performs no Hermes installation, startup, authentication or provider
call. Filesystem lifecycle fixtures are qualified; installed-host behavior remains
`not-run`.

## Qwen Code

The `qwen-code` family exposes `qwen-code-cli`, with executable candidate `qwen`.
Repository `.agents/skills` and personal `~/.agents/skills` support the common
LocalSetup directory-package projection. Filesystem fixtures cover symlink and
portable modes, resources, shared owners, detach and native-content preservation.
Installed Qwen qualification remains `not-run`.

```bash
localsetup plan --target-directory PROJECT --tools qwen-code-cli --skill-scope both --skills ls-context
localsetup install --target-directory PROJECT --tools qwen-code-cli --skill-scope both --skills ls-context --apply
localsetup verify --target-directory PROJECT
```

The [v0.23.0 storage source](https://github.com/QwenLM/qwen-code/blob/98a9c964158697dd5631d15a62174684ff7bbb53/packages/core/src/config/storage.ts)
includes both `.qwen` and `.agents` skill providers. `QWEN_HOME` relocates the
native user root; common personal discovery remains under the user's home.
The [release skill manager](https://github.com/QwenLM/qwen-code/blob/98a9c964158697dd5631d15a62174684ff7bbb53/packages/core/src/skills/skill-manager.ts)
uses project, user, extension and bundled precedence, with native `.qwen` ahead
of common `.agents` within a scope. LocalSetup preserves native same-name skills;
putting a package in common storage does not override them. This is verified-at
source evidence, not a claim that v0.23.0 first introduced the feature.

Bare mode disables skill loading, and safe mode restricts it to bundled skills.
Settings, enablement and project-root handling also affect the effective inventory.
Source link validation accepts external directory targets, but successful adapter
verification still establishes filesystem exposure, not activation, resource
execution or a permission grant. Qualify those behaviors in the intended release
and session before claiming host support.

Native `QWEN.md`, settings, extensions, credentials, sessions, runtime overrides
and generated AutoSkill content remain user-owned. Keep learned skills in native
locations, separate from installed canonical packages. This adapter does not
redirect creation, rewrite native collisions, change skill settings or invoke
Qwen. Use official installation guidance with a verified selected artifact;
installation, authentication and functional host qualification are separate.

The [versioned skill guide](https://github.com/QwenLM/qwen-code/blob/98a9c964158697dd5631d15a62174684ff7bbb53/docs/users/features/skills.md)
limits automatic curation to native `auto-skill-*` directories carrying the
`source: auto-skill` marker. Ordinary projected packages are outside that
selection; this is not protection against general agent file or shell tools.
The [memory guide](https://github.com/QwenLM/qwen-code/blob/98a9c964158697dd5631d15a62174684ff7bbb53/docs/users/features/memory.md)
documents `QWEN.md`, existing `AGENTS.md` and personal project
`.qwen/QWEN.local.md`; LocalSetup does not duplicate or insert native context.
Effective context precedence remains unqualified, and generated memory settings
and optional synchronization remain untouched.

## Kimi CLI

The `kimi` family exposes `kimi-cli`, with executable candidate `kimi`, common
repository `.agents/skills` and personal `~/.agents/skills`. Local filesystem
fixtures qualify symlink and portable ownership. Installed Kimi behavior remains
`not-run`; remote backends and custom-agent prompt assembly require separate
qualification.

```bash
localsetup plan --target-directory PROJECT --tools kimi-cli --skill-scope both --skills ls-context
localsetup install --target-directory PROJECT --tools kimi-cli --skill-scope both --skills ls-context --apply
localsetup verify --target-directory PROJECT
```

The [pinned skill loader](https://github.com/MoonshotAI/kimi-cli/blob/86f136422a0aae6b217ea49e7ea1d2e8a1defcd2/src/kimi_cli/skill/__init__.py)
selects the first existing generic personal directory: `~/.config/agents/skills`
before `~/.agents/skills`. Even an empty preferred directory masks the latter
root. LocalSetup refuses affected personal writes and reports the mask during
verification. It preserves both roots; it does not move content, populate both,
or delete an empty preferred root. A symlink alias to the same physical directory
does not mask that directory. Repository-only operations and recorded detach
remain available. An unknown or unreadable root requires path review.

`merge_all_available_skills` controls branded roots only; enabling it cannot
remove the generic mask. Branded `.kimi/skills`, `.claude/skills` and `.codex/skills`
precede generic roots within their scope. Preserve native skill identities and
check effective origins when duplicates exist; filesystem success does not prove
which skill wins. Flat native Markdown skills remain in place. Project discovery
uses the nearest Git root, with cwd fallback outside Git.

The [source configuration](https://github.com/MoonshotAI/kimi-cli/blob/86f136422a0aae6b217ea49e7ea1d2e8a1defcd2/src/kimi_cli/config.py)
defaults brand merging on and supports additive `extra_skill_dirs`.
The [CLI source](https://github.com/MoonshotAI/kimi-cli/blob/86f136422a0aae6b217ea49e7ea1d2e8a1defcd2/src/kimi_cli/cli/__init__.py)
replaces automatic user/project discovery with explicit `--skills-dir` values,
while configured extras, plugins and built-ins remain. LocalSetup does not supply
that flag or change native settings. `KIMI_SHARE_DIR` controls this source's native
configuration/runtime root, not common skill locations; default configuration is
`~/.kimi/config.toml`. Explicit configuration and run overrides remain unqualified.

Native `AGENTS.md`, custom agents, MCP configuration, plugins, credentials,
sessions, share-root state and skills remain user-owned. Skill projection does
not grant tool execution, establish flow compatibility with LocalSetup workflows,
or alter context assembly. Follow the official installation guide with a verified
selected artifact; no Kimi installation, startup, authentication or provider call
is performed by this adapter.

## Factory Droid

The `factory` family exposes `factory-droid`, with executable candidate `droid`.
It projects common repository `.agents/skills` and personal `~/.agents/skills`
directory packages using recorded shared ownership. Both LocalSetup projection
modes are filesystem-tested. Portable mode avoids relying on Droid's unqualified
handling of arbitrary external symlink targets through common roots.

```bash
localsetup plan --target-directory PROJECT --tools factory-droid --skill-scope both --skills ls-context --mode portable
localsetup install --target-directory PROJECT --tools factory-droid --skill-scope both --skills ls-context --mode portable --apply
localsetup verify --target-directory PROJECT
```

[Factory skills documentation](https://docs.factory.ai/harness/skills) describes
`.factory/skills`, `.agents/skills` and `.agent/skills` at project and personal
scopes. Duplicate sanitized identities within one folder bucket invalidate a
skill. The exact sanitizer and same-target alias deduplication are not established
by the available primary documentation. LocalSetup therefore accepts canonical
lowercase alphanumeric names separated by single hyphens verbatim, and reports
other names for native catalog review. This conservative prerequisite does not
claim that Droid rejects those names or reproduce its unpublished normalization.

The bounded static scan groups the three project roots separately from the three
personal roots. It stops at each `SKILL.md` package and refuses duplicate desired
identities, aliases, unknown metadata, traversal cycles, scans over 4,096 entries
or depth over 32. A package at an adapter root cannot hide planned children.
Apply and repair check affected recorded ownership even when another client
changes a shared path or package. Existing custom content remains in place;
resolve conflicting origins explicitly before retrying.

Filesystem verification is not an effective Droid catalog check. In an authorized
installed session, use `/skills` Effective and `/diagnostics` to inspect winning
sources, invalid/disabled entries and resources. Project plugins, user plugins,
built-ins, mission/organization/CLI overrides and dynamically activated nested
folders remain outside static qualification. Native symlink support is documented
in release notes; external targets through common roots remain unqualified.

[Factory settings](https://docs.factory.ai/droid-cli/settings) include personal
and project `settings.json` plus adjacent `settings.local.json` overlays.
`disabledSkills` combines settings across scopes. LocalSetup preserves these
controls, custom droids, plugins, credentials, sessions, hooks and permissions.
Skill `allowed-tools` metadata does not establish a runtime sandbox. Root and
nested [AGENTS.md guidance](https://docs.factory.ai/harness/agents-md) remains
native; this adapter does not generate aliases or change custom prompt assembly.

Use official installation guidance with a verified selected artifact. Starting
Droid can initialize state and require sign-in; this adapter invokes no Droid
command and performs no authentication, provider call or automatic update change.
Installed-host behavior remains `not-run`.

## Google Antigravity variants

Antigravity application, IDE and CLI records represent different runtime
contracts. Select them by product identity; an executable name or a shared
`.agents` parent does not establish compatibility between them.

| Variant | Directory-package adapter | Personal skills | Qualification |
| --- | --- | --- | --- |
| `antigravity-app` | Common project `.agents/skills` | `~/.gemini/config/skills` | Filesystem fixtures; host not-run |
| `antigravity-ide` | Experimental metadata; no fresh export | `~/.gemini/antigravity/skills` | Documented paths; host not-run |
| `antigravity-cli` | No generic package export | Flat `~/.gemini/antigravity-cli/skills/<name>.md` | Format boundary; host not-run |

The [application skill guide](https://antigravity.google/docs/skills/) and
[IDE skill guide](https://antigravity.google/docs/ide/skills/) independently name
their different personal roots. Both document `.agents/skills` with singular
`.agent/skills` compatibility. Neither establishes automatic `~/.agents/skills`
discovery. LocalSetup writes the selected application root, preserves the
compatibility roots and other variants, and does not migrate existing content.

```bash
localsetup plan --target-directory PROJECT --tools antigravity-app --skill-scope both --skills ls-context --mode portable
localsetup install --target-directory PROJECT --tools antigravity-app --skill-scope both --skills ls-context --mode portable --apply
localsetup verify --target-directory PROJECT
```

Symlink and portable projection are filesystem-tested; native symlink loading,
resource selection and duplicate precedence remain unqualified. The portable
example avoids assuming external-link support. Exact application and IDE binary
identities require explicit build identification. No automatic product replacement
or installation is implied by adding the application adapter.

[IDE rules](https://antigravity.google/docs/ide/rules/) and
[application rules](https://antigravity.google/docs/rules-workflows/) document
`.agents/rules`, `.agent/rules` compatibility and global `~/.gemini/GEMINI.md`.
The IDE record corrects its prior personal skill and global rule pointers; it does
not move or remove files at old paths. Native rule activation and file limits
remain separate from skill loading. Experimental IDE rule insertion is unverified;
LocalSetup does not translate arbitrary context into native rules automatically.
Existing LocalSetup-owned framework state is distinct from upstream native state.

The [CLI plugin guide](https://antigravity.google/docs/cli/plugins/) documents
flat project `.agents/skills/<name>.md` files, not established
`<name>/SKILL.md` packages. Its executable is `agy`. CLI plugins also have
manifests, agents, rules, hooks and MCP components. A future conversion must retain
supporting assets, relative references, metadata, namespaces, collisions and
rollback; copying only `SKILL.md` is insufficient. Until that conversion is
qualified, `antigravity-cli` is an experimental, non-exported catalog record.
Preserve CLI settings, sessions, tokens, plugins and migration state. No Gemini
migration, authentication, app startup or native permission change is performed.

## Roo Code legacy

The `roo-code/roo-code-legacy` profile records the archived Roo Code IDE
extension. [The upstream repository](https://github.com/RooCodeInc/Roo-Code)
is archived; LocalSetup emits no fresh-install projection, and fresh
`--tools roo-code-legacy` selection fails. This is a retained-installation
compatibility record, not an active bundle recommendation or a Kilo profile.
The retained extension identity is `RooVeterinaryInc.roo-cline`; no standalone
executable identity or current marketplace availability is asserted.

The [v3.54.0 skill loader](https://github.com/RooCodeInc/Roo-Code/blob/27001b2b5aa47b65e8a6ba1914e0f4216be0ebb0/src/services/skills/SkillsManager.ts)
scans ordinary `.agents/skills` and `.roo/skills` at project and home scopes,
plus `skills-<mode>` directories for known modes. Project scope takes priority
over home; native Roo entries replace common entries with the same discovery
key (name, scope and first mode). Restricted skills beat generic skills within
a scope; multiple mode arrays prevent a simple total ordering. This describes
pinned source behavior; effective discovery in
a retained host remains unverified. Mode-specific directories stay outside
the portable common-skills contract and are preserved as native overlays.

The loader follows directory links, including external targets, and requires
the visible directory or link name to equal the skill's metadata name. Names
must be 1–64 lowercase ASCII letters or digits with single intervening hyphens.
Its native mode editor can write discovered `SKILL.md` files through links.
Do not use that editor against shared canonical packages; keep native writers
quiescent during LocalSetup lifecycle operations. This is an operational
boundary, not filesystem isolation or proof against arbitrary native writes.

The retained profile uses the ordinary common roots as metadata. Existing
receipts remain authoritative for actual paths and scope: a missing receipt
does not authorize inferred installation. For a healthy recorded installation:

```bash
localsetup verify --target-directory PROJECT
localsetup plan --target-directory PROJECT
localsetup update --target-directory PROJECT
```

These operations verify and refresh recorded framework packages, not the Roo
extension. Synthetic historical receipts exercise both adapter modes, scope
retention and detach while preserving custom common/native/mode content.
Repository-only doctor repair refuses automatic inference; use reviewed
recorded-path recovery under [adapter ownership](ADAPTER_OWNERSHIP.md).
Native rules, custom modes, settings, credentials, sessions and skills remain
unmanaged. Root `AGENTS.md` (with `AGENT.md` fallback) depends on the native
`roo-cline.useAgentRules` setting; LocalSetup does not alter that setting or
insert policy. Native `.roo/rules` and mode-specific rules retain their own
loading contract. Host acceptance still requires an
explicit target and exact retained build, harmless discovery checks, effective
mode priority and link-name validation; no host installation was performed.

## Continue CLI legacy

The `continue/continue-cli-legacy` record covers the `cn` executable from
`@continuedev/cli`, not historical Continue IDE extensions. The
[pinned upstream maintenance notice](https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/README.md)
states that the repository is no longer actively maintained. LocalSetup keeps
this profile retained-only and emits no fresh-install projection; fresh
`--tools continue-cli-legacy` selection fails. The inspected source tree is
not proof of the contents of a released binary.

The [pinned CLI loader](https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/extensions/cli/src/util/loadMarkdownSkills.ts)
scans project `.continue/skills`, project `.claude/skills`,
and `<continue-home>/skills`. It does not scan `.agents/skills`. Continue home
is `CONTINUE_GLOBAL_DIR` when supplied, otherwise `~/.continue`; dotenv loading
precedes that resolution. LocalSetup records the default native path as
metadata and retains existing receipt paths during lifecycle operations. It
does not load dotenv, initialize Continue configuration or infer a relocation
from the current shell environment. A successful filesystem check therefore
does not establish the effective `cn` home or discovery scope.

The loader checks immediate child directory entries and skips per-package
directory symlinks. Its resource walker disables link following. A physical
native package is the candidate shape for retained-host qualification; the
existing symlink adapter shape is not qualified for CLI loading. Recorded
updates preserve the receipt's mode and paths without converting or moving
user content. Do not replace a symlink merely to make a filesystem check pass;
any conversion needs exact-build qualification and reviewed ownership recovery.

For a healthy recorded installation, use the scope-preserving commands:

```bash
localsetup verify --target-directory PROJECT
localsetup plan --target-directory PROJECT
localsetup update --target-directory PROJECT
```

Synthetic receipts exercise native adapter paths in both stored modes,
recorded scope, updates and detach. This establishes filesystem preservation,
not skill activation; no Continue host was installed or invoked. Automatic
repository-only repair refuses inference, and missing or unhealthy receipts
need [recorded-path recovery](ADAPTER_OWNERSHIP.md). Preserve native settings,
credentials, sessions, custom skills, `.claude` compatibility content and
common-root neighbors. Duplicate skill selection has no established stable
priority; do not infer precedence from root enumeration order. Generated
slash-command names remain a CLI-specific surface. Explicit `--config` and
repeatable `--rule` inputs have their own native contract; policy insertion,
universal `AGENTS.md` loading and effective context loading are unqualified.

## Aider explicit instruction files

The `aider/aider` CLI profile provides a manual instruction-file integration.
Use a qualified `aider` executable and explicitly select a reviewed regular
file. [Aider's conventions guide](https://aider.chat/docs/usage/conventions.html)
documents `--read`; it does not establish automatic `AGENTS.md` discovery or a
native Agent Skills catalog. LocalSetup emits no skill-platform projection,
so fresh `--tools aider` selection fails. No skill directory, configuration,
provider session or native history is created by this catalog record.

For an existing repository context file, run from the intended project and
list **every file intended as read-only context** on the command line:

```bash
aider --read /absolute/project/AGENTS.md --read /absolute/project/CONVENTIONS.md
```

Replace these example paths with reviewed files that actually exist. This is
a native Aider invocation, not a provider-free validation command; starting it
uses the owning session's provider and disclosure authority. Read-only context
is still disclosed to the model. It does not grant or restrict shell/file-tool
authority, and skill metadata or resource links do not become progressive
skill discovery merely because the instruction file mentions them.

The [pinned argument handling](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/main.py)
resolves relative read paths from the launch directory, not from the YAML
configuration's location. Directory arguments can recursively include files;
use regular file paths and avoid passing an entire skill tree. Files under
other directories and nested context are not automatically selected by this
procedure. Review the selected content and its size before adding it.

**Read lists replace lower-priority lists; they do not merge across layers.**
This follows the [pinned configuration parser](https://github.com/bw2/ConfigArgParse/blob/9453a69a95bd4f7fbc5ad86d16813ed489336118/configargparse.py)
used by the inspected Aider source.
Repeated CLI `--read` arguments accumulate within the command line, but that
CLI list overrides environment/configuration read values. Before using the
example, include any existing convention files you still intend to load.
Likewise, adding a project configuration's `read` list can shadow a home list.
The [YAML configuration guide](https://aider.chat/docs/config/aider_conf.html)
describes home, Git-root and working-directory search order, later-file
priority, and explicit `--config` selection. An existing file on disk is not
proof that its list is effective for a particular invocation.

For users who choose persistent configuration, edit the selected configuration
manually, preserving its existing keys, comments and intended read entries.
For example, an existing list containing `CONVENTIONS.md` must retain that
entry when the reviewed instruction file is added:

```yaml
read:
  - /absolute/project/CONVENTIONS.md
  - /absolute/project/AGENTS.md
```

Absolute paths are machine-local; keep real local paths out of public
configuration examples and committed portable templates. Use launch-independent
paths when appropriate, and account for higher-priority
configuration, environment and CLI inputs. Never replace the complete YAML
file with this fragment. LocalSetup does not rewrite YAML, add a global read
entry, concatenate installed skills or enable this integration by default.
Remove only the entry you added to undo a manual configuration change; restore
its prior value if you changed an existing entry. Preserve native settings,
credentials, chat history, sessions and custom content.

Catalog qualification is bounded to these explicit instructions. Filesystem
projection is not applicable, and host loading has not been tested. Exact
installed-build qualification must verify the effective read set from the
intended launch directory without inferring it from another configuration
scope. No paid model call or Aider installation was performed for this record.

## Claude Code CLI

`claude-code/claude-code-cli` retains the `claude-code` installer selector and
`claude` executable. The [skills guide](https://code.claude.com/docs/en/skills)
establishes project `.claude/skills` and personal `~/.claude/skills`, including
ordinary directory links to external targets. Identical physical targets load
once. Enterprise skills precede personal, then project skills; plugin names
are scoped, and skills win over same-name legacy commands. This does not
establish common `.agents/skills` discovery, plugin behavior or Cowork support.
The filesystem fixtures cover ordinary packages and resources in symlink and
portable modes while preserving custom content. No native host was invoked.

The [directory reference](https://code.claude.com/docs/en/claude-directory)
defines `CLAUDE_CONFIG_DIR` as a relocation of personal native paths. This
adapter qualifies the default personal root. Nondefault, empty or relative
values require separate qualification: apply, affected package refresh and
personal repair refuse default personal writes before mutation, and selected
verification reports the mismatch. The check reads the environment without
loading native settings or credentials. Recorded detach remains available;
no root is moved or converted. An absolute override resolving to the same
default root is accepted. Repository-only operations remain available unless
they also refresh packages belonging to a recorded personal Claude owner.

For default personal scope and a qualified project, ordinary installation is:

```bash
localsetup plan --tools claude-code --skill-scope both --mode portable
localsetup install --tools claude-code --skill-scope both --mode portable --apply
```

Inspect the plan before application. A personal-root diagnostic means resolve
the native selection and recorded ownership explicitly; it does not authorize
moving files or clearing the environment to conceal a real configured root.
[Ownership recovery](ADAPTER_OWNERSHIP.md) preserves existing records and custom
neighbors. LocalSetup framework state is separate from native Claude state.

[Memory discovery](https://code.claude.com/docs/en/memory) includes `CLAUDE.md`,
`.claude/CLAUDE.md`, `CLAUDE.local.md` and modular `.claude/rules`. Imports or a
symlink can reuse `AGENTS.md`; automatic loading of `AGENTS.md` itself is not
established. Preserve existing instruction content, imports and native rules.
Additional-directory skill discovery does not imply the same memory loading:
that memory behavior has a separate native setting. Context is model guidance,
not enforced permissions.

[Settings](https://code.claude.com/docs/en/settings) use separate rules. Shared
`.claude/settings.json` follows the primary working directory. From v2.1.211,
local settings normally use the Git root or the main checkout root for a
worktree, with non-Git, home-root, Windows and ownership exceptions. Older
starting-directory local files may also load; root values win and permission
rules combine. Settings re-resolution after `/cd` requires v2.1.246 or later.
Relative sandbox/permission paths still anchor to primary cwd. These are
vendor-documented version boundaries, not tested host guarantees; SDK settings
resolution does not qualify the CLI. Skill operations preserve settings,
approvals, global excludes, credentials and sessions.

The documented [goal command](https://code.claude.com/docs/en/goal) supports
`/goal`, `/goal clear` and print-mode use. It requires workspace trust and
enabled hooks, and uses a model to evaluate transcript evidence between turns.
Evaluation incurs provider usage and does not independently inspect the files
or execute validation. Completion is native session outcome, not external
controller acceptance. No goal is activated by LocalSetup installation.

The [v2.1.139 release](https://github.com/anthropics/claude-code/releases/tag/v2.1.139)
introduced goals. Current documentation limits the condition to 4,000
characters; no byte limit or Unicode counting algorithm is established here.
Active conditions restore on resume, but turn count, elapsed timer and token
baseline reset. Achieved or cleared goals do not restore. The resume-picker
route requires v2.1.239 or later for goal restoration. An impossible verdict
can clear the goal as failed; a no-progress pause can leave it active. These
outcomes must not be reported as successful external acceptance.

A time or turn clause in goal prose is not a hard limit. Separate print-mode
[CLI limits](https://code.claude.com/docs/en/cli-reference) have their own rules:
queued stream-json messages can start turns with new turn limits, and reliable
subagent spend/teardown accounting has a v2.1.217 boundary. The metadata leaves
universal payload-byte and iteration enforcement unverified. Qualify exact
build, trust, hooks, effective settings and provider behavior before claiming
native goal recovery or bounded execution; no live call was made here.

## OpenAI Codex CLI

`codex/codex-cli` retains the `codex` executable and installer selector.
The [official skills guide](https://learn.chatgpt.com/docs/build-skills)
describes `.agents/skills` discovery along working-directory ancestors toward
the repository root, plus `$HOME/.agents/skills` and additional administrative
and bundled scopes. Repository resolution is therefore an ancestor aggregate,
not a single directory. LocalSetup still writes only its selected target's
recorded adapter; it does not populate or normalize every ancestor or child.

Symlinked skill folders are supported by the documented loader. Same-name
skills are not necessarily collapsed into one winner: multiple entries can
remain available. Do not infer repository-over-user priority or relocate a
custom duplicate to force uniqueness. LocalSetup's shared-owner physical
write deduplication is a separate ownership rule. Optional package-local
`agents/openai.yaml` supplies Codex-specific display, invocation or dependency
metadata; it is not mandatory portable metadata for other hosts.

`CODEX_HOME` selects native Codex configuration and context, while personal
common skills remain under the OS user's home. Changing `CODEX_HOME` does not
isolate those common skills or authorize their disclosure. The filesystem
fixtures use an alternate native profile and verify that both adapter modes
write common roots, preserve profile config/instructions/sessions and nested
custom skills, and retain another client's shared package after Codex detach.
Those fixtures do not launch Codex or prove its effective skill catalog.

```bash
localsetup plan --tools codex --skill-scope both --mode portable
localsetup install --tools codex --skill-scope both --mode portable --apply
```

Inspect the plan before application. Native files under `.codex` or
`CODEX_HOME`, including existing policy, hooks, approvals, authentication,
current goals and sessions, remain outside this skill adapter's ownership.
Framework state locations in the registry are distinct from native runtime
state. Do not add a second `.codex/skills` copy merely for product naming.

The [AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md)
selects the first nonempty `AGENTS.override.md` or `AGENTS.md` at Codex home
(the registry calls this `first-nonempty`). Empty files are skipped. Project discovery
selects at most one override, base or configured fallback file per directory,
then accumulates instructions from project root toward cwd. This does not
establish `.agents/AGENTS.md` as another universal root. Preserve overrides,
fallback choices, existing authored content and managed ownership boundaries.
The dedicated guide describes a default combined 32 KiB instruction budget;
other configuration wording describes a per-file limit. That wording difference
requires exact-build qualification before asserting precise truncation behavior.
No instruction file is reduced or rewritten to fit an assumed limit here.

[Native configuration](https://developers.openai.com/codex/config-basic)
keeps user and project TOML settings separate from portable skill metadata.
Project TOML layers resolve from root toward cwd, with closer values taking
priority, and relative paths resolve against their containing `.codex` directory.
Project trust affects whether project configuration is honored. Model, MCP,
approval and sandbox choices remain native authority; context text does not
replace enforcement. Current documentation is evidence about the product,
not proof of a locally installed build or all Codex application variants.

The [native slash-command reference](https://learn.chatgpt.com/docs/cli/slash-commands)
documents `/goal` to view or set an objective, with edit, pause, resume and clear
controls. Objectives are nonempty and limited to 4,000 characters in that
reference. This is not a verified byte or token budget. Goal continuation does
not expand sandbox, disclosure or unattended-operation authority; native status
is not external controller acceptance. Exact release availability, persistence
across owner exit and accounting require separate qualification. This profile
does not change the current session's goal, global policy or skill projections.

## Cursor CLI and IDE

Cursor IDE retains platform selector `cursor`; fresh repository and personal
installations write only `.agents/skills` and `~/.agents/skills`. The registry
also inventories native `.cursor/skills` and compatibility `.claude/skills` and
`.codex/skills` roots in both scopes. Discovery is aggregate, not first-existing
fallback. Filesystem tests cover both attachment modes, mixed Claude ownership,
native custom neighbors and detach. Historical recorded common/native dual
layouts use the recorded-path preservation rules above; this change does not
remove or relocate an existing adapter.

Cursor Agent CLI remains a separate, non-exported catalog profile. Its command
candidates are `agent` and the distribution's `cursor-agent` alias. The generic
name `agent` alone does not identify a vendor: verify the resolved executable
and selected distribution before use. No CLI install selector or effective host
qualification is inferred from the IDE selector. Native client startup,
authentication, configuration repair and updates are outside adapter operations.

[Cursor skills documentation](https://cursor.com/docs/skills) describes native,
common and compatibility discovery roots, nested skill directories and skill
metadata. An inspected CLI distribution statically follows external directory
symlinks and coalesces some equivalent lexical paths across root conventions.
That observation is neither an IDE guarantee nor a portable duplicate-name or
same-realpath deduplication contract. Frontmatter identity, resource loading,
nested discovery and the final effective list need selected-host evidence.

Before qualifying a host, inventory each existing project and personal discovery
root above. Record each package's relative path, declared name, physical target
and logical installation owners; include nested packages and custom entries.
Group repeated identities, distinguishing old Cursor aliases, another client's
native projection and custom content. Report unresolved groups in the host
qualification evidence. Do not infer safety from equal bytes or shared targets,
and do not delete, relocate or suppress another owner's adapter to resolve a
report. Mixed-client filesystem installation remains supported; unresolved
duplicates leave effective Cursor catalog selection unqualified. Cursor's own
fresh writer creates no additional native alias. This boundary avoids imposing
a universal duplicate rejection rule on otherwise valid mixed-client layouts.

[CLI configuration](https://cursor.com/docs/cli/reference/configuration) separates
project `.cursor/cli.json` permissions from global `~/.cursor/cli-config.json`.
The project file is not a general settings overlay. Documented
`CURSOR_CONFIG_DIR` and XDG configuration overrides are not established skill-root
overrides; simultaneous override precedence remains unqualified. Preserve
native configuration, credentials, sessions and databases. CLI permissions
include Shell, Read and Write controls with deny precedence; do not transfer
those assumptions to IDE settings or LocalSetup authority.

[Rules](https://cursor.com/docs/rules) use `.cursor/rules/*.mdc`; a plain Markdown
file there is not an equivalent rule. IDE User Rules remain a manual settings
surface. CLI `AGENTS.md`, `CLAUDE.md` and rules are multiple context sources;
their inventory does not establish exclusive fallback or skill precedence.
Local personal skills do not imply availability in SSH workspaces, cloud agents
or remote workers. Neither variant has a qualified native goal/budget contract.

Example of an explicit filesystem plan, using an already selected installation:

```bash
localsetup plan --tools cursor --skill-scope both
```

Review the plan's targets and existing ownership before applying through the
normal installer. Host qualification must separately verify selected artifact
identity, the effective skill list, resource access and duplicate behavior for
the actual CLI or IDE version; filesystem success does not certify these.

## Google Gemini CLI

Selector `gemini-cli` installs common repository and personal skill adapters at
`.agents/skills` and `~/.agents/skills`, using symlink or portable mode. It
identifies Google Gemini CLI (`gemini`, package `@google/gemini-cli`), separately
from Antigravity and Gemini Code Assist. Installation and lifecycle tests
qualify filesystem behavior; native listing, activation and resource execution
remain host-not-run. Adapter operations do not install or start Gemini CLI,
authenticate, change trust or grant tool permissions.

The [v0.58.0 skill manager](https://github.com/google-gemini/gemini-cli/blob/ac9431c9e2290d68af31a77614ff2fddb2391ca3/packages/core/src/skills/skillManager.ts)
aggregates built-in, extension, user and trusted-workspace sources. Workspace
overrides user; within either scope common `.agents/skills` overrides native
`.gemini/skills` for an exact matching name. This is per-name merging, not
first-existing-directory fallback. Lookup and disabled-name checks are
case-insensitive while merge keys are exact-case; avoid case-variant identities.
A preserved native skill can be shadowed by a common skill of the same name.
Preserving its bytes does not prove it remains effective.

The [loader](https://github.com/google-gemini/gemini-cli/blob/ac9431c9e2290d68af31a77614ff2fddb2391ca3/packages/core/src/skills/skillLoader.ts)
scans a root skill and one level of package directories, not arbitrary nested
category trees. External package directory symlinks are also an intended native
linking workflow. These source contracts do not certify resource execution or
host sandbox behavior. Native `skills install`, `link` and `uninstall` target
`.gemini/skills`; do not use them to manage shared LocalSetup entries.

`GEMINI_CLI_HOME` is a home-prefix override, not a direct `.gemini` path.
[Native path resolution](https://github.com/google-gemini/gemini-cli/blob/ac9431c9e2290d68af31a77614ff2fddb2391ca3/packages/core/src/utils/paths.ts)
places **both** personal `.agents` and `.gemini` below that prefix. Unset or
empty values use the native default. LocalSetup accepts an unset/empty override
or an absolute override resolving to the supplied home; relative or different
homes fail before personal writes. Diagnostics do not disclose the override
value. Repository-only placement remains available. A package refresh selected
through another client also checks recorded Gemini personal ownership. Verify
reports an incompatible home; personal/combined repair refuses affected writes.
Detach remains available and does not relocate native configuration or sessions.

Default context is `GEMINI.md`; `context.fileName` can explicitly select other
filenames. Do not assume automatic `AGENTS.md` loading. Preserve native context,
`.gemini/settings.json`, trust, credentials, history, extensions and hooks.
System settings/defaults and trust-file overrides are separate native settings;
the home check does not implement their resolver or authorize changing them.

[Skill configuration](https://geminicli.com/docs/cli/skills/) can disable skills,
and untrusted workspaces do not participate in workspace discovery. Copying
packages neither activates them nor satisfies native consent. A later authorized
host qualification should inspect `gemini skills list --all`, confirm
common/native and workspace/user precedence, and separately exercise harmless
activation and resource loading. No standalone native goal/budget contract is
claimed.

Example plan for an already selected CLI installation:

```bash
localsetup plan --tools gemini-cli --skill-scope both
```

Review planned ownership and the effective native home before applying through
the normal installer. Local personal placement does not establish availability
in a remote process, container or cloud environment.

## Kilo CLI source loading and filesystem maintenance

Selector `kilo` retains its existing command and default attachment mode.
Fresh writes prefer `.agents/skills` and `~/.agents/skills`; native `.kilo`
and Claude compatibility roots remain discovery inventory. Native configuration
directories scan both `skill` and `skills` recursively: project/home `.kilo` and
`.kilocode`, the XDG Kilo directory, and an explicit `KILO_CONFIG_DIR`. Project
ancestor and primary-worktree discovery remain native behavior; default registry
paths do not resolve these overrides. Recorded native
adapters keep their paths and mode on omitted-selector update. No rule,
configuration, custom skill, mode overlay, credential or session is moved.
This profile describes the current Kilo CLI/server, not legacy IDE extensions.

The [v7.5.15 loader](https://github.com/Kilo-Org/kilocode/blob/e0ef9096391ebffba8560875665a2d7249ac6dc5/packages/opencode/src/skill/index.ts)
scans recursively and follows symlinks, but project common and ordinary native
sources are untrusted. Its
[source guard](https://github.com/Kilo-Org/kilocode/blob/e0ef9096391ebffba8560875665a2d7249ac6dc5/packages/opencode/src/kilocode/config/variable.ts)
checks the opened file's resolved target against project scope. A project
`SKILL.md` symlink resolving to an external package store is **unsupported for
loading by this source version**, even though LocalSetup can maintain that
filesystem entry. Returning to the native directory does not avoid the guard.

`verify` preserves its filesystem result and adds `native_loading` evidence.
An external project source produces `unsupported-project-source` and a warning
under the recorded `ordinary-untrusted-project-root` policy basis. This does not
resolve explicit native trust configuration; effective configuration remains
unqualified.
`source-contained` means the inspected source paths resolve inside the project;
it is a snapshot, not a security boundary, host execution test or resource
qualification. Missing or ambiguous evidence remains `unqualified`.
Every result keeps `host_verified: false`. Native environment override names
are reported without their values; LocalSetup does not parse effective native
configuration or infer that an arbitrary environment string is a boolean.

Portable materialization keeps package files and resources inside the project:

```bash
localsetup plan --tools kilo --mode portable --skill-scope both
```

For an existing installation, preserve recorded selection and paths while
reviewing an explicit mode change:

```bash
localsetup plan --target-directory <repo> --mode portable
localsetup update --target-directory <repo> --mode portable
```

The second command applies the reviewed mode change through normal ownership,
backup and recovery controls. Do not change `KILO_CONFIG_DIR`, native trust or
approval controls to make an external project link load. Historical symlink
layouts remain inspectable, updateable and detachable as filesystem state;
they are not thereby certified for native loading. Repository-only automatic
repair of an old nonpreferred layout follows the recorded-path preservation
rules above.

Personal trust is separate: native loading considers the effective home, active
project and actual target. A home link into a project does not confer home
trust on project content. `KILO_TEST_HOME` is a real runtime home override
despite its test-oriented name; XDG configuration and `KILO_CONFIG_DIR` are
separate. `KILO_DISABLE_EXTERNAL_SKILLS` disables common and Claude discovery.
Claude-specific disable flags affect Claude compatibility only. Keep native
configuration and these switches unchanged; qualify the selected host layout
before relying on loading or activation.

Discovery aggregates sources with duplicate warnings and later replacement.
Native/config directories can override common names; this is not a universal
common-wins rule. JSON/JSONC configuration also merges in layers. Explicit
`skills.paths` and remote URLs are additional native mechanisms, not automatic
common-root equivalents. Default project instructions choose the first filename
with ancestor matches among `AGENTS.md`, compatible `CLAUDE.md` and deprecated
`CONTEXT.md`. Global context uses the explicit configuration directory when set, otherwise
the normal XDG Kilo configuration root, then compatible Claude context. The
override replaces the normal root; it does not add a default-XDG fallback. Flags and
explicit instruction patterns can change that behavior.

Preserve the `AGENTS.md` bridge and top-level native `mcp` configuration.
Embedded shell expansion, activation, permissions, resources and goal/budget
behavior remain native and unqualified. No Kilo process, trust initializer,
native skill-management command or provider call is part of adapter operations.

## Oh My Pi (OMP)

Selector `omp-cli` writes `.agents/skills` and `~/.agents/skills` directory
packages. Both symlink and portable modes have filesystem lifecycle coverage;
installed OMP loading and activation remain unverified. OMP, Pi and LSCli are
separate runtimes with no implied extension, permission or session compatibility.

```bash
localsetup plan --tools omp-cli --skill-scope both --skills ls-context
localsetup install --tools omp-cli --skill-scope both --skills ls-context --apply
localsetup verify
```

The [pinned v18.1.11 common provider](https://github.com/can1357/oh-my-pi/blob/e3106be68f778635da3a17106835ce2e0e6992af/packages/coding-agent/src/discovery/agents.ts)
scans one package level below `.agent/skills` and `.agents/skills`; directory
symlinks are accepted by its loader. Keep resources inside their skill package;
filesystem readability does not certify native `skill://` activation or tool
permissions. Nested taxonomy is not a supported common discovery layout.
Native `.omp` skills outrank the common provider. Within that provider, nearer
project roots precede farther roots, project precedes personal, and singular
`.agent` precedes plural `.agents`. Custom directories can replace default
same-name results. Inspect winning origins before relying on a selected host;
LocalSetup preserves custom packages and does not remove collisions.

`skills.enableAgentsUser` and `skills.enableAgentsProject` independently control
common discovery. `skills.enabled`, disabled providers/extensions, ignored names
and include filters can suppress packages. LocalSetup does not toggle these
settings or enable unrelated foreign providers. Native profile selection
(`--profile`, `OMP_PROFILE`, legacy `PI_PROFILE`) and native directory overrides
(`PI_CONFIG_DIR`, `PI_CODING_AGENT_DIR`) do not relocate the common OS-home roots.
Their effects on native settings and state differ; do not derive all native
locations from a single directory. WSL native discovery can additionally probe
Windows home; this adapter only writes its explicitly selected repository and
personal ownership targets and does not perform those probes.

[Context and configuration](https://github.com/can1357/oh-my-pi/blob/e3106be68f778635da3a17106835ce2e0e6992af/docs/context-files.md)
remain native domains: `RULES.md` is sticky rule content, while `AGENTS.md` is
session context. They are not fallback alternatives. The nearest nonempty
ancestor `.omp` can stop native context discovery even when the desired file is
absent. Other context providers have separate priority and shadowing behavior;
the registry records inventory, not a universal concatenation order.

Global settings choose the first present `config.yml` or `config.yaml`, then
combine project settings, `PI_CONFIG_FILES`, repeated `--config` overlays and
runtime overrides. Project native settings include `settings.json` and
`config.yml`. Preserve native profiles, config, MCP files, hooks, commands,
plugins, credentials, sessions and databases. Startup may migrate or quarantine
native data, so it is not used as a read-only adapter check. The native documented
`yolo` approval default is not authority for automated probes. LocalSetup does
not start OMP or change approvals. Earlier `/goal`, `/guided-goal` and `/loop`
registry claims are not re-certified; command lifecycle and limits remain
unverified. Use official installation guidance with a verified selected artifact
before separately qualifying a host.

## OpenClaw workspace and default-state integration

Selector `openclaw` prefers `<workspace>/.agents/skills` and personal
`~/.agents/skills`. Select the intended agent's resolved workspace explicitly;
placing skills in a repository does not associate that repository with a gateway
agent. Existing recorded common/native personal adapters retain their paths on
omitted-selector updates. No automatic migration or native configuration change
occurs. Framework state uses the separate roots in [CLIENT_STATE.md](CLIENT_STATE.md#openclaw-state-ownership-correction).

```bash
localsetup plan --target-directory WORKSPACE --tools openclaw --skill-scope both --mode portable --skills ls-context
localsetup install --target-directory WORKSPACE --tools openclaw --skill-scope both --mode portable --skills ls-context --apply
localsetup verify --target-directory WORKSPACE
```

The [pinned v2026.9.2 loader](https://github.com/openclaw/openclaw/blob/3928bad9badfcb6c7d140530435e806fb8092190/src/skills/loading/skill-root-discovery.ts)
requires project skill targets inside their configured skill root unless native
configuration explicitly allows another target. External package-store symlinks
therefore do not satisfy the ordinary policy. LocalSetup preserves filesystem
maintenance and reports `native_loading: unsupported-project-source`; it does
not add `skills.load.allowSymlinkTargets` allowances. `source-contained` is only
a path snapshot under the ordinary configured-root policy, not proof of agent
association, effective configuration, sandbox copying or resource activation.
Use a reviewed portable plan for contained workspace packages. Keep resources
inside their package; personal directory symlinks have different native rules,
including containment of `SKILL.md` within the resolved package directory.

Personal common discovery is conditional on the default-state profile. The
static write prerequisite accepts an absent/blank `OPENCLAW_STATE_DIR` or an
absolute override resolving to the effective native home's `.openclaw` directory.
Other or ambiguous state overrides require profile qualification before affected
personal writes, including shared-package refresh through another client.
Detach remains available. This deliberately bounded predicate is not a complete
native resolver: CLI profiles, legacy paths, effective settings and OS-home
mapping still require qualification. `OPENCLAW_HOME` affects native state home;
it does not relocate common personal skills, which use the native OS-home
resolver. Explicit `OPENCLAW_CONFIG_PATH` selects native configuration separately.
LocalSetup neither edits these values nor copies skills into an isolated profile
as a fallback.

Discovery aggregates sources. Workspace `skills` overrides workspace
`.agents/skills`, followed by personal common, managed state-directory skills,
bundled and additional sources. Preserve higher-priority custom packages and
inspect winning names in the selected host. Agent filters, required binaries,
environment checks and sandbox synchronization can further limit availability.
Filesystem verification does not certify these native conditions.

Keep `AGENTS.md`, `SOUL.md`, identity/user guidance and memory in their native
workspace roles; the workspace is not itself a sandbox. Native config,
credentials, SQLite databases and companions, sessions and delegated harness
state remain separately owned. No gateway startup, authentication or provider
call is part of adapter operations.

[Native goals](https://github.com/openclaw/openclaw/blob/3928bad9badfcb6c7d140530435e806fb8092190/docs/tools/goal.md)
are durable session objectives. Documented operations include status, start,
edit, pause, resume, complete, block and clear; a second goal requires clearing
the existing one, and `/new` or `/reset` clears it. Optional positive token
budgets are tool parameters, not a `/goal start` flag or billing cap. A resumed
budget-limited goal starts a new accounting window. This is not authority for
background scheduling. Delegated Codex goals retain native Codex ownership.
Host loading, resources and delegated runtime behavior remain unverified.

## OpenCode collision inventory primitive

`ls.core.opencode_collisions.conflicting_sources` inventories caller-selected
skill roots before native loading. It compares exact frontmatter names with
explicit intended metadata-file identities; it does not predict the winning
source in OpenCode's concurrent loader. Distinct files with identical bytes
remain distinct origins. Directory aliases of the same resolved metadata file
are one identity. The caller must supply the complete applicable roots and
intended destinations, enforce its native-configuration boundary, and turn
inventory errors into a refusal rather than accepting partial results.

The recursive scan shares budgets of 4,096 directory entries and 65,536 total
entries across roots, and scans each resolved directory once. Ordinary package
documents consume only the total-entry budget. Discovery continues below a
package's `SKILL.md` so nested conflicting identities remain visible. It refuses cycles,
depth beyond 32 directories, unresolved links and unreadable or malformed
metadata. The existing safe reader accepts one string `name` in frontmatter
bounded to 16 KiB and refuses metadata-file symlinks. These are conservative
LocalSetup inventory limits, not claimed native OpenCode parser limits.
Directory symlinks are traversed; no skill body is executed or sent to a
provider. Root enumeration, installation hooks and native host qualification are
separate integration responsibilities; this primitive alone changes no adapter
path, install decision or native configuration.

### OpenCode installation inventory checks

The [stable v1.18.29 loader](https://github.com/anomalyco/opencode/blob/16747470f976aca3d362ad730bcd3fe82ecc2c9a/packages/opencode/src/skill/index.ts)
parses matches concurrently and overwrites duplicate names. Its lexical-path
deduplication does not establish deterministic priority for distinct sources.
These checks apply to that stable profile, not the separate V2 beta.

Installation, personal repair, combined repair and verification check selected
OpenCode skill identities in bounded local roots. Shared package refreshes also
check affected recorded OpenCode owners when another client initiated the
operation. The check preserves conflicts and returns `opencode_skill_inventory`;
it does not pick a native winner or modify the conflicting skill.

Checked roots include the selected target and its Git worktree ancestors when
available, personal common/Claude roots, native `.opencode/skill` and `skills`,
and the default XDG OpenCode roots. An absolute `OPENCODE_CONFIG_DIR` contributes
its singular and plural skill roots. Without Git context, a target beneath the selected home is checked up to that
home; other targets are checked only at the target, plus personal roots; this is not a claim about discovery from
other working directories. Native configuration can add `skills.paths` and
`skills.urls`; these are not parsed or downloaded by the static check and need
separate effective-host qualification.

The bounded environment contract accepts an unset or literal `false`
`OPENCODE_DISABLE_EXTERNAL_SKILLS`, the selected default home (or equal absolute
`OPENCODE_TEST_HOME`), and default XDG configuration (or its equal absolute
path). Other values require qualification before affected writes. This is a
conservative LocalSetup boundary, not an emulation of native boolean parsing or
an assertion that every rejected override is unsupported upstream. No setting
is changed automatically. Claude-specific flags do not establish that common
skills are disabled. Native agent permissions and filters remain authoritative.

Planned destinations and canonical managed sources are explicit accepted
identities. Already recorded portable counterparts are accepted only after
existing ownership and package-content checks; this does not establish native
realpath deduplication or a deterministic lexical origin. Unmanaged copies with
identical bytes remain distinct conflicting origins. Current preferred paths
are unchanged by this check; changing defaults and qualifying historical-path
updates are separate profile operations.

### OpenCode preferred paths and native boundaries

Selector `opencode` now prefers `.agents/skills` and `~/.agents/skills` for new
installations. Omitted-selector updates preserve recorded native repository
`.opencode/skills` and personal `~/.config/opencode/skills` paths and modes;
custom content stays in place. Repository repair of a nonpreferred historical
layout follows the recorded-path preservation procedure above. Changing the
fresh default does not authorize migration of existing directories.

```bash
localsetup plan --tools opencode --skill-scope both --skills ls-context
localsetup install --tools opencode --skill-scope both --skills ls-context --apply
localsetup verify
```

Both symlink and portable package layouts have filesystem lifecycle coverage.
The pinned stable source follows directory symlinks and reads Markdown without
Kilo's project-source containment guard. That difference is source evidence,
not proof of installed resource activation or permissions. Keep package
references and assets together; the native skill tool controls content access.
Native singular/plural skill roots under project `.opencode`, home `.opencode`
and XDG configuration remain discovery inventory. Preferred common roots do not
imply deterministic duplicate-name priority; the bounded collision checks above
apply before affected mutations.

[Configuration](https://github.com/anomalyco/opencode/blob/16747470f976aca3d362ad730bcd3fe82ecc2c9a/packages/opencode/src/config/config.ts)
merges layers and supports JSONC. Preserve project/global settings, explicit
configuration, plugins, managed settings and separate TUI configuration. Do not
generate a replacement config file merely to add skill discovery. Native state,
auth, sessions and databases remain separate from LocalSetup bookkeeping.

[Instructions](https://github.com/anomalyco/opencode/blob/16747470f976aca3d362ad730bcd3fe82ecc2c9a/packages/opencode/src/session/instruction.ts)
use conditional filename fallback: project `AGENTS.md`, compatible `CLAUDE.md`,
then deprecated `CONTEXT.md`, stopping at the first filename family with ancestor
matches. Global context first selects the effective config directory's
`AGENTS.md`, then compatible personal Claude context. Native flags and explicit
instruction paths can alter this behavior. This does not make skills or config
files first-existing systems. Keep the `AGENTS.md` bridge and all custom native
policy content intact.

Use official stable-v1 installation guidance with a verified selected artifact.
No native installation, startup, authentication or provider call occurs during
adapter operations. OpenCode V2 beta uses a separate executable and is outside
this profile. No native `/goal` lifecycle is certified, and filesystem support
is not a claim of native host loading, activation or sandbox qualification.

## LSCli runtime-only catalog entry

Family/variant `lscli/lscli` describes the LocalSetup-owned runtime with command
`lscli`; `localsetup agent` dispatches the same CLI. The Python distribution
remains `localsetup`. This entry has no automatic `--tools` adapter projection.
It also creates no generic `localsetup state` root: LSCli already owns session,
lease, snapshot and protected-runtime locations through its runtime APIs.

LSCli loads explicit workspace-relative files through repeatable `--context`
and `--skill` arguments. A selected skill must name `SKILL.md`; there is no
common-directory scan, personal skill discovery or native-provider precedence.
Use ordinary materialized workspace files, for example
`--skill skills/example/SKILL.md`, together with the required run arguments and
current read and separate disclosure grants in [LSCLI.md](LSCLI.md). Selection
alone grants no resource-directory access or script execution. The broker
protects `.agents` and other policy/private directories and refuses symlink
traversal; common symlink adapters are therefore not a usable shortcut.

Explicit nested context refresh reads `AGENTS.md` from the workspace root
through the selected nested directory, under current read/disclosure grants.
It is not automatic startup ancestor discovery and does not imply Claude-file
fallback. Selected context and skill snapshots remain conversation content;
permissions stay outside the saved text and must be re-evaluated on resume.

Provider profiles default to
`~/.local/share/localsetup/config/lscli/profiles.json`; runtime state and protected
runtimes default to `~/.local/share/localsetup/state/lscli` and
`~/.local/share/localsetup/runtimes/lscli`. Explicit runtime CLI overrides retain
their documented checks. These roots do not follow native third-party client
configuration or implicit XDG relocation. Setup manages the protected runtime;
this catalog entry does not install providers, configure credentials, discover
skills, or certify that a given installed release is ready to execute.

There is no standalone LSCli goal command. Task/session identity, bounded runs,
steering, approvals, cancellation, branching and compaction retain the runtime
contracts in [LSCLI.md](LSCLI.md). Catalog registration is not additional host
qualification or a promise of Pi/OMP extension compatibility.

For repository-owned implementations, research evidence may use
`kind: repository_source` with a public `reference` under `ls/` or
`pyproject.toml`. External sources retain their existing HTTPS `url` contract.
The repository source is versioned by the consuming tree; it is not an invented
upstream release, a private maintenance pointer, or proof of installed behavior.
