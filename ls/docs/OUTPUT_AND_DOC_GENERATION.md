---
status: ACTIVE
version: 4.89
owner_skill: ls-script-docs-authoring
last_updated: "2026-02-20"
---

# Output and doc generation (platform default)

**Purpose:** Default behavior for all generated content (reports, context, user-facing docs, tool output). Applies to framework tooling and to agent-produced markdown. Every user of the platform gets rich, readable output by default.

## Default behavior for any generated docs or output

When the framework or an agent generates markdown, reports, or documentation (for any project, not only this repo):

- **Code and copy-paste:** Use fenced code blocks for commands, snippets, and terminal output. Use a language hint when useful (`bash`, `python`, `text`). Make sections that are meant to be copied clearly scannable (e.g. a short heading like "Run this" or "Copy-paste").
- **Lists:** Use `-` or `*` for unordered lists and `1.` for ordered lists so structure is clear. Break long prose into bullets where it helps.
- **Typography:** Use heading levels for sections, **bold** for labels and important terms, *italic* for secondary emphasis, and inline `code` for paths, commands, and literals.
- **Links:** In-repo references to docs or config (e.g. other `.md` files, `.yaml` indexes) should be markdown links, not bare paths, so they work in GitHub and previews.
- **Glyphs and icons:** Use symbols or icons where they improve scanability (e.g. [OK], [FAIL], checkmarks, bullets). Prefer glyphs that render in common editors and GFM. Avoid relying on glyphs for critical meaning when plain text is clearer.
- **Humanized prose:** Default to natural, specific language. Avoid template-y or inflated phrasing; vary sentence length; be concrete. Use the humanizer skill when refining existing text.

Tooling that writes reports or output (audit, PR review, validation summaries, generated context) must follow this by default. See [TOOLING_POLICY.md](TOOLING_POLICY.md) (Markdown output) for GFM compatibility and script output rules.

## Terminal layout baseline

Human-facing CLI status and progress output must fit an 80-column baseline,
independent of a wider development terminal. Fixed rows occupy 80 visible columns;
this is a row-formatting contract, never a terminal or tmux window-size setting.
The native TUI canvas fills the qualified primary viewer's actual terminal size
within its supported geometry and retained-history limits. A wider terminal keeps
its extra space; CLI format checks must not resize a user's attached canvas.
ANSI styling contributes no width. Reserve suffixes such as ` [100%]` at the right
edge, subtract their width and any prefix from 80, and fill only the remaining
space with activity symbols. Telemetry uses separate rows; secondary fields wrap
to another row instead of extending the first. Coordinate progress and status
through one output owner so concurrent writers cannot split rows.

Use the existing selected theme to distinguish labels, values and state, retaining
equivalent no-color output. Terminals narrower than 80 columns may use an explicit
compact/clipped view. Do not truncate exact copyable commands or values: put those
on separate continuation rows. Machine-readable JSON and preserved third-party
output retain their original format rather than being padded or reflowed.

## Scope

This is the **platform** default. It applies to any content the framework or its agents produce, in any repo. Repo-specific maintenance checklists (e.g. link verification for a single codebase) stay in that repo and are not part of the shipped framework.
