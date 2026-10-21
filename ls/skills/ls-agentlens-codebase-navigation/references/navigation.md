# Navigation Patterns

## Pattern 1: Exploring a New Codebase

```
1. Read .agentlens/INDEX.md
   → Get list of all modules
   → Note entry points and hub modules

2. Pick relevant module from INDEX
   → Read modules/{slug}/MODULE.md
   → Understand module purpose and files

3. Need a specific symbol?
   → If modules/{slug}/outline.md exists, use its line map
   → Otherwise search source for the symbol

4. Check for warnings and TODOs first
   → If modules/{slug}/memory.md exists, read it
   → Otherwise search module source for TODO, FIXME, WARNING, SAFETY, and DEPRECATED markers
   → Review the findings before editing
```

## Pattern 2: Finding Where Something Is Defined

```
1. Start with INDEX.md
2. Search for keyword in module descriptions
3. Go to matching MODULE.md
4. If outline.md exists, use it for symbol locations
5. Otherwise search source for the symbol
6. Read only the specific source lines needed
```

## Pattern 3: Understanding Dependencies

```
1. If modules/{slug}/imports.md exists, read it
2. Otherwise inspect imports in the relevant source files
3. See which files import what
4. Understand the dependency graph
5. Navigate to related modules as needed
```

## Pattern 4: Before Modifying Code

```
1. If memory.md exists for the module, read it
2. Otherwise search the module source for these markers
3. Check for:
   - TODO: Pending work
   - FIXME: Known bugs
   - WARNING: Dangerous areas
   - SAFETY: Critical invariants
   - DEPRECATED: Code to avoid
4. Understand the context before changes
```

## Token Efficiency Tips

- **Start with generated maps** in large codebases, then verify the relevant source sections
- **Use `outline.md` when present** to find exact line numbers first; otherwise search source
- **Read only relevant sections** of source code
- **Navigate hierarchically**: INDEX → MODULE → available sidecars → source

### Upstream benchmark boundary

The [upstream AgentLens README benchmark](https://github.com/nguyenphutrong/agentlens/blob/e28f9395af4aba1ccb3cf2820bbf0234bd60c360/README.md#L47-L60) reports one 362K-line PHP/Laravel corpus:

- Raw source: about 3,627,260 tokens.
- All generated AgentLens documentation: 129,850 tokens, reported as 96.4% fewer than raw source.
- Hierarchical navigation with `INDEX.md` plus one module: about 25,580 tokens, 80.3% fewer than reading all generated AgentLens documentation.

These are corpus-specific upstream estimates, not a general performance guarantee. Measure the target repository rather than extrapolating them.

## Staleness and Regeneration

This skill does not provide a regeneration command. If `.agentlens/` appears stale, first verify the claim against source files. Regenerate only when the target repository or an external AgentLens installation documents the exact command to run.

LocalSetup does not include an `agentlens` executable, and this skill should not imply that one is available from the repo or `PATH`.
