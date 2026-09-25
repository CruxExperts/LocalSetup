---
status: ACTIVE
version: 4.44
owner_skill: ls-docs-organization
---

# Plugin Packs

Plugin packs are a portable distribution surface for existing LocalSetup packs. They do not replace adapter installs; they package selected skills and workflow packages for agent platforms that support plugin-style loading.

The first supported platform is Codex. The canonical source is `ls/config/plugin-packs.yaml`; generated catalogs live in `ls/docs/_generated/plugin-packs.json` and `ls/docs/_generated/plugin-packs.md`.

## Commands

- `localsetup plugin list --platform codex`
- `localsetup plugin plan --platform codex --plugin-packs bootstrap core --output <dir>`
- `localsetup plugin build --platform codex --plugin-packs bootstrap core --output <dir>`
- `localsetup plugin validate --platform codex --path <plugin-or-marketplace-root>`

## Output Shape

Codex builds create a marketplace root with relative plugin paths:

```text
<output>/
|-- marketplace.json
`-- plugins/
    `-- localsetup-bootstrap/
        |-- .codex-plugin/plugin.json
        |-- README.md
        `-- skills/
            |-- ls-context/
            |-- ls-communication-and-tools/
            |-- ls-framework-audit/
            `-- ls-plugin-bootstrap-context/
```

Each generated plugin includes selected skill packages, selected workflow packages, and one generated context skill named `ls-plugin-<source-pack>-context`.

Generated plugin skill directories are derived artifacts. The canonical source remains under `ls/skills/` and `ls/workflows/`. During build, runtime-facing Markdown references to public framework docs are rewritten into package-local files under `references/localsetup/docs/`, and each package receives `references/localsetup/.localsetup-reference-bundle.json` with provenance, copied references, rewrites, exclusions, and validation status.

Do not hand-edit generated plugin packages. Update the source package or framework docs, then rebuild and re-run `localsetup plugin validate`.

## Safety

The generator materializes only packages from `ls/skills/` and `ls/workflows/`. It rejects absolute paths, parent traversal, private maintenance paths, missing context inputs, package symlinks that resolve outside allowed package roots, missing transform manifests, invalid emitted package metadata, and runtime Markdown references that still point at source-checkout docs.

Rebuilding replaces generated plugin directories matching selected plugin pack IDs under `<output>/plugins/`. Keep unrelated files outside those generated plugin directories.

Non-Codex platform metadata is reserved for later work. Do not claim Claude Code, Cursor, OpenCode, Kilo, or OpenClaw plugin pack support until that platform has generator and validator coverage.
