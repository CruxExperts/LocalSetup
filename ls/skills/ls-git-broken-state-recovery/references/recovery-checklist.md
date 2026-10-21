# Recovery Checklist

Run this checklist before and after each remediation step.

## Preflight

1. Confirm the repository and resolve both metadata roots:

```bash
git rev-parse --show-toplevel
git rev-parse --path-format=absolute --git-dir
git rev-parse --path-format=absolute --git-common-dir
```

2. Use the controller-assigned task directory and capture a snapshot:

```bash
TASK_STATE_DIR="$PWD/.agents/state/<controller-task-slug>"
python scripts/snapshot_git_state.py . --output-dir "$TASK_STATE_DIR"
```

3. Before any manual metadata repair, create an automatic verified backup:

```bash
python scripts/backup_git_metadata.py . --output-dir "$TASK_STATE_DIR"
```

The command must report a verified archive, SHA-256 digest, and JSON receipt. A missing or failed receipt is a hard stop.

4. Before moving a branch pointer, inspect reflog, create a rescue ref for every local-only tip, and verify each rescue ref.
5. Immediately before a force or manual edit, obtain explicit confirmation naming the repository, exact ref or path, old and new values, and verified backup receipt when metadata is involved.

## Post-Fix Verification Gate

1. Status and branch coherence:

```bash
git status
git branch --show-current
git symbolic-ref -q HEAD
git rev-parse --verify HEAD^{commit}
```

2. Worktree integrity:

```bash
git worktree list --porcelain
```

3. Ref health:

```bash
git show-ref --head >/dev/null
git fsck --full --no-reflogs
```

4. Rescue and changed-ref verification:

```bash
git show-ref --verify refs/heads/rescue/<timestamp>
git rev-parse --verify refs/heads/<changed-branch>^{commit}
```

5. Smoke test normal operations:

```bash
git rev-parse HEAD
git log --oneline -n 3
```

## Hard Stop Conditions

Stop and escalate if any of these remain true:

- `git status` prints a fatal error.
- `git symbolic-ref -q HEAD` is empty but detached `HEAD` is not intentional and corroborated by porcelain-v2 status plus a resolvable commit.
- `git worktree list --porcelain` still references missing paths after prune.
- `git fsck` introduces new critical corruption.
- a rescue ref, remote target, archive digest, or backup receipt cannot be verified.
- the requested force or manual edit lacks point-of-risk confirmation for its exact target and values.
