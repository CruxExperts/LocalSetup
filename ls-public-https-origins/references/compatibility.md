# Compatibility record

- Latest published LocalSetup release inspected: **v4.49.2** (2026-10-19).
- Release commit: `c0ecf4618255347615f52c5779b7f3a284048209`.
- Source contract: `ls/docs/PLATFORM_REGISTRY.md`, Skill registration (new skills),
  plus release `ls/config/pack.yaml` and `ls/tests/skill_smoke_commands.yaml`.
- Standard Agent Skills metadata; no required framework-specific frontmatter.
- Standalone runtime: Python 3.12+ standard library; no LocalSetup, Envman or
  sibling skill imports. Optional Linux deployment requirements are documented.
- Evidence: Python source compilation, CLI help in an isolated standalone copy,
  relative Markdown reference checks, metadata and package surface validation,
  and read-only registration projection against the release metadata.
- Scope: compatibility of package structure, dependencies and registration
  contracts. This is not a claim that an upstream integrated release or every
  target platform has been exercised. Final generated catalogs and deployment
  checks belong to the maintainer's owning checkout after integration.

Primary references: [release](https://github.com/CruxExperts/LocalSetup/releases/tag/v4.49.2),
[Agent Skills specification](https://agentskills.io/specification).
