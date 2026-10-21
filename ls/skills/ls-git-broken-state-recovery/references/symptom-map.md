# Symptom Map

Use this map after running `scripts/snapshot_git_state.py` with the controller task output directory.

| Symptom | Evidence to Confirm | Lowest-Risk First Move | Escalation |
| --- | --- | --- | --- |
| Phantom worktree path | `worktree_list.txt` includes a path that does not exist | `git worktree prune -v` | Resolve `--git-common-dir`, create a verified backup, and confirm the exact stale metadata path before removal |
| Branch "already used by worktree" | Branch delete/switch fails with a lock message | Locate the holder with `git worktree list --porcelain`; switch branch in that worktree | Treat verified stale ownership metadata as a phantom worktree path |
| Detached HEAD surprise | `status.txt` has `# branch.head (detached)`, symbolic-ref output is empty, and `rev_parse_head.txt` resolves a commit | Inspect reflog and create a rescue ref at `HEAD` | Switch only after the rescue ref resolves |
| HEAD/ref disagreement | Successful branch-current and symbolic-ref captures name different branches | Generate with `--repo` so the planner creates a verified backup automatically | Repair through `git symbolic-ref`; use only the planner's resolved per-worktree `HEAD` fallback after separate confirmation |
| Missing object/ref errors | A failed status or show-ref capture contains "unknown revision", "not a valid object name", or "cannot lock ref" | Inspect reflog and create verified rescue refs for local-only tips | Verify the remote target, then obtain exact point-of-risk confirmation before `git branch -f` |
| Zero-hash worktree entries | Worktree list contains an all-zero hash outside an initial unborn branch | Prune worktrees and verify filesystem paths | Recreate the affected worktree from a verified branch ref |

## Read the Room Before Acting

- Keep all snapshots, backup archives, and receipts beneath the explicit controller-assigned `.agents/state/<task-slug>/` directory.
- Treat `snapshot.json` as the path handoff. Do not guess the latest snapshot by directory modification time.
- If unpushed commits might exist, inspect `reflog_head.txt` and create rescue refs before any force operation.
- If multiple worktrees exist, use `--git-dir` for the current worktree and `--git-common-dir` for shared metadata.
- A backup is valid only when `backup_git_metadata.py` verifies required archive members and writes the digest receipt.
- Force updates and manual metadata edits require point-of-risk confirmation for the exact repository, target, values, and applicable receipt.
