---
status: ACTIVE
version: 5.7
owner_skill: ls-script-and-docs-quality
---

# Product naming and branding validation

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

## Runtime identity

Installer help, wizard titles, core diagnostics, package descriptions and new
release titles use the canonical display spelling. The wizard uses a plain
`LocalSetup installer` title so its terminal banner has the same spelling.
Diagnostic prose may change case; command prefixes, JSON keys, error codes,
environment-variable names and recorded paths retain their existing contracts.
Automation should use those structured values instead of matching display prose.

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
uv run --locked python ls/tools/validate_branding.py --repo-root .
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

The README graphics are generated by `ls/tools/render_brand_graphics.py` from
`assets/brand/tokens.json` and the fonts recorded in
`assets/brand/font-lock.json`. The visual package has its own version `1.0.0`;
that is not the LocalSetup framework release version. `assets/README.md` is a
derived inventory maintained by the documentation alignment tools. Acceptance
hashes and accessibility evidence live in `ls/config/branding.json`.

| Asset | Embedded product text | Current use |
|---|---|---|
| `localsetup-readme-hero.png` and `localsetup-readme-hero-dark.png` | `[LS]`, accented L/S in LocalSetup | README introduction |
| `localsetup-architecture.png` and `localsetup-architecture-dark.png` | `[LS]`, accented L/S, LocalSetup CLI | README and framework architecture guidance |
| `localsetup-install-lifecycle.png` and `localsetup-install-lifecycle-dark.png` | `[LS]`, accented L/S | Installation and rollback guidance |
| `localsetup-logo.png` | None | Historical release mark |

The three light paths remain stable fallbacks. Current consumers use GitHub's
`<picture>` theme sources with a descriptive `<img>` fallback. Keep commands in
selectable text outside the images. Review every changed raster, including small
labels, and check its consumer's alt text. The historical release image and note
remain immutable.

## Visual contract for documentation and AstroSite

`assets/brand/tokens.json` owns the approved light and dark palettes and type
scale. The light background/accent are `#FFFFFF`/`#173DA6`; the dark pair are
`#071012`/`#36E4C6`. Use semantic foreground, muted, surface, border, and
status tokens from that file instead of deriving new colors from screenshots.
The mark is the literal, tightly spaced `[LS]`. The wordmark spells
`LocalSetup`, with its **L** and **S** in the active accent color. This visual
treatment does not alter command casing or the technical identifiers above.
The [visual asset rights](../../assets/brand/ASSET_LICENSE.md) distinguish
Crux-owned graphics from JetBrains font software and the repository MIT license.

JetBrains Mono v2.304 Regular (400) and SemiBold (600) are pinned for generated
graphics, with exact hashes and the original SIL Open Font License in the font
lock. The web type stack starts with JetBrains Mono, then Cascadia Mono,
SFMono-Regular, DejaVu Sans Mono, Liberation Mono, Consolas, and monospace.
Disable ligatures for interface and terminal text. Use 400 for body copy and 600
for display text; retain the token type scale and readable content sizing.
The handoff's fallback-rendered DejaVu images are superseded by the pinned
graphics here.

An optional JetBrainsMono Nerd Font glyph profile is suitable for a future
AstroSite terminal or icon treatment. Nerd Fonts publishes its patched fonts
under the SIL Open Font License 1.1; retain the exact font package's license and
upstream attribution when that profile is pinned and distributed. Keep ASCII
labels as the default and a fallback for each optional glyph. Do not require a
Nerd Font to read the README, follow install commands, or understand status.
The upstream [Nerd Fonts license](https://github.com/ryanoasis/nerd-fonts/blob/master/LICENSE)
and [JetBrainsMono package](https://github.com/ryanoasis/nerd-fonts) are the
starting points for the future release-specific pin.

AstroSite is a separate source and deployment project. Its implementer should
consume this token and font contract, use one theme state for all pages, support
light, dark, and system preference, and preserve readable focus and status text
without relying on color alone. Use the same `[LS]`/accented wordmark and derive
social media art from the same visual system. Do not treat the repository's one
static GitHub social preview as a website theme switch. LocalSetup's current
README, documentation images, and generated-image script are the reviewable
GitHub reference; the standalone site has no release or homepage URL here yet.

Render with `uv run --locked python ls/tools/render_brand_graphics.py`; pass
`--check` to verify exact output. The social image is rendered only to a private
`--social-output` path for upload to GitHub settings. Graphics use embedded
copyright and usage metadata; the JetBrains font retains its own license.

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
