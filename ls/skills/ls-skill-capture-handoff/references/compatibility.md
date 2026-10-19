# Compatibility record

This is the standalone candidate's historical baseline, not a claim about the
current or forthcoming LocalSetup release. The candidate came from immutable
source commit `4fe1fa2227e7980fc980ead5154117974ba5f9d0`; its original
archive SHA-256 is
`fea6cc2615c5fd839a5a70f0bf95f764ad82d2ac4d9cdc9b74262dc0c3118e96`.
LocalSetup integration of the adapted package is unreleased.

- Published LocalSetup release inspected by the candidate: **v4.49.2** (2026-10-19).
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
