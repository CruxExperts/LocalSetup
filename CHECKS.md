# Candidate check evidence

Baseline checked on 2026-10-19: LocalSetup **v4.49.2**, commit
`c0ecf4618255347615f52c5779b7f3a284048209`.

Completed checks:

- Python compilation of every included script.
- Every script's `--help` in a temporary standalone skill copy with no
  framework on its Python path and no inherited application environment.
- Agent Skills name/description/version shape, directory match, body size and
  all local Markdown references.
- Read-only registration projection against the actual release pack and smoke
  metadata: both pack memberships, taxonomy classes and commands are present.
- Native LocalSetup candidate validation and materialized package-surface
  validation: both packages accepted, no package-surface issues.
- Installed validator functions were compared structurally with the exact
  v4.49.2 source: frontmatter parsing, candidate validation, materialization and
  surface validation functions are identical.
- Content review for host identities, paths, credentials and private artifacts.
  Existing content-safety patterns found zero matches. That pattern set is
  marked stale by the framework, so it is supplementary evidence, not a fresh
  safety certification. Native candidate validation also warns that the chosen
  user-authorized global source location differs from its recommended repo path.

Limits: no complete upstream integration, generated-catalog regeneration,
cross-platform execution or fresh privileged service deployment is claimed for
this contribution. Transport helpers were generalized from a working Linux
deployment; the new parameterized package requires the receiving deployment's
own configuration and live-path checks. Project-required focused tests belong
to the maintainer's integration work. Archive creation, retrieval and digest
verification are recorded in the associated feature request.
