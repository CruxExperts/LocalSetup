---
name: ls-wf-skill-import-normalize-test
description: Use when running the skill onboarding pipeline from vetting through sandbox testing.
metadata:
  version: "1.0"
---

Use this pipeline package to onboard skills with consistent checks.
Use [SKILL_IMPORTING.md](../../docs/SKILL_IMPORTING.md) for the staged onboarding
contract and final copy gates. Follow the owning skills for each capability:

- [ls-skill-vetter](../../skills/ls-skill-vetter/SKILL.md) for source and trust review.
- [ls-import-external-skills](../../skills/ls-import-external-skills/SKILL.md) for candidate staging
  and gated import.
- [ls-normalize-skill-format](../../skills/ls-normalize-skill-format/SKILL.md) for normalization.
- [ls-skill-sandbox-tester](../../skills/ls-skill-sandbox-tester/SKILL.md) for
  post-normalization smoke checks.

These sources own the procedures and acceptance gates; this package composes them.
