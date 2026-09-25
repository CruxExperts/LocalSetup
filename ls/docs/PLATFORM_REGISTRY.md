---
status: ACTIVE
version: 4.44
owner_skill: ls-framework-compliance
---

# Platform registry (LocalSetup)

**Purpose:** Human-readable summary of the AI client variants LocalSetup supports and the canonical registration sources for skills and workflow packages.

**Manifest source:** `ls/config/clients.yaml` is canonical. It records client families and distinct CLI/IDE variants, their researched native surfaces, LocalSetup state contracts, and compatibility projection eligibility. `ls/config/platforms.yaml` is generated from eligible compatible variants for installer consumers; do not edit it directly. The root `--tools` flag remains a compatibility alias for current `--platforms`.

## Platform examples

The following six entries are examples. The [generated platform adapter inventory](_generated/platform-adapters.md) lists every current installer selector. [Client integration metadata](CLIENT_INTEGRATION_METADATA.md) owns variant, personal-path, lifecycle, and qualification details. Catalog support does not establish functional host qualification; retained-only variants are not fresh-install selectors.

| ID | Display name | Repo adapter path | Managed package library |
|----|--------------|-------------------|-----------------------|
| codex | OpenAI Codex CLI | .agents/skills | ~/.local/share/localsetup/packages |
| claude-code | Claude Code CLI | .claude/skills | ~/.local/share/localsetup/packages |
| cursor | Cursor IDE | .agents/skills | ~/.local/share/localsetup/packages |
| kilo | Kilo CLI | .agents/skills | ~/.local/share/localsetup/packages |
| opencode | OpenCode CLI | .agents/skills | ~/.local/share/localsetup/packages |
| openclaw | OpenClaw CLI | .agents/skills | ~/.local/share/localsetup/packages |

The canonical registry also tracks researched variants that are not projected into the compatibility manifest. Update `clients.yaml`, its research evidence, and the "Skill registration (new skills)" section when adding one.

## Shared home library

LocalSetup installs selected skills and workflow packages to `~/.local/share/localsetup/packages`. Fresh global-only invocation without a selected or recorded target creates no adapters. Explicit `--tools` or `--platforms` selects fresh adapters; selector-free updates of a recorded target retain its validated clients, scope, and paths. See [updates using the default target](ADAPTER_OWNERSHIP.md#updates-using-the-default-target). Adapter directories are shared surfaces: LocalSetup owns the marker and managed package entries it records, not the whole directory by path shape. `--mode portable` creates managed copies instead of symlinks. Rollback uses `.localsetup/lock.json` and removes only managed paths recorded by that install. See [Adapter ownership](ADAPTER_OWNERSHIP.md).

## Skill registration (new skills)

Register capabilities in their owning metadata, then regenerate the shared catalogs. Paths below are relative to the source checkout, which contains `ls/`.

| Registration surface | Required update |
|----------------------|-----------------|
| `ls/skills/<name>/SKILL.md` | Name, description, version, and task-specific instructions |
| `ls/config/pack.yaml` | Applicable pack membership and skill taxonomy |
| `ls/tests/skill_smoke_commands.yaml` | Supported smoke command or explicit doc-only `N/A` |
| Generated catalogs | Run both documentation generators; validate catalog and package surfaces |

[SKILLS.md](SKILLS.md), [WORKFLOW_REGISTRY.md](WORKFLOW_REGISTRY.md), and the generated taxonomy provide the complete inventory. Each package's frontmatter owns its current description. Platform templates carry discovery guidance and distinct routing boundaries, not a second catalog. Do not add a row to every template, the docs index, or `ls-context` for each new skill. Add a documentation-index entry only when introducing a distinct maintained document.

**If you add a new platform:** update the canonical client registry and research evidence, regenerate its compatibility projection, and update this summary. Preserve platform-specific loader instructions and adapter ownership. Template adoption is separate from installing selected package adapters; the installer does not copy these context templates into target repositories.

## Workflow registration (new workflow packages)

When adding a new workflow package, create `ls/workflows/ls-workflow-<id>/SKILL.md` and `workflow.yaml`, register its workflow-pack membership in `ls/config/pack.yaml`, and regenerate the workflow catalogs from `workflow.yaml`. Keep task triggers in package metadata rather than repeating them across platform templates.

Use [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md) for the model and [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md) for the manifest contract.

## Reference

- LocalSetup CLI: target users run `localsetup plan|install|verify|rollback`; source contributors may run `ls/tools/localsetup.py --source-root . ...` from this checkout.
- Root wrapper: `./install --directory .` opens the interactive guided-choice wizard for global-only; use `--tools cursor,codex` or `--platforms cursor codex` to select adapters. The wizard shows `Enter number(s) | d details | b back | q quit | ? help` on prompts and explains each platform's adapter path, such as `.agents/skills` for Codex. Visual rendering is controlled with `--color auto|always|never`, `--no-color`, and `--glyphs auto|ascii|unicode`; plain text labels remain present for status meaning. Use `--target-directory /path/to/project` to attach selected adapters outside the source checkout. Automation must use `--non-interactive --yes`.
- Windows: WSL2-only. Native PowerShell installation surfaces are removed.
- Skills and rules (paths and model): [SKILLS_AND_RULES.md](SKILLS_AND_RULES.md).
- Release and publish are handled by this repo's automatic versioning hooks and GitHub workflow.
