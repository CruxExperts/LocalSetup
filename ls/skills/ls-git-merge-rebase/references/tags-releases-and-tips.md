# Tags, Releases, and Tips

Use these recipes for release tags and compact reminders that are useful across
advanced Git workflows.

## Tags and Releases

```bash
# Create annotated tag, preferred for releases.
git tag -a v1.2.0 -m "Release 1.2.0: Added auth module"

# Create lightweight tag.
git tag v1.2.0

# Tag a past commit.
git tag -a v1.1.0 abc123 -m "Retroactive tag for release 1.1.0"

# List tags.
git tag -l
git tag -l "v1.*"

# Push tags.
git push origin v1.2.0
git push origin --tags

# Delete a tag locally and remotely.
git tag -d v1.2.0
git push origin --delete v1.2.0
```

## Tips

- `git rebase -i` is the single most useful advanced git command. Learn it first.
- Never rebase commits that have been pushed to a shared branch. Rebase your local/feature work only.
- `git reflog` can help locate prior local reference tips, but recovery is not guaranteed. The 90-day `gc.reflogExpire` default is configurable; unreachable entries use a separate 30-day default and `git gc` can prune reflogs and unreachable objects. Inspect the relevant local reflog promptly.
- `git bisect run` with an automated test is faster than manual binary search and eliminates human error.
- Worktrees are cheaper than multiple clones because they share `.git` storage.
- Prefer `git subtree` over `git submodule` unless you have a specific reason. Subtrees are simpler for collaborators.
- Enable `rerere` globally. It remembers conflict resolutions so you never solve the same conflict twice.
- `git stash push -m "description"` is much better than bare `git stash`. You'll thank yourself when you have 5 stashes.
- `git log -S "string"` (pickaxe) is the fastest way to find when a function or variable was added or removed.
