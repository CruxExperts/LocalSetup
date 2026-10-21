# Update Procedure

Use this when refreshing the skill's volatile version facts.

## Steps

1. From the repo root, run the verifier:

   ```bash
   node ls/skills/ls-nodejs-nextjs-development/scripts/verify-current-versions.mjs --json
   ```

2. Compare the output with `data/verified-versions.json`. Preserve the exact
   `verifiedAt`, publication timestamp, computed age, 48-hour result, integrity,
   signature records, tarball, and provenance values from that single verifier run.
3. If facts changed, replace the stored snapshot with the verifier current object
   after removing only the computed `drift` array, then update:
   - `SKILL.md` current stable snapshot
   - `references/version-matrix.md`
   - `references/nextjs-latest-stable.md`
   - any reference that names the changed release line or date
4. Keep source URLs and the exact verification timestamp next to changed facts.
5. Run:

   ```bash
   node ls/skills/ls-nodejs-nextjs-development/scripts/verify-current-versions.mjs --json
   uv run --locked python ls/tools/skill_validation_scan.py ls/skills/ls-nodejs-nextjs-development --scan-root . --no-fetch
   uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
   git diff --check
   ```

## Rules

- Do not rewrite stable guidance from non-stable channels.
- Keep canary, beta, RC, experimental, Current, Active LTS, Maintenance LTS, and
  EOL labels distinct.
- If a project is pinned to older versions, use that project's versions for
  implementation decisions and use this skill only as context.
