# Repository Structure and Conflicts

Use these recipes when managing shared code layouts, sparse monorepo checkouts,
or merge and rebase conflicts.

## Subtree and Submodule

### Subtree

Subtree copies code into your repository and does not require special clone
commands for collaborators.

```bash
# Add a subtree.
git subtree add --prefix=lib/shared https://github.com/org/shared-lib.git main --squash

# Pull updates from upstream.
git subtree pull --prefix=lib/shared https://github.com/org/shared-lib.git main --squash

# Push local changes back to upstream.
git subtree push --prefix=lib/shared https://github.com/org/shared-lib.git main

# Split subtree into its own branch for extraction.
git subtree split --prefix=lib/shared -b shared-lib-standalone
```

### Submodule

Submodule records a pointer to another repository at a specific commit.

```bash
# Add a submodule.
git submodule add https://github.com/org/shared-lib.git lib/shared

# Clone a repo with submodules.
git clone --recurse-submodules https://github.com/org/main-repo.git

# Initialize submodules after cloning without --recurse-submodules.
git submodule update --init --recursive

# Update submodules to latest.
git submodule update --remote

# Remove a submodule.
git rm lib/shared
rm -rf .git/modules/lib/shared
# Remove the entry from .gitmodules if it persists.
```

### Which One to Use

```text
Subtree: Simpler, no special commands for cloners, code lives in your repo.
Use when: shared library, vendor code, infrequent upstream changes.

Submodule: Pointer to exact commit, smaller repo, clear separation.
Use when: large dependency, independent release cycle, many contributors.
```

## Sparse Checkout

### Check Out Only Needed Directories

```bash
# Enable sparse checkout.
git sparse-checkout init --cone

# Select directories.
git sparse-checkout set packages/my-app packages/shared-lib

# Add another directory.
git sparse-checkout add packages/another-lib

# List what is checked out.
git sparse-checkout list

# Disable sparse checkout and check out everything again.
git sparse-checkout disable
```

### Clone With Sparse Checkout

```bash
# Partial clone plus sparse checkout for huge repositories.
git clone --filter=blob:none --sparse https://github.com/org/monorepo.git
cd monorepo
git sparse-checkout set packages/my-service

# No-checkout clone for metadata first.
git clone --no-checkout https://github.com/org/monorepo.git
cd monorepo
git sparse-checkout set packages/my-service
git checkout main
```

## Conflict Resolution

### Understand Conflict Sides

Conflict markers delimit the two candidate sections; edit the file to the intended
resolved content, then remove all three marker lines before staging.

<code>&lt;&lt;&lt;&lt;&lt;&lt;&lt; HEAD</code><br>
first candidate section<br>
<code>&equals;&equals;&equals;&equals;&equals;&equals;&equals;</code><br>
second candidate section<br>
<code>&gt;&gt;&gt;&gt;&gt;&gt;&gt; commit-or-branch</code>

For a **merge**, index stage `:1` is the common ancestor, `:2` is the current
branch (ours), and `:3` is the incoming branch (theirs). `git checkout --ours`
therefore selects the current branch; `--theirs` selects the merged-in branch.

For a **rebase**, Git applies the commit being replayed onto the upstream/base
branch. Stage `:1` is the merge base, `:2` (ours) is the upstream/base version,
and `:3` (theirs) is the commit being replayed. Do not use whole-file
`--ours` or `--theirs` until this mapping matches the intended content.

```bash
# Inspect base, ours, and theirs before choosing a side.
git show :1:path/to/file.ts
git show :2:path/to/file.ts
git show :3:path/to/file.ts
git diff --cc path/to/file.ts

# After choosing the correct side for the current merge or rebase context.
git checkout --ours path/to/file.ts
git add path/to/file.ts

# Or select theirs after applying the mapping above.
git checkout --theirs path/to/file.ts
git add path/to/file.ts

# Use a merge tool when neither whole-file side is correct.
git mergetool
```

### Rebase Conflict Workflow

```bash
# During rebase, conflicts appear one commit at a time.
# Fix the conflict in the file, then stage it.
git add fixed-file.ts
git rebase --continue

# If a commit is now empty after resolution, skip it.
git rebase --skip
```

### Rerere

`rerere` reuses recorded conflict resolutions.

```bash
# Enable rerere globally.
git config --global rerere.enabled true

# See recorded resolutions.
ls .git/rr-cache/

# Forget a bad resolution.
git rerere forget path/to/file.ts
```
