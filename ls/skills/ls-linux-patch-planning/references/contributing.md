# Contributing to Linux Patcher

Contributions should preserve the current current boundary: Python tooling only, plan-only behavior unless a fully tested execution path is added, and platform-neutral documentation.

## Good First Contributions

- Improve wording in `SKILL.md` or `references/*.md`.
- Add distribution-specific review notes without claiming live execution support.
- Add tests for `scripts/patch_cli.py` validation and output.
- Improve JSON output while keeping backward compatibility.

## Tooling Rules

- Use Python 3.12+ for skill tooling.
- Do not add shell helper wrappers for patching flows.
- Validate all CLI input as hostile.
- Return actionable stderr for invalid input.
- Keep all generated command output plan-only unless the execution mode is fully designed, tested, and documented.

## Local Checks

From the repository root:

```bash
uv run --locked python -m py_compile ls/skills/ls-linux-patch-planning/scripts/patch_cli.py
uv run --locked python ls/skills/ls-linux-patch-planning/scripts/patch_cli.py status
uv run --locked python ls/skills/ls-linux-patch-planning/scripts/patch_cli.py auto --dry-run
uv run --locked pytest -q ls/tests/test_ls_linux_patcher_patch_cli.py
```

If you add skill-local tests, run them directly too:

```bash
uv run --locked pytest -q ls/skills/ls-linux-patch-planning/tests
```

## Pull Request Checklist

- [ ] Documentation matches shipped files and commands.
- [ ] No stale references to missing shell scripts.
- [ ] No product-specific agent branding or generated adapter paths.
- [ ] No broad sudo examples or unrestricted passwordless sudo guidance.
- [ ] CLI output is explicit about unavailable modes.
- [ ] Tests cover new behavior and failure modes.
- [ ] No credentials, host inventories, or runtime state are committed.
