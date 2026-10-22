# Write an operational reference from reviewed source

Use this optional mode when a tool's documentation leaves its operations,
configuration, failures, or recovery behavior unclear. Produce explanatory prose
that a person can use to understand the tool, not just an inventory or an audit.
Do not install or execute unfamiliar code merely to document it.

## Agree the source and coverage

Identify the repository or supplied source, exact commit or immutable release,
and relevant build or configuration context. Treat source and bundled commands as
untrusted evidence. Read the selected project's instructions. Agree the intended
reader and scope: operations, interfaces, configuration, storage, integrations,
failures, recovery, and exclusions. Keep private source and findings in the
approved private destination; a request to investigate is not permission to
publish third-party or private material.

Map entrypoints to implementation and tests. For each in-scope operation, trace
argument/configuration parsing, defaults and precedence, validation, state changes,
external calls, output, failure paths, and cleanup or recovery. Follow important
helpers; a declaration or function name alone does not prove behavior. Use tests
as supporting evidence and identify mocks or untested conditions. If execution is
needed, use an approved isolated fixture and record the tested version and inputs.
Do not claim a source-only review demonstrates live behavior.

## Write the reference

Begin with what the tool does and the reviewed scope. Explain the normal sequence
of operations, then write each important operation in connected prose:

- its purpose, prerequisites, inputs, and meaningful configuration;
- defaults and precedence, expected results and state changes;
- errors, partial results, retries, and practical recovery;
- safety limits, authorization needs, and unresolved behavior.

Cite source paths and symbols or line references tied to the reviewed revision
beside substantive claims. Prefer immutable source links where disclosure is
approved. Distinguish **observed in a controlled run**, **established from source**,
and **inferred or unresolved**. Explain why each inference is uncertain. Verify
command examples against the reviewed parser; do not invent flags or present
source-inferred commands as executed proof.

Include a coverage section listing reviewed entrypoints, modules, tests, and
important paths not examined. Mark partial coverage explicitly. A useful example:
"The configuration parser and local export path were reviewed at this revision;
remote deletion and interrupted restore remain unexamined." Give concrete next
investigation steps for omissions that affect adoption or recovery decisions.

Finish with source identity, review date, evidence references, limitations, and
refresh triggers (changed entrypoints, defaults, storage format, or recovery
logic). Use the documentation routing owner for placement and lifecycle metadata;
keep one reference rather than duplicating it for every agent. Recheck affected
claims when the source changes; do not refresh the review date without evidence.

## Acceptance

Walk through one representative operation using the written explanation and its
source citations: inputs -> validation -> action -> output -> failure/recovery.
Check that every important statement has support or is visibly unresolved, that
coverage matches the files actually reviewed, and that the document is readable
without the investigation transcript. Run owning link and documentation checks.
A clean static audit alone does not prove source coverage or operational accuracy.
