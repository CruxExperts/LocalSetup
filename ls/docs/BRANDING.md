---
status: ACTIVE
version: 4.84
owner_skill: ls-script-docs-authoring
---

# Product positioning, voice, and branding

Introduce the framework as **LocalSetup (LS)**. Use **LocalSetup** for product
display text and **LSCli** for the integrated CLI display name. Its command is
`lscli`. The existing framework command and Python distribution remain
`localsetup`; neither `LS` nor `ls` is a new command alias.

This contract changes display text, not compatibility identifiers. Preserve
existing imports, filenames, directories, URLs, environment variables, persisted
keys, package names and protocol values. Preserve upstream attribution and
immutable historical evidence. Update owned generator inputs before regenerating
their output. Managed policy text must be changed through its owning source and
generator, never by editing a rendered tail.

## Product positioning and voice

The lead message is **See the work. Keep control.** Explain LocalSetup as
**the harness for your agent harness**: a shared operating layer that brings
project context, reusable capabilities, visible operations, and explicit control
boundaries to supported AI tools. The three pillars are **visibility, security,
and privacy**. Lead with concrete operator and business benefits; capabilities
and integrations substantiate those benefits.

The public [narrative](WHY_LOCALSETUP.md) connects this positioning to the
engineering and service proposition. The GitHub homepage introduces the system;
the narrative provides the fuller explanation, founder background, examples,
and implementation boundaries. Small businesses need useful service: understand
their work, choose a manageable improvement, integrate it properly, and measure
the result. Do not promise a universal savings percentage or assume every
problem needs AI.

Use a pragmatic, experienced voice. AI is a tool and a force multiplier for
engineering judgment. Avoid both doom rhetoric and inflated transformation
claims. The supplied founder story includes 35 years of hands-on engineering,
Netscape-era HTML websites in 1995, and more than nine years owning a web hosting
company before selling it after growing from a bedroom operation to an office
with employees. Keep those facts accurate; do not invent certifications,
customer names, market leadership, or financial outcomes.

Visibility means an operator can inspect managed terminal work outside the
conversation. It does not mean every external client action is monitored.
OpenPGP signing, encryption, and trust are capabilities applied to selected
workflows; they do not encrypt every file or all model traffic. Scheduled checks
require explicit activation. External clients retain their own permissions.
Use the exact boundaries in the owning runtime documentation.

Describe persistent context as reviewable files that can be reused across
sessions and supported clients. Do not promise permanent model memory, invisible
integration, automatic compliance, or identical behavior across providers.
Efficiency means less repeated setup and reuse of maintained procedures unless
measured evidence supports a more specific claim.

Current first-party source is **source-available**, free for noncommercial use
under its stated terms, with separate commercial licensing available. Do not
call the new noncommercial grant open source. Preserve earlier MIT permissions
and third-party terms. GitHub licensing copy stays concise; detailed offers,
pricing, and support agreements belong to the separate company website work.
See [licensing](LICENSING.md).

Keep language plain, connected, and specific. The [homepage README](../../README.md)
is the short-form reference. Preserve generated facts and historical release
blocks. Keep detailed inventories and operational contracts in linked guides.
Describe ecosystem tools through their actual scope and their own licenses;
do not imply they are bundled requirements or automatically covered by this
repository's terms.

## Runtime identity

Installer help, wizard titles, core diagnostics, package descriptions and new
release titles use the canonical display spelling. The wizard uses a plain
`LocalSetup installer` title so its terminal banner has the same spelling.
Diagnostic prose may change case; command prefixes, JSON keys, error codes,
environment-variable names and recorded paths retain their existing contracts.
Automation should use those structured values instead of matching display prose.

Runtime wheel/distribution names, sandbox paths and heartbeat launcher/profile
keys keep their literal lowercase identifiers. CLI examples likewise preserve
the executable spelling. Classify ambiguous occurrences with exact source-line
exceptions; do not rename these values or exempt a whole runtime module.

The Agent Q processed-mailbox identifier and existing non-model skill-index and
skill-validation user-agent values are compatibility identifiers. Their retained
spelling does not define the model-request identity below.

`ls.core.branding` exports `PRODUCT_NAME`, `PRODUCT_ABBREVIATION`, `CLI_NAME` and
`CLI_COMMAND`. Its `user_agent()` constructs the exact model-request identity
`LocalSetup/<framework_version()>` through the existing runtime version resolver.
The helper has no SDK dependency. Calling the helper alone does not prove the
header reached a provider: transport integration must enforce it at final send,
and request-capture tests must verify actual bytes for coding, compaction and
direct completion.

## Inventory and report mode

Run from the source checkout:

```bash
localsetup branding validate
```

