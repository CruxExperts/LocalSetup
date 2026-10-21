# Upstream Coverage

The Superpowers upstream inventory is now tracked in [upstream/manifest.yaml](./upstream/manifest.yaml).

Summary:

- Source: `https://github.com/obra/superpowers`
- Release/ref: `v6.1.1`
- Commit: `d884ae04edebef577e82ff7c4e143debd0bbec99`
- License: `MIT`
- Inventoried upstream `SKILL.md` files: `14`
- Active LocalSetup imports from this wave: `ls-requesting-code-review`
- Inert upstream source copies: `references/upstream/*/*.source.md`

Upstream scripts, hooks, plugins, tests, package metadata, webserver tooling, and symlinks are intentionally excluded from active LocalSetup runtime surfaces.

## Selective archive boundary

The manifest preserves upstream `SKILL.md` source copies and the license, not a
complete checkout or the transitive closure of their relative links. Source
snapshots retain their original bytes and recorded hashes. Their links can name
auxiliary files that were intentionally not imported.

For example, the subagent-driven-development snapshot refers to
`skills/requesting-code-review/code-reviewer.md` at the pinned commit above.
That auxiliary prompt is outside this archive's imported set. It is not a missing
LocalSetup runtime component: active review uses `ls-requesting-code-review` and
the router's LocalSetup-native workflow destinations.

The strict active-document audit excludes these `*.source.md` snapshots while
still checking authored coverage notes and ordinary references. Archive integrity
means matching the source and license hashes in the manifest; it does not assert
that every original upstream relative link resolves locally.
