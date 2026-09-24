---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-audit
date: 2026-05-10
---

# Codex CLI Researcher Report

## Summary

The installed local CLI is `codex-cli 0.130.0`. `codex debug prompt-input` is the best local prompt-load check found because it renders the model-visible prompt input list as JSON. `CODEX_HOME` was unset, so this install uses `~/.codex/config.toml` by default.

Repo-local `AGENTS.md` is supported. Global `~/.codex/AGENTS.md` exists and loaded in a non-repo prompt-load check, but global/repo merge behavior has upstream ambiguity. Custom agents are supported locally via `~/.codex/agents/*.toml`. The local model catalog recognizes `gpt-5.5`, `gpt-5.4-mini`, and `gpt-5.3-codex`.

## Local Evidence

| Evidence | Result |
|---|---|
| `codex --version` | `codex-cli 0.130.0` |
| `codex debug --help` | Shows `models`, `app-server`, and `prompt-input` |
| `codex debug prompt-input` | Renders model-visible prompt input as JSON |
| `~/.codex/config.toml` | Active global config surface with `CODEX_HOME` unset |
| `~/.codex/agents/*.toml` | Role files use `name`, `description`, `model`, `model_reasoning_effort`, `sandbox_mode`, and `developer_instructions`; researcher also uses `web_search = "live"` |
| `codex debug models` | Local catalog includes `gpt-5.5`, `gpt-5.4-mini`, and `gpt-5.3-codex` |

## External Sources

- OpenAI model docs, accessed 2026-05-10: `https://developers.openai.com/api/docs/models`
- `gpt-5.5`: `https://developers.openai.com/api/docs/models/gpt-5.5`
- `gpt-5.4-mini`: `https://developers.openai.com/api/docs/models/gpt-5.4-mini`
- `gpt-5.3-codex`: `https://developers.openai.com/api/docs/models/gpt-5.3-codex`
- Codex use cases, accessed 2026-05-10: `https://developers.openai.com/codex/use-cases`
- Upstream Codex `AGENTS.md` doc stub: `https://github.com/openai/codex/blob/main/docs/agents_md.md`
- Upstream issues/discussions used as weaker evidence: `openai/codex#18189`, `openai/codex#15993`, `openai/codex#11717`, `openai/codex#20656`, `openai/codex#21512`

## Supported Claims

- `~/.codex/config.toml` is the default config path when `CODEX_HOME` is unset.
- `codex debug prompt-input` is appropriate for prompt-load checks.
- Repo-local `AGENTS.md` is supported.
- Custom agent TOML files are supported in this local install.
- The three selected model names are valid in the local model catalog.

## Unverified Or Drift-Prone Claims

- A complete public custom-agent TOML schema was not found.
- Global `~/.codex/AGENTS.md` merge behavior with repo-local `AGENTS.md` is not fully proven for every session type.
- Goal mode is still drift-prone; upstream reports show plan/goal interactions can be unstable.
- Write capability for custom agents appears sandbox-driven, but no separate official `write_permissions` schema was found.

## Recommendations

- Treat prompt-load evidence as session/context specific.
- Keep global-config changes approval-gated.
- Keep bootstrap-pack docs Codex-first and avoid claiming stable cross-framework behavior.
