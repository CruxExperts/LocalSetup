---
name: ls-skill-capture-handoff
description: "Captures completed operational knowledge as a standalone global skill and prepares an authorized upstream feature request with complete compressed code. Use at task completion to offer capture, or after a user asks to preserve a setup workflow and contribute it."
license: MIT
compatibility: "Python 3.12+ for archive tooling; GitHub submission requires an authorized account. LocalSetup is optional."
metadata:
  version: "1.0"
---

# Capture and upstream handoff

**Purpose:** Preserve working knowledge once, then deliver reusable code to the
maintainer without making another agent rebuild it from a prose request.

## Boundaries

This skill owns the completion offer, packaging and contribution handoff.
The host's skill authoring/import workflow retains ownership of content creation,
duplicate resolution, external import screening and framework registration.
No other skills, framework commands or secret provider are required standalone.
Never install this behavior as global policy without explicit user direction.

## Workflow

1. Finish the actual software setup/repair task and its documentation first.
   When it adds reusable knowledge, ask once: “Would you like me to capture this
   as a global skill/workflow?” Respect a prior answer. Skip trivial changes,
   tasks with no reusable result, or a previously declined offer.
2. After authorization, inspect existing skills for an appropriate owner.
   Improve that owner when appropriate; resolve actual collisions with the user.
   Capture the shortest repeatable procedure, reusable implementation, parameters,
   prerequisites, checks, expected failures and recovery. Put details in linked
   references. Keep credentials, private evidence and deployment identities out.
3. Check the latest published LocalSetup release through verified public HTTPS.
   Record its exact tag and commit. Follow its Agent Skills and registration
   contract, while keeping LocalSetup commands optional. If the release cannot
   be checked, report compatibility as unconfirmed rather than claiming latest.
4. Exercise the package's real non-destructive entrypoints in a standalone
   temporary directory without framework imports. Run only checks authorized by
   the task and host policy. Record exact coverage and limitations. Install in
   the host's canonical global library; adapters reference that owner. Do not
   overwrite a managed package or silently opt other installations into policy.
5. Prepare the complete public candidate. Use an explicit reviewed file list;
   exclude private state, env files, logs, keys, signed URLs and Git history.
   Include code, references, templates, license, compatibility record and
   integration instructions. `scripts/package_skill.py` builds a deterministic
   tar.gz with per-file SHA-256 manifest and prints the archive digest. Its
   structural guards are not a substitute for reviewing public disclosure.
6. Once the concrete package is ready, ask: “Would you like me to submit this to
   LocalSetup as a feature request with the full code archive?” Existing explicit
   submission authorization satisfies this step. Silence is not authorization.
7. On approval, inspect the target repository and its contribution rules. Prefer
   an issue with a compressed attachment or a verified immutable archive link
   accessible to the receiving maintainer. If issue attachment upload is not
   supported, publish an explicitly scoped code-only proposal branch and link
   the raw compressed archive at its commit. Do not merge or release it.
   Avoid recipient-encrypted-only links the maintainer cannot decrypt.
8. The request includes the problem, proposed behavior, authorization boundaries,
   exact compatibility baseline, full code archive and SHA-256, check evidence,
   integration instructions and remaining work. Verify issue and download after
   submission. Record URLs and local ownership in durable documentation.

## Efficient reuse

Reuse the reviewed code and passing evidence; do not reconstruct implementations
from chat. Recheck changed layers and release-specific integration. Pass the next
agent a narrow action list: inspect archive/manifest, apply candidate, run focused
checks, adapt current contracts, review and integrate through normal project gates.
Submission is a recommendation, not automatic upstream acceptance.

## Examples and policy

- [Portable policy text](assets/global-policy.md) requires explicit adoption.
- [Feature request template](assets/feature-request.md) is an authoring aid;
  replace all placeholders before submission.
- [Compatibility](references/compatibility.md) records this package's baseline.
- Run `python3 scripts/package_skill.py --help` for archive arguments.
