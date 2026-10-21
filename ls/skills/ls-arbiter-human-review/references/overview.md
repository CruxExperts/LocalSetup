# Arbiter Filesystem Queue Overview

`ls-arbiter-human-review` provides an agent-side Python helper for creating and reading Arbiter Zebu decision plans. The helper is intentionally local and file-based: it writes markdown plans into an Arbiter queue and reads completed plan metadata after a reviewer or bot records answers.

## Lifecycle

```text
agent pushes plan JSON
  -> scripts/arbiter_cli.py writes ~/.arbiter/queue/pending/<plan>.md
  -> Arbiter Zebu bot or human reviewer reads the pending plan
  -> reviewer records answers and marks the plan complete
  -> completed answers are available through get, await, or heartbeat polling
```

The CLI does not send chat messages, run the Arbiter Zebu bot, or perform external notification delivery. Notification files under `~/.arbiter/queue/notify/` are optional coordination inputs for the agent heartbeat.

## Queue Layout

| Path | Meaning |
|---|---|
| `~/.arbiter/queue/pending/` | Plans waiting for review |
| `~/.arbiter/queue/completed/` | Plans that have answers |
| `~/.arbiter/queue/notify/` | Session notification markers consumed by heartbeat logic |

Use `--queue-dir <path>` on any subcommand to test against a temporary queue without touching the user queue.

## Generated Plan Shape

The generated markdown file has YAML frontmatter with plan metadata and a `decisions` list. The body repeats the same decisions in a reviewer-friendly format but is not an answer store for the bundled CLI.

Important frontmatter fields:

| Field | Meaning |
|---|---|
| `planId` / `id` | Unique plan identifier |
| `tag` | Project or topic lookup key |
| `status` | Derived completion state: `pending` or `completed` |
| `total`, `answered`, `remaining` | Counts derived from the decisions list |
| `decisions[].answer` | Non-empty completed answer value |

`status`, `get`, and `await` validate answers from the frontmatter `decisions` list against each matching decision's option keys and `allowCustom` setting. A compatible reviewer must write answers into that list; plan-level completion fields and body text never bypass validation.

## Minimal Completion Convention

A completed plan may be moved into `completed/` or marked `status: completed` in frontmatter, but neither signal bypasses answer validation. Each answered frontmatter decision should include:

```yaml
status: answered
answer: selected-option-key
answered_at: "2026-05-09T18:00:00Z"
```

When every decision has a valid non-empty string answer, `get` returns a JSON `answers` object keyed by decision ID. An answer must match one of that decision's option keys unless `allowCustom: true`; otherwise the plan remains pending.

## Heartbeat Reference

Agent heartbeat or session-resume logic should check `notify/` for local notification files, then call:

```bash
python3 scripts/arbiter_cli.py get <plan-id>
```

If no notification file is present, the agent may still poll with `status --tag <tag>` for known blockers.
