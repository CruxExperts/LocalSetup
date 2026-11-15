---
localsetup_provenance:
  schema_version: 1
  source_provenance_hash: c4edb7bc34c31658f2f456e70b3f34ff8c79919938fd7c6334d5038237089bcc
  emitter: generate-docs
framework_version: 4.94.1
source_commit: e72af37f9002f804f90eeed1a49a8600a797dd69
artifact_sha256: 5ce4949227d75f75f72c4ce822e3c0e7958e57a16acf1fdc16a050a35da5d212
---
# Platform Adapters

Fresh global-only invocation without a selected or recorded target creates no adapters. Explicit `--tools` or `--platforms` selects fresh adapters; selector-free updates of a recorded target retain validated clients, scope, and paths.

| Platform | Repo Paths | Verify Rules |
|---|---|---|
| `codex` | `.agents/skills` | `skills_visible, namespace_ls` |
| `claude-code` | `.claude/skills` | `skills_visible, namespace_ls` |
| `cursor` | `.agents/skills` | `skills_visible, namespace_ls` |
| `kilo` | `.agents/skills` | `skills_visible, namespace_ls` |
| `opencode` | `.agents/skills` | `skills_visible, namespace_ls` |
| `openclaw` | `.agents/skills` | `skills_visible, namespace_ls` |
| `github-copilot-cli` | `.agents/skills` | `skills_visible, namespace_ls` |
| `github-copilot-vscode` | `.agents/skills` | `skills_visible, namespace_ls` |
| `cline-cli` | `.cline/skills` | `skills_visible, namespace_ls` |
| `cline-vscode` | `.cline/skills` | `skills_visible, namespace_ls` |
| `amp-cli` | `.agents/skills` | `skills_visible, namespace_ls` |
| `goose-cli` | `.agents/skills` | `skills_visible, namespace_ls, goose_skills_configured` |
| `pi-cli` | `.agents/skills` | `skills_visible, namespace_ls` |
| `hermes-agent` | `.hermes/skills` | `skills_visible, namespace_ls` |
| `qwen-code-cli` | `.agents/skills` | `skills_visible, namespace_ls` |
| `kimi-cli` | `.agents/skills` | `skills_visible, namespace_ls` |
| `factory-droid` | `.agents/skills` | `skills_visible, namespace_ls` |
| `antigravity-app` | `.agents/skills` | `skills_visible, namespace_ls` |
| `gemini-cli` | `.agents/skills` | `skills_visible, namespace_ls` |
| `omp-cli` | `.agents/skills` | `skills_visible, namespace_ls` |
