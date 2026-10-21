# AgentLens Output Structure

## Directory Layout

```
.agentlens/
├── INDEX.md              # L0: Global routing table
├── modules/
│   └── {module-slug}/
│       ├── MODULE.md     # L1: Module overview
│       ├── outline.md    # L1: Symbol maps, when generated
│       ├── memory.md     # L1: Warnings and TODOs, when generated
│       └── imports.md    # L1: Dependencies, when generated
└── files/
    └── {file-slug}.md    # L2: Deep docs for complex files, when generated
```

This layout follows the current [upstream README hierarchy](https://github.com/nguyenphutrong/agentlens/blob/e28f9395af4aba1ccb3cf2820bbf0234bd60c360/README.md#L30-L40) and its [output-structure table](https://github.com/nguyenphutrong/agentlens/blob/e28f9395af4aba1ccb3cf2820bbf0234bd60c360/README.md#L264-L273).

## Lifecycle

The `.agentlens/` tree is target-repo documentation produced outside this skill. LocalSetup does not ship or install an AgentLens generator.

Use the tree as follows:

1. Read `.agentlens/INDEX.md` first.
2. Navigate into `modules/` and `files/` for focused context.
3. Treat module sidecars as optional: use the files that the generator produced.
4. When content appears stale, verify against source. Regenerate only with a command documented by the target repo or by an external AgentLens installation.

## File Purposes

### INDEX.md (Always Read First)
- Project name and description
- Complete list of modules with descriptions
- Entry points (main files)
- Hub files (heavily imported)
- High-priority warnings summary

### MODULE.md
- Module purpose and responsibility
- List of all files in the module
- File descriptions and line counts
- Language breakdown

### outline.md
- Symbol maps for large files (>500 lines)
- Functions, classes, structs, enums, traits
- Line numbers for quick navigation
- Visibility (public/private)

### memory.md
- TODO comments
- FIXME and BUG markers
- WARNING and SAFETY notes
- DEPRECATED markers
- Business rules (RULE, POLICY)

### imports.md
- Which files import which
- Internal dependencies within module
- Helps understand coupling

### files/{slug}.md (L2 - Complex Files Only)
- Generated for very complex files
- Detailed symbol documentation
- More context than outline.md

## Provenance Sidecars

Some imported skills or framework workflows may include `_meta.json` files as provenance sidecars. They are not part of the `.agentlens/` output structure and are not consumed by this skill. For `ls-agentlens-codebase-navigation`, any `_meta.json` beside the skill is metadata about the imported skill source only, not runtime configuration.
