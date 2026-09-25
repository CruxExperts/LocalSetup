---
status: ACTIVE
version: 4.44
owner_skill: ls-framework-compliance
---

# OpenCode Agent Team Bootstrap Pack

This pack captures a reusable OpenCode-native controller/subagent workflow for LocalSetup. It mirrors the Codex agent-team intent while using OpenCode surfaces and schema names instead of Codex-specific config.

It is intentionally native to OpenCode:

- `~/.config/opencode/opencode.jsonc` or project `opencode.jsonc`
- project `AGENTS.md`
- `~/.config/opencode/agents/*.md` or project `.opencode/agents/*.md`
- `agent.<name>` config entries
- `mode: primary`, `mode: subagent`, or `mode: all`
- per-agent `model`
- per-agent `permission`
- `permission.task` for Task-tool subagent routing

OpenCode runtime source has supported both `agent/` and `agents/` agent directories, but the documented path is `agents/`. Use `agents/` in new pack examples unless a local installation explicitly requires the singular path.

## Operating Model

Use a primary controller for non-trivial work. The controller owns requirements, decomposition, ledger state, validation, and final acceptance. Keep fanout small: one or two subagents is normal, and three is reserved for clearly independent discovery, research, or validation.

Use direct single-agent work for trivial one-step tasks, simple questions, cases where no useful independent subtask exists, or active mode/tool constraints. For non-trivial work, first look for a useful bounded subtask that can preserve context, reduce risk, or improve verification.

## Role Map

- `build` or a custom `controller`: primary controller; `model: <provider>/Agent-Main`; `permission.task` allows only known subagents.
- `plan`: primary planning agent; edit denied; write-like bash denied or restricted.
- `explore`: read-only repository discovery; `model: <provider>/Agent-Scout`.
- `researcher` or `scout`: read-only external/current fact verification; `model: <provider>/Agent-Scout`. Built-in `scout` availability can be runtime-flag or version dependent, so custom `researcher` is safer for reusable packs.
- `worker`: bounded implementation; `model: <provider>/Agent-Coder`; `task: deny` unless the assignment explicitly needs nested delegation.
- `tester`: validation-only; `model: <provider>/Agent-Scout`; edit denied.
- `reviewer`: final risk, evidence, and regression review; `model: <provider>/Agent-Frontier`; edit and bash denied by default.

## Safe Config Example

Use generic model slots. Replace `<provider>` with the provider id configured in the target OpenCode installation.

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "default_agent": "controller",
  "agent": {
    "controller": {
      "mode": "primary",
      "model": "<provider>/Agent-Main",
      "description": "Primary controller for non-trivial LocalSetup work.",
      "permission": {
        "task": {
          "*": "deny",
          "explore": "allow",
          "researcher": "allow",
          "worker": "allow",
          "tester": "allow",
          "reviewer": "allow"
        }
      }
    },
    "plan": {
      "mode": "primary",
      "model": "<provider>/Agent-Main",
      "permission": {
        "edit": "deny",
        "bash": "deny",
        "task": {
          "*": "deny",
          "explore": "allow",
          "researcher": "allow",
          "reviewer": "allow"
        }
      }
    },
    "explore": {
      "mode": "subagent",
      "model": "<provider>/Agent-Scout",
      "description": "Read-only discovery of files, systems, tests, and risks.",
      "permission": {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "task": "deny"
      }
    },
    "researcher": {
      "mode": "subagent",
      "model": "<provider>/Agent-Scout",
      "description": "Read-only current or source-backed fact verification.",
      "permission": {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "webfetch": "allow",
        "websearch": "allow",
        "task": "deny"
      }
    },
    "worker": {
      "mode": "subagent",
      "model": "<provider>/Agent-Coder",
      "description": "Bounded implementation for an exact write scope.",
      "permission": {
        "task": "deny"
      }
    },
    "tester": {
      "mode": "subagent",
      "model": "<provider>/Agent-Scout",
      "description": "Validation commands, failure summaries, and evidence capture.",
      "permission": {
        "edit": "deny",
        "task": "deny"
      }
    },
    "reviewer": {
      "mode": "subagent",
      "model": "<provider>/Agent-Frontier",
      "description": "Final read-only risk, correctness, scope, and evidence review.",
      "permission": {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "task": "deny"
      }
    }
  }
}
```

Rule order matters for `permission.task`: put a wildcard deny first, then explicit allows, because the last matching rule wins. `permission.task` controls which subagents an agent can invoke through the Task tool. It does not block a user from directly invoking a subagent with `@agent` autocomplete.

Keep `default_agent` set to a primary agent such as `controller`, `build`, or `plan`; OpenCode falls back when the configured default is not a valid primary agent.

## Optional OpenAI-Compatible Provider Shape

This is a provider shape, not a recommendation for a specific vendor. Use environment-backed keys or the target installation's normal auth flow.

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "<provider>": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local Agent Model Slots",
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

## Ledger Expectations

For non-trivial repo work, keep the same LocalSetup controller ledger pattern used by Codex:

- objective and acceptance criteria
- current phase
- plan
- subtask table with owner, status, evidence, and next step
- checkpoints only after controller verification
- validation command table
- decisions and resume notes

OpenCode agent reports are evidence, not completion. The primary controller verifies reports, inspects diffs, records validation, and performs final acceptance.

## Approval Boundary

This pack may describe global OpenCode state, but repo-local audit and documentation work must not modify home-directory files, credentials, global permissions, provider keys, or external runtime mirrors without explicit user approval.

## Related Files

- [AUDIT_PROMPT.md](AUDIT_PROMPT.md)
- [MODEL_MAP.md](MODEL_MAP.md)
- [metadata.yaml](metadata.yaml)
- [../codex-agent-team/README.md](../codex-agent-team/README.md)
