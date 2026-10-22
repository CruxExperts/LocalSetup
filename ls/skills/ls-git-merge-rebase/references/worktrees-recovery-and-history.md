# Worktrees, Recovery, and History

Use these recipes for parallel branches, recovery through reflog, cherry-picks,
stash management, and history inspection.

## Worktree

### Work on Multiple Branches

```bash
# Add a worktree for a different branch.
git worktree add ../myproject-hotfix hotfix/urgent-fix

# Add a worktree with a new branch.
git worktree add ../myproject-feature -b feature/new-thing

# List worktrees.
git worktree list

# Remove a worktree when done.
git worktree remove ../myproject-hotfix

# Prune stale worktree references.
git worktree prune
```

### Use Cases

```bash
# Review a PR while keeping your current work untouched.
git worktree add ../review-pr-123 origin/pr-123

# Run tests on main while developing on a feature branch.
git worktree add ../main-tests main
cd ../main-tests && npm test

# Compare behavior between branches side by side.
git worktree add ../compare-old release/v1.0
git worktree add ../compare-new release/previous
```

## Continue an Agent Task in a Branch Workspace

Use this sequence when an in-progress task needs a separate Git branch workspace
and an LSCli session checkpoint:

1. **Preflight the selected client.** Confirm that the native agent client can
   start work in the destination and accept a concise handoff. If that client
   cannot continue there, explain the limitation and stop before creating the
   destination or copying session history.
2. **Review and preserve the original work.** In the source checkout, inspect
   `git status --short` and the diff for task-related paths. Keep the original
   worktree and all dirty work intact. Review separately how task-related edits
   should reach the new workspace; the session branch does not copy files.
3. **Prepare the destination branch workspace.** Create or select the Git
   worktree using the normal worktree procedure above. Keep the source checkout
   available as the untouched record of its original work.
4. **Copy a settled checkpoint into a new session.** Supply the original
   session's source workspace separately from the destination workspace:

   ```bash
   lscli branch \
     --source-task SOURCE_TASK \
     --source-session SOURCE_SESSION \
     --checkpoint CHECKPOINT_SHA256 \
     --task DESTINATION_TASK \
     --session DESTINATION_SESSION \
     --profile PROFILE \
     --source-workspace /path/to/source-checkout \
     --workspace /path/to/destination-worktree
   ```

   `--workspace` must identify an existing destination directory. Omitting
   `--source-workspace` keeps the older same-workspace behavior by treating
   `--workspace` as the source too. The source lease checks its recorded
   workspace identity; the checkpoint must be settled, current with its journal,
   and compatible with the selected profile. Branching leaves source history
   and workspace files unchanged.
5. **Review the receipt and resume with fresh authority.** The JSON result names
   `destination_workspace`, along with the new task, session and checkpoint.
   Show that destination path in the handoff, then resume the checkpoint there
   with a fresh run grant. The destination starts with an empty operation
   journal; file grants, approvals and credentials are not copied.
6. **Write the handoff explicitly.** Carry forward the task objective, relevant
   instructions, completed progress and next steps. The checkpoint preserves
   settled LSCli message bytes, but this does not reopen the native host client's
   exact live conversation or restore its permissions.

If destination creation or checkpoint branching fails, leave the source
worktree untouched. Inspect any destination session evidence before retrying;
never overwrite or automatically retry an existing session.

## Reflog Recovery

### See What Git Remembers

```bash
# Show reflog for HEAD movements.
git reflog

# Show reflog for a specific branch.
git reflog show feature/my-branch

# Show with timestamps.
git reflog --date=relative
```

Example reflog output:

```text
abc123 HEAD@{0}: commit: Add feature
def456 HEAD@{1}: rebase: moving to main
ghi789 HEAD@{2}: checkout: moving from feature to main
```

### Recover From Mistakes

Before **every** `git reset --hard` below, stop unless `git status --short` is
clean. Preserve desired staged, unstaged, and relevant untracked work with an
approved WIP commit, patch, `git stash push -u`, or separate worktree. A backup
branch preserves the current commit only; it does not preserve uncommitted work.

```bash
# Undo a bad rebase after finding the pre-rebase commit in reflog.
git reflog
git reset --hard ghi789

# Recover a deleted branch after finding its last commit.
git reflog
git branch recovered-branch abc123

# Recover after reset --hard.
git reflog
git reset --hard HEAD@{2}

# Recover a dropped stash.
git fsck --unreachable | grep commit
git stash list
git log --walk-reflogs --all -- stash
```

## Cherry-Pick

### Copy Commits to Another Branch

```bash
# Pick a single commit.
git cherry-pick abc123

# Pick multiple commits.
git cherry-pick abc123 def456 ghi789

# Pick a range: exclusive start, inclusive end.
git cherry-pick abc123..ghi789

# Pick without committing, leaving changes staged.
git cherry-pick --no-commit abc123

# Cherry-pick from another remote or fork.
git remote add upstream https://github.com/other/repo.git
git fetch upstream
git cherry-pick upstream/main~3
```

### Handle Conflicts During Cherry-Pick

```bash
# Resolve conflicts in files, then stage resolved files.
git add resolved-file.ts

# Continue the cherry-pick.
git cherry-pick --continue

# Or abort the cherry-pick.
git cherry-pick --abort
```

## Stash Patterns

```bash
# Stash with a message.
git stash push -m "WIP: refactoring auth flow"

# Stash specific files.
git stash push -m "partial stash" -- src/auth.ts src/login.ts

# Stash including untracked files.
git stash push -u -m "with untracked"

# List stashes.
git stash list

# Apply most recent stash and keep it in the stash list.
git stash apply

# Apply most recent stash and remove it from the stash list.
git stash pop

# Apply a specific stash.
git stash apply stash@{2}

# Show what is in a stash.
git stash show -p stash@{0}

# Create a branch from a stash.
git stash branch new-feature stash@{0}

# Drop a specific stash.
git stash drop stash@{1}

# Clear all stashes.
git stash clear
```

## Blame and Log Archaeology

```bash
# Show who changed each line, with date.
git blame src/auth.ts

# Blame a specific line range.
git blame -L 50,70 src/auth.ts

# Ignore whitespace changes in blame.
git blame -w src/auth.ts

# Find when a string was added or removed.
git log -S "function oldName" --oneline

# Find when a regex pattern was added or removed.
git log -G "TODO.*hack" --oneline

# Follow a file through renames.
git log --follow --oneline -- src/new-name.ts

# Show log with file changes.
git log --stat --oneline -20

# Show all commits affecting a specific file.
git log --oneline -- src/auth.ts

# Show diff of a specific commit.
git show abc123
```
