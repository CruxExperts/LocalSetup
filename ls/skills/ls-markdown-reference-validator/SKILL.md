---
name: ls-markdown-reference-validator
description: "Use when validating repository Markdown local references and anchors; scheduled-safe YAML-configured reports keep host-aware scans explicit and local-only."
metadata:
  version: "1.0"
compatibility: "Python 3.12+, PyYAML. Config-driven targets and Kilo manifest discovery."
---

# Markdown reference validator

Validate repository Markdown reference integrity across LocalSetup docs, skills, templates, and repository documentation.

## Purpose

- Parse markdown files from configured paths/globs.
- Validate local reference targets from markdown links and inline code paths.
- Validate markdown `#anchor` fragments against heading slugs.
- Write a durable markdown report for AI agents and humans.
- Run safely on schedule with interval guard and optional jitter.

## Why this exists

Framework and ops docs evolve quickly. Broken references waste agent cycles and reduce reliability. This skill provides a deterministic, zero-token integrity check that can run in cron/login automation and produce one report location agents can inspect.

## Inputs and outputs

- **Input config (YAML):** `ls/skills/ls-markdown-reference-validator/templates/markdown_reference_audit.yaml`, or a repo-local copy adapted from it.
- **Strict repo profile:** `ls/skills/ls-markdown-reference-validator/templates/markdown_reference_strict_repo.yaml`
- **Host-aware profile:** `ls/skills/ls-markdown-reference-validator/templates/markdown_reference_host_aware.yaml`
- **Skill entrypoint:** `scripts/markdown_reference_audit.py`
- **Engine script:** `ls/skills/ls-markdown-reference-validator/scripts/markdown_reference_validator.py`
- **Default report:** .localsetup/state/markdown-reference/default.md (ignored local state)
- **Default run-state marker:** .localsetup/state/markdown-reference/default-last-run-epoch (ignored local state)

## Run and report

1. Confirm the target YAML configuration before a scheduled or bulk audit,
   including scan scope, report destination, and interval settings.
2. Run the configured scanner using the commands below.
3. Review the report's pass or failure counts and summarize broken paths and
   anchors. Record the report location and remaining findings in the audit summary.

## Typical commands

Run once manually:

```bash
python3 ls/skills/ls-markdown-reference-validator/scripts/markdown_reference_audit.py \
  --force \
  --reason manual
```

Run the strict repo-owned profile:

```bash
python3 ls/skills/ls-markdown-reference-validator/scripts/markdown_reference_audit.py \
  --config ls/skills/ls-markdown-reference-validator/templates/markdown_reference_strict_repo.yaml \
  --force \
  --reason manual
```

Run the explicit host-aware profile (its report stays in ignored local state and outside-repository paths are redacted):

```bash
python3 ls/skills/ls-markdown-reference-validator/scripts/markdown_reference_audit.py \
  --config ls/skills/ls-markdown-reference-validator/templates/markdown_reference_host_aware.yaml \
  --force \
  --reason manual
```

Run with login jitter (3-5 min):

```bash
python3 ls/skills/ls-markdown-reference-validator/scripts/markdown_reference_audit.py \
  --reason login-autostart \
  --jitter-min-seconds 180 \
  --jitter-max-seconds 300
```

## Scheduling model

Recommended defaults:

- **Periodic:** every 12 hours (cron trigger).
- **Login:** run once at user login with random delay 3-5 minutes.

Use `ls-cron-job-scheduling` with a user-created manifest (for example `cron/manifest.yaml` in the target repo) for periodic trigger, and XDG autostart desktop entry for login trigger. This skill does not ship a cron manifest.

## Config model (YAML sidecar)

The sidecar config defines:

- `targets[]`: each with `base_dir`, `include_globs`, optional `exclude_globs`.
- `kilo_manifest_discovery.manifests[]`: `kilo.json` or `kilo.jsonc` paths to discover additional instruction/skills markdown surfaces. JSONC manifests may use comments and trailing commas.
- `report.output_path`, `report.state_file`, and `report.max_findings`.
- `extraction.inline_code_mode`: `off | smart | all` (`smart` is default and recommended).
- `ignore.*` blocks to reduce false positives from templates/examples while keeping strict checks for concrete links.

The shipped default profile is repo-only and writes report/state only beneath ignored .localsetup/state/markdown-reference/. Use the strict repo profile for narrower tracked-source link and anchor review. Use the host-aware profile only when you explicitly need local adapter or host instruction surfaces; its report remains local-only and redacts paths outside repo_root.

The strict profile checks active documentation, not the transitive completeness of
selectively imported upstream archives. It excludes only `*.source.md` snapshots
under `references/upstream/`; authored coverage notes and ordinary references remain
in scope. Preserve snapshot bytes and verify their recorded hashes separately.
Use a custom profile without that exclusion when intentionally auditing archived
upstream links, and interpret omissions against the archive's import policy.

### Config schema (practical defaults)

```yaml
extraction:
  inline_code_mode: smart
  # Back-compat toggle: if inline_code_mode omitted, true => smart / false => off
  include_inline_code_paths: true

ignore:
  # Skip scanning these markdown sources entirely.
  source_file_globs:
    - "**/node_modules/**"
    - "**/.kilo/state/**"
    - "**/.kilo/plans/**"

  # Skip targets matching any regex (examples/placeholders/snippets).
  target_regexes:
    - '^URL$'
    - '^<[^>]+>$'
    - '^\$[A-Za-z_][A-Za-z0-9_]*(/.*)?$'

  # Skip targets starting with these prefixes.
  path_prefixes:
    - "/etc/"
    - "old.reddit.com/"

  # Skip targets containing these placeholder tokens.
  placeholder_tokens:
    - "<repo>"
    - "{slug}"
    - "YYYY-MM-DD"
```

### Example snippet

```yaml
targets:
  - name: framework-docs-and-skills
    base_dir: "{repo_root}"
    include_globs:
      - "ls/docs/**/*.md"
      - "ls/skills/**/*.md"
      - "ls/templates/**/*.md"

kilo_manifest_discovery:
  manifests:
    - "{repo_root}/kilo.jsonc"
    - "/home/user/.config/kilo/kilo.jsonc"
```

## Validation behavior

- Skips external URLs (`http`, `https`, `mailto`, etc.).
- Resolves relative targets from the source file's directory.
- Also tries repo-root resolution for repo-absolute style links like `ls/...` or `docs/...`.
- Applies ignore controls in this order: source glob skip -> placeholder/prefix/regex/pseudo-path skip -> strict path/anchor checks.
- Validates config section shapes up front and reports schema errors instead of raw tracebacks.
- Reports unreadable source markdown, anchor targets, and state files instead of treating them as clean results.
- Flags:
  - `missing_path`
  - `missing_anchor`
  - `unreadable_source`
  - `unreadable_anchor_target`
- Includes Kilo manifest discovery notes in report for observability.

## Safety and hardening

- Input hostile-by-default handling.
- Sanitized text fields and bounded lengths.
- Interval guard to avoid noisy repeated runs.
- Optional jitter to reduce post-login contention.

## Related references

- `ls/docs/TOOLING_POLICY.md`
- `ls/docs/INPUT_HARDENING_STANDARD.md`
- `ls/skills/ls-cron-job-scheduling/SKILL.md`
