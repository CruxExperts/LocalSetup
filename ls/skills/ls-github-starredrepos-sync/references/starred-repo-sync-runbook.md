# Starred Repo Sync Runbook

1. Verify context:

   ```bash
   node scripts/verify-github-auth.mjs
   ```

2. List stars:

   ```bash
   node scripts/list-starred-repos.mjs --limit 100 --json
   ```

3. Preview sync:

   ```bash
   node scripts/sync-starredrepos.mjs --dry-run
   ```

4. Apply local archive updates only after reviewing the plan:

   ```bash
   node scripts/sync-starredrepos.mjs --apply
   ```

5. Commit, push, or create remote only when separately authorized:

   ```bash
   node scripts/sync-starredrepos.mjs --apply --commit
   node scripts/sync-starredrepos.mjs --apply --commit --push
   node scripts/sync-starredrepos.mjs --apply --create-remote
   ```

Each mutation flag unlocks only its named action.
