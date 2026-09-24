---
status: ACTIVE
version: 5.6
owner_package: ls-workflow-umbrella-run
---

# Agentic umbrella workflows (LocalSetup)

**Purpose:** Definition of umbrella workflows: single kickoff, no mid-run stop, PHC (pre-human-confirmation) gates, single final webhook. Framework-wide umbrella workflows (if any) are listed in [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md); repo-local umbrella names live only in `.agent/` queue files or PRDs.

## Umbrella invariants

- **Single kickoff**  - User invokes one named workflow; execution runs through defined phases without requiring repeated "continue" for each step.
- **No mid-run stop**  - Once started (after impact confirmation if required), the workflow runs to completion or a defined gate; the agent does not pause for approval between every sub-step unless the workflow spec defines such a gate.
- **PHC gates**  - Pre-human-confirmation gates may be defined for destructive or high-impact steps; at those gates the agent presents impact and waits for explicit user confirmation before proceeding.
- **Single final webhook**  - One outcome/notification at the end (e.g. status, summary, or external webhook) rather than many intermediate pings.

## Reference

- [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md)  - Named workflows, when to use, impact review, and any framework-wide umbrella IDs.
- [PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md](PRD_SCHEMA_EXTERNAL_AGENT_GUIDE.md)  - Spec format and outcome template.
