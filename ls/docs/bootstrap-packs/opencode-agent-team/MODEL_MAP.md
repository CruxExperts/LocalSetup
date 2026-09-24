---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-compliance
---

# OpenCode Agent Model Map

This map defines portable model slots for the OpenCode agent-team bootstrap pack. The public pack intentionally uses generic slot names so each installation can bind them to its own provider, budget, rate limits, and compliance requirements.

Use model references in this shape:

```text
<provider>/Agent-Main
```

## Slots

| Slot | Intended capability | Typical use | Notes |
|---|---|---|---|
| `Agent-Frontier` | GPT-5.5-class frontier reasoning | Architecture, security, final review, high-risk decisions | Use sparingly; require source-backed evidence for volatile claims. |
| `Agent-Main` | GPT-5.4-class general controller/build model | Primary controller, planning, integration, normal implementation | Default for primary agents. |
| `Agent-Coder` | GPT-5.3 Codex-class coding model | Bounded implementation with exact write scope and tests | Good fit for `worker` assignments. |
| `Agent-Scout` | GPT-5.4-mini-class fast scout model | Exploration, research summaries, validation summaries, low-risk parallel discovery | Default for read-heavy subagents. |
| `Agent-Lowcost` | Cheapest acceptable utility model | Titles, summaries, low-risk routine transformations | Do not use for high-risk decisions or external fact verification unless the controller rechecks evidence. |

## Binding Guidance

- Keep slot names stable in repo docs and examples.
- Bind slots in the target OpenCode provider config, not in this public pack.
- Avoid publishing private provider ids, account routes, rate cards, or credentials.
- Re-check official provider docs before making cost-sensitive routing changes.
- Prefer a stronger slot when the task involves security, irreversible operations, external integrations, or ambiguous architecture.
- Prefer `Agent-Scout` for low-risk read-only discovery and validation summaries.

## Example Binding Shape

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "<provider>": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Agent Model Slots",
      "options": {
        "baseURL": "https://example.invalid/v1",
        "apiKey": "{env:AGENT_MODEL_API_KEY}"
      },
      "models": {
        "Agent-Frontier": { "name": "Agent-Frontier" },
        "Agent-Main": { "name": "Agent-Main" },
        "Agent-Coder": { "name": "Agent-Coder" },
        "Agent-Scout": { "name": "Agent-Scout" },
        "Agent-Lowcost": { "name": "Agent-Lowcost" }
      }
    }
  }
}
```

The concrete model behind each slot is intentionally local policy. Record local bindings in private machine or project config, not in public framework docs.