The JSON report inventories Git-tracked and nonignored new files, hashes regular
files, records visual assets, and classifies each product-name occurrence.
Symlinks are inventoried without reading their targets. Unreadable paths are
findings. Private ignored state is outside this public repository scan; review
current private operational guidance separately without publishing it.

Classification is conservative: canonical display names and recognizable
technical tokens pass automatically. Ambiguous text remains an
`unclassified_reference` for source-owner review. The report includes path, line,
column, matched token and exact-line hash rather than copying whole source lines.
It does not rewrite files or infer that a test, quotation, or old document can be
ignored merely from its directory name.

Report mode exits zero when scanning succeeds, even if `ok` is false. This allows
inventory before existing references are corrected; it is not a compliance gate.
`--strict` exits 1 for findings. Policy/input errors exit 2 in either mode. Strict
enforcement will be connected to release and PR gates after current owned
surfaces have been remediated and visually reviewed.

## Exact exceptions

`ls/config/branding.json` owns schema version 1, `exceptions`, `visual_reviews` and
`binary_reviews`.
Each text exception has a repository-relative `path`, `line_sha256`, exact `token`,
positive `count`, `kind`, and a nonempty `reason`. Allowed kinds are
`compatibility_identifier`, `upstream_attribution`, `historical_evidence`, and
`negative_test`. The line hash is SHA-256 of UTF-8 text without its line terminator.
The count is the number of matching token occurrences on identical lines in that
file. An edited line, extra occurrence, removed file, or now-canonical reference
invalidates the exception and produces `stale_exception`.

Use exceptions only for evidence that must retain exact spelling. Correct owned
display text instead of suppressing it. Do not add broad directory exclusions,
wildcard exceptions, or an exception merely to make a release pass.

## Visual and accessibility evidence

Every inventoried image or PDF requires a hash-bound `visual_reviews` entry with
`path`, `sha256`, `reviewed_text`, `accessibility_evidence`, `reviewer`, and
`reviewed_at`. Inspect the actual rendered asset: OCR and text search do not prove
embedded branding. Record either the reviewed embedded text or that the asset
has no brand-bearing text, and identify the matching accessibility text in its
consumer. Review assets without an unrelated redesign. A changed hash or removed
asset invalidates its approval; a new asset requires review.

Unknown binary files produce `binary_classification_required`; recognized image
signatures require visual review even without a normal image extension. A
nonvisual binary may have an exact `binary_reviews` entry with `path`, `sha256`
and a nonempty classification `reason`. Changing or removing that binary makes
the review stale. Do not classify rendered images as nonvisual binary data.

The policy records hash-bound visual reviews of the current asset inventory.
Repository-wide branding compliance remains unproven until the text,
generated output, installed runtime, compatibility, transport, artifact and visual
checks all pass. The validator supports that evidence; it does not substitute
for installed-artifact and visual acceptance.

## Generated display text

Plugin names and descriptions originate in `ls/config/plugin-packs.yaml`; plugin
payload metadata and orientation text are emitted by `ls/core/plugin_packs.py`.
Workflow registry headings originate in `ls/core/docs_artifacts/writers.py`.
Repository-profile guidance is emitted by `ls/core/repo_profiles.py`. Correct
these inputs before running the documentation generators; editing a generated
heading alone will not survive regeneration. Schema titles are display text,
while schema keys, plugin identifiers and emitted filenames remain compatibility
identifiers.

Run both owning generators after committing source changes, preserving the
separate generated-document receipt required by the publishing workflow:

```bash
uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
```

## Current visual assets

The identity package is version **1.2.0**; that version is metadata, never text
inside artwork. The current direction is a bright technical workspace with
cool-white/ice-blue surfaces, navy lettering, electric-blue brackets, and a
visible operational lane alongside the agent-tool foundation. The dusk variant
uses readable blue/navy surfaces rather than near-black space imagery.

`assets/brand/tokens.json` owns the semantic palettes and typography for
code-rendered surfaces. Marketing compositions are reviewed raster plates under
`assets/brand/plates/`, generated with the built-in image tool from the
[production briefs](../../assets/brand/PROMPTS.md). Their provenance binds exact
source hashes. `ls/core/branding/graphics.py` owns export dimensions,
non-cropping resampling, metadata, deterministic diagrams, and the asset ZIP.
Do not replace reviewed plates silently or edit derived exports by hand.

