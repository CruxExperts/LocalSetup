---
status: ACTIVE
version: 4.47
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
| `Agent-Frontier` | Highest-capability reasoning model available to the installation | Architecture, security, final review, high-risk decisions | Allocate when the risk or complexity warrants it; require source-backed evidence for volatile claims. |
| `Agent-Main` | General-purpose model suited to integrated planning and implementation | Primary controller, planning, integration, normal implementation | Choose the least-cost model that meets task accuracy and risk needs. |
| `Agent-Coder` | Coding-capable model suited to bounded implementation | Changes with an exact write scope and focused checks | Good fit for `worker` assignments. |
| `Agent-Scout` | Fast, lower-cost model suited to read-only tasks | Exploration, research summaries, validation summaries, low-risk discovery | Default for clear read-heavy subagents. |
| `Agent-Lowcost` | Least-cost model that meets the output quality needed | Titles, summaries, routine transformations | Do not use for high-risk decisions or external fact verification unless the controller rechecks evidence. |

## Binding Guidance

- Keep slot names stable in repo docs and examples.
- Bind slots in the target OpenCode provider config, not in this public pack.
- Avoid publishing private provider ids, account routes, rate cards, or credentials.
- Choose the least-cost slot that meets the task's accuracy and risk needs; use more capable slots when they materially improve quality or reduce risk.
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
