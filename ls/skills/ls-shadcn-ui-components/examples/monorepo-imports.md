# Monorepo Imports Example

Read first: `references/project-context.md`, `references/frameworks.md`, and
`references/components-json.md`.

Find every `components.json`, map it to package roots, and choose the one that
matches the edited app/package. Use `--cwd` when supported. Respect package
imports and exports for shared UI packages.

Failure modes: installing into the wrong package, using app aliases in the UI
package, and forgetting to import shared UI CSS in the consuming app.