| Asset | Purpose and embedded content |
|---|---|
| `localsetup-readme-hero.png` / `localsetup-readme-hero-dark.png` | Distinct light/dusk 2048×768 heroes: LocalSetup, See the work. Keep control., harness line, visibility/security/privacy, agent tools, live operations, OpenPGP workflows |
| `localsetup-social-preview.png` | 1280×640 landscape campaign preview |
| `localsetup-social-square.png` | 1080×1080 square campaign composition |
| `localsetup-social-wide.png` | 1536×512 wide header composition |
| `localsetup-mark.png` | 512×512 blue-bracket/navy-letter `[LS]` avatar |
| Architecture and installation light/dark pairs | Token-driven readable diagrams with pinned typography |
| `localsetup-logo.png` | Immutable historical release mark; retain its original provenance and license |

Keep all current artwork version-free. Use real light and dusk variants through
GitHub `<picture>` sources and meaningful fallback alt text. The marketing
illustration is conceptual, not a screenshot or evidence of a live operation.
Keep actual commands in selectable text. Review every changed raster, including
small labels, and update its hash-bound visual evidence in `ls/config/branding.json`.
The asset inventory `assets/README.md` is generated through documentation alignment.

The bracket mark remains recognizable; text is navy on light artwork and white
on dusk artwork. Use generous spacing and clear contrast rather than dense
starfields or security-symbol decoration. The native terminal's saved theme has
its own owning runtime configuration; updating brand artwork does not silently
change a running operator session.

## Typography, rights, and exports

JetBrains Mono Regular and SemiBold remain pinned by exact hashes and the
original SIL Open Font License in `assets/brand/font-lock.json`. Code-rendered
labels use those fonts. The generated marketing plate lettering preserves the
established technical identity but is not a verified font rendering. Do not
attribute model-generated lettering to an unverified font family.

All current visual assets carry the exact copyright, company URL, and private
asset notice in supported metadata and manifests. These asset terms are separate
from the software license; JetBrains font rights and the historical release
mark remain intact. See [asset rights](../../assets/brand/ASSET_LICENSE.md).

Render and verify with:

```bash
localsetup branding render
localsetup branding render --check
```

Use `--social-output PATH` for an additional preview copy and
`--pack-output PATH` for a deterministic ZIP containing the ten exports, token
file, manifest, rights notice, and production briefs. Use a descriptive filename
such as `assets/localsetup-brand-1.2.0.zip`. The generated
[manifest](../../assets/brand/manifest.json) records dimensions, byte counts, and
hashes. Uploading social artwork or modifying GitHub settings is a separate
publication action.

AstroSite and the commercial website are separate projects. They can consume
this narrative and token/asset contract, but repository work does not constitute
a website deployment or a pricing publication.

## Technical syntax recognition

The scanner recognizes uppercase environment identifiers independently of shell
expansion punctuation, and recognizes relative resource filenames with known
extensions. Lowercase `localsetup` and `lscli` at the executable position in a
simple shell-labelled Markdown fence are command identifiers. Blocks containing
here-documents, line continuations or multiline quotes remain conservative;
console transcripts require a prompt prefix for automatic command recognition.
This does not exempt the
rest of the line or block: comments, echoed prose, unlabelled prose and display
separators still require canonical branding or an exact reviewed exception.

## Historical and protocol records

The policy retains exact spelling in dated public audit and release records,
source provenance ledgers, and established mail-header and non-model HTTP
user-agent identifiers. Each exception binds the original line hash, token and
occurrence count. A changed record or an additional occurrence requires review;
these entries do not exempt a directory or future files.

Exception `token` values are metadata, not display prose. The scanner recognizes
that distinction only in the validated owning `ls/config/branding.json`, serialized
with `json.dumps(policy, indent=2)` and a final newline. It still checks every
rationale and visual-review description. Arbitrary JSON token fields receive no
such exemption; noncanonical policy formatting is scanned conservatively.

Legacy skill aliases are recognized from regular inventoried
`ls/skills/ls-*/SKILL.md` sources through the framework's alias mapping. Exact
aliases inside quotes or backticks are compatibility identifiers, including in
generated migration maps. A symlink source, unknown alias, plain-prose occurrence
or adjacent incorrect display reference does not receive that classification.

## Enforced repository checks

PR validation, documentation synchronization, documentation drift checks and the
release workflow run `validate_branding.py --repo-root . --strict`. Release
validation checks branding before building public artifacts. Run the same command
locally after source changes and again after regeneration. Keep the policy's
exact compatibility and test-fixture records aligned with the reviewed source;
do not regenerate exceptions merely to clear a failure.

The existing inventory includes synthetic legacy installer inputs and deliberate
branding-negative tests. These are test cases, not historical audit evidence.
Ordinary fixture prose uses current product spelling. Hook labels and version
presentation use LocalSetup; the executable remains `localsetup`.

A clean repository scan proves the inventoried text classifications and asset
review hashes. It does not prove installed SDK behavior, provider wire headers,
or the contents of a published artifact. Those require the installed and
exact-release checks described above.
