---
name: ls-wf-contribution-review-submit
description: "Guide a contribution from choosing a task through implementation, review and submission; use to resume interrupted contributions or prepare a pull request. Keeps preparation separate from confirmed publication."
metadata:
  version: "1.0"
---

# Contribution, review, and submission

**Purpose:** Give each contribution a clear next step from initial work to
confirmed submission, using the receiving project's rules and existing owners.
This package coordinates work; it does not replace project-specific tests or
review requirements and does not grant publication authority.

## Start or resume

Read the receiving project's instructions, contribution guide, issue context,
branch/base rules, and current Git status before editing. Confirm the contribution's
objective and acceptance criteria; search for existing work to avoid duplicates.
Preserve unrelated changes and work inside the project's allowed checkout.
Use [Git workflows](../../skills/ls-git-merge-rebase/SKILL.md) for branch and history
operations. Do not create another checkout without applicable authority.

Keep one compact progress record under the receiving repository's existing private
task-state owner. Reuse an existing ledger rather than creating a second tracker.
Record the objective, source issue, applicable project rules, repository/ref and
HEAD, dirty baseline, owned paths, acceptance checks and results, review findings,
unfinished work, next action, and publication authority. Add the submitted URL and
verified remote revision only after observing them. Never store credentials or
raw private evidence in a public contribution document.

On resume, verify the record against current source, branch, dirty state and remote
revisions. Reuse checks only while their inputs remain applicable. Explain stale
or missing evidence and continue the smallest unfinished step.

## Progress view

Report one state, evidence, remaining work, and the next action:

- **Working:** scope or implementation is incomplete, checks have failed, or
  review findings remain unresolved.
- **Ready for review:** the scoped diff and applicable checks are available;
  include known limitations and a reviewable explanation.
- **Ready for submission:** required review is resolved, documentation and
  generated outputs are aligned, the exact signed checkpoint is recorded where
  required, and the proposed destination and payload are prepared.
- **Submitted:** the receiving service confirms the contribution URL and expected
  branch/head. A local commit, successful push alone, or attempted request does
  not prove a pull request was created. Submission does not imply merge.

States can move backward when scope or relevant inputs change. Preserve earlier
checkpoints and distinguish local state from pending or unknown remote actions.

## Compose the existing owners

Implement within the agreed files and follow the project's focused validation.
Use [requesting review](../../skills/ls-requesting-code-review/SKILL.md) when an
independent review is useful and
[receiving review](../../skills/ls-receiving-code-review/SKILL.md) to verify findings
before fixing them. Do not request repeat reviews for unchanged code.

Prepare the final contribution title, explanation, checks, issue links, and any
compatibility or unfinished-work notes. Follow
[GitHub publishing](../../skills/ls-github-release-publishing/SKILL.md) for the
applicable publication checks and privacy review, without making a product release
or unrelated settings audit a prerequisite for an ordinary contribution. Show the
prepared result before requesting any missing publication decision. Reuse existing
explicit authority; preparation does not authorize a push or submission.

After an authorized submission, verify its URL and exact head through the receiving
service and update the record. If acknowledgement is lost, reconcile read-only
before retrying to avoid duplicate submissions. If publication fails, retain the
reviewable local checkpoint, report the concrete blocker, and leave state at
ready for submission with its next action.
