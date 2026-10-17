---
name: ls-workflow-pipeline-pr-feedback-loop
description: Use when turning pull request feedback into fixes, tests, and follow-up review.
metadata:
  version: "1.0"
---

Use this pipeline package for structured PR feedback loops.
Follow the owning skills in sequence:

1. [ls-receiving-code-review](../../skills/ls-receiving-code-review/SKILL.md)
   for technical triage and verification of actionable feedback.
2. [ls-tdd-guide](../../skills/ls-tdd-guide/SKILL.md) for implementing accepted
   fixes with test evidence.
3. [ls-pr-reviewer](../../skills/ls-pr-reviewer/SKILL.md) for follow-up review
   only when materially changed risky code or a resolved substantial finding
   warrants another independent look.

These skills own the procedures; this package composes their phases.

Consume Copilot comments alongside human and other automated feedback. Bind each
review and accepted finding to the reviewed PR head SHA; confirm the current head
before acting. Recheck stale findings against the current diff rather than
assuming they still apply. Deduplicate the same defect across reviewers and
deterministic checks, retaining one actionable finding and its source references.
Send accepted fixes to the existing repository agents, not a cloud coding agent.

Do not request another review for unchanged code or routinely run both Lite and
Balanced review. A Copilot outage or exhausted allowance is advisory: continue
existing repository-agent review and deterministic validation without enabling a
paid fallback, disabling checks, or treating missing review as validation proof.
