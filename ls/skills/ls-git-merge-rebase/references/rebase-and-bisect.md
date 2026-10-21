# Rebase and Bisect

Use these recipes when cleaning local history or finding the commit that
introduced a bug. Confirm the target commits are local or explicitly approved
for rewrite before rebasing.

## Interactive Rebase

### Squash, Reorder, Edit Commits

```bash
# Rebase last 5 commits interactively.
git rebase -i HEAD~5

# Rebase onto main, covering all commits since diverging.
git rebase -i main
```

The editor opens with a pick list:

```text
pick a1b2c3d Add user model
pick e4f5g6h Fix typo in user model
pick i7j8k9l Add user controller
pick m0n1o2p Add user routes
pick q3r4s5t Fix import in controller
```

Commands available:

```text
pick   = use commit as-is
reword = use commit but edit the message
edit   = stop after this commit to amend it
squash = merge into previous commit and keep both messages
fixup  = merge into previous commit and discard this message
drop   = remove the commit entirely
```

### Common Patterns

```bash
# Squash fix commits into their parent.
# Change "pick" to "fixup" for the fix commits:
pick a1b2c3d Add user model
fixup e4f5g6h Fix typo in user model
pick i7j8k9l Add user controller
fixup q3r4s5t Fix import in controller
pick m0n1o2p Add user routes

# Reorder commits by moving lines.
pick i7j8k9l Add user controller
pick m0n1o2p Add user routes
pick a1b2c3d Add user model

# Split a commit into two.
# Mark as "edit", then when it stops:
git reset HEAD~
git add src/model.ts
git commit -m "Add user model"
git add src/controller.ts
git commit -m "Add user controller"
git rebase --continue
```

### Autosquash

```bash
# When committing a fix, reference the commit to squash into.
git commit --fixup=a1b2c3d -m "Fix typo"

# Or keep the fix commit message for the combined edit step.
git commit --squash=a1b2c3d -m "Additional changes"

# Later, rebase with autosquash.
git rebase -i --autosquash main
```

`fixup` and `squash` commits are automatically placed after their targets.

### Abort or Continue

```bash
git rebase --abort      # Cancel and restore original state.
git rebase --continue   # Continue after resolving conflicts or editing.
git rebase --skip       # Skip the current commit and continue.
```

## Bisect

### Binary Search Through Commits

```bash
# Start bisect.
git bisect start

# Mark current commit as bad.
git bisect bad

# Mark a known-good commit from before the bug existed.
git bisect good v1.2.0
# or:
git bisect good abc123

# Git checks out a middle commit. Test it, then mark the result.
git bisect good
git bisect bad

# Return to the original branch when done.
git bisect reset
```

### Automated Bisect

```bash
# Fully automatic: git runs the script on each commit.
# Script must exit 0 for good and 1 for bad.
git bisect start HEAD v1.2.0
git bisect run ./test-for-bug.sh
```

Example test script:

```bash
cat > /tmp/test-for-bug.sh << 'EOF'
#!/bin/bash
# Return 0 if the bug is not present, 1 if it is.
npm test -- --grep "login should redirect" 2>/dev/null
EOF
chmod +x /tmp/test-for-bug.sh
git bisect run /tmp/test-for-bug.sh
```

### Bisect With Build Failures

```bash
# If a commit does not compile, skip it.
git bisect skip

# Skip a range of known-broken commits.
git bisect skip v1.3.0..v1.3.5
```
