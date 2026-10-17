---
localsetup_provenance:
  schema_version: 1
  source_provenance_hash: dabf3cd90cbd2585d57599c8da7f48da83437ed2f911ee333c3ed11ca2ec901d
  emitter: generate-docs
framework_version: 4.49.1
source_commit: e1133e8d06fc743c8f715842f7c2233f9aece7b8
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
