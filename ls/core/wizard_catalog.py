"""Wizard choice catalogs and prior-state loading."""

from __future__ import annotations

from pathlib import Path

from .adapters import legacy_global_roots, remove_managed_adapter_entries
from .lockfile import load_json
from .manifests import load_pack_config, load_platforms
from .paths import expand_user_path, legacy_target_lockfile_path
from .registry import load_registry
from .selection import recommended_packs_for_target, resolve_package_selection
from .skills import load_skill_catalog
from .wizard_models import PLATFORM_LABELS, Choice, WizardState

def _platform_choices(repo_root: Path) -> list[Choice]:
    choices: list[Choice] = []
    for platform in load_platforms(repo_root):
        label = PLATFORM_LABELS.get(platform.platform_id, platform.platform_id)
        repo_paths = ", ".join(platform.repo_paths) if platform.repo_paths else "no repo adapter path"
        global_paths = ", ".join(platform.global_paths) if platform.global_paths else "no global adapter path"
        choices.append(
            Choice(
                value=platform.platform_id,
                label=label,
                summary=f"Writes adapter path {repo_paths}.",
                effect=f"Connects this repo to LocalSetup skills for {label} through {repo_paths}.",
                best_for=f"You use {label} in this repository and want its agent skill picker to see LocalSetup.",
                tradeoff=f"Creates or updates repo-local adapter path(s); global fallback is {global_paths}.",
            )
        )
    return choices

def _pack_choices(repo_root: Path) -> list[Choice]:
    pack = load_pack_config(repo_root)
    names = list(dict.fromkeys(["core", *pack.optional_packs, *pack.packs.keys()]))
    metadata = {
        "core": (
            "Everyday LocalSetup context, safety, task matching, and test workflow basics.",
            "Installs the compact starter set for interactive agent work.",
            "You want a smaller baseline than the normal fresh-install profile.",
            "Keeps the install compact; specialized ops, publishing, and integrations stay out until selected.",
        ),
        "bootstrap": (
            "Agent-team startup, repo audit, safety, docs, git, and testing pack.",
            "Adds the skills most useful when bringing a repo under agent-team workflow control.",
            "You are preparing a repo for structured controller-led work or first-pass audits.",
            "Overlaps with core and dev, so it is a little broader than a minimalist install.",
        ),
        "dev": (
            "Code, docs, git, testing, markdown validation, and repo repair workflows.",
            "Adds developer-maintenance skills for day-to-day implementation and cleanup.",
            "You will edit, test, audit, or repair this repository with agents.",
            "Adds more repo-maintenance surface than a simple end-user setup needs.",
        ),
        "ops": (
            "Server, cron, Linux service, Ansible, patching, and baseline diagnostics workflows.",
            "Installs operational skills for maintaining machines and services.",
            "This repo manages infrastructure, servers, scheduled work, or service triage.",
            "Not needed for most app-only repositories.",
        ),
        "integrations": (
            "External systems and service connectors such as DNS, mail, secrets, MCP, NPM, and scraping.",
            "Adds skills that talk to outside services or local credential-backed systems.",
            "You expect agents to work with integrations after setup.",
            "Some workflows may require credentials or extra host tools before use.",
        ),
        "frontend": (
            "Frontend, UI, accessibility, design, React, Next.js, Tailwind, and browser-debugging skills.",
            "Adds focused web-interface and frontend implementation guidance.",
            "You expect agents to build, review, or debug frontend experiences.",
            "Adds UI-specific guidance that may be unnecessary for backend-only repositories.",
        ),
        "architecture": (
            "Architecture, system design, diagrams, deploy readiness, incident response, and tech-debt planning.",
            "Adds planning and review skills for larger technical decisions and operational readiness.",
            "You need design tradeoff support, diagrams, or release/incident planning.",
            "Adds broader planning workflows that may be more than a small script repo needs.",
        ),
        "publishing": (
            "Release, public repo identity, PR review, GitHub publishing, and automatic versioning support.",
            "Installs skills for publishing and release hygiene.",
            "You plan to ship changes, prepare public docs, or manage release flow.",
            "Adds release-process opinions that are unnecessary for private scratch repos.",
        ),
        "harness": (
            "Opt-in autonomous harness capability for Codex heartbeat checks.",
            "Installs the heartbeat skill and workflow only; activation still requires explicit harness commands.",
            "You want a target repo to support scheduled heartbeat runs after a deliberate enable step.",
            "Does not create config, cron entries, or state during normal install.",
        ),
        "skill-lifecycle": (
            "Skill authoring, discovery, import, normalization, vetting, sandbox testing, and bundle inventories.",
            "Adds the skill maintenance pipeline and upstream skill bundle wrappers.",
            "You maintain or import LocalSetup/Agent Skills content.",
            "Not usually needed for ordinary application development.",
        ),
        "growth-content": (
            "Marketing, CRO, SEO/GEO, lifecycle email, deliverability, and writing/editing support.",
            "Adds product-growth and content workflow guidance.",
            "You work on conversion, content, email, SEO, or messaging tasks.",
            "Can add non-engineering guidance that is irrelevant for infrastructure-only repos.",
        ),
        "specialized": (
            "Specialized human-review, writing, Kilo output, and umbrella workflow support.",
            "Adds narrow skills for specialized agent orchestration and review cases.",
            "You use those specialized agent/operator workflows.",
            "Narrow scope; review the pack contents before using as a broad default.",
        ),
        "experimental": (
            "Reserved empty pack for future incubating skills.",
            "Currently installs no skills or workflows.",
            "Only useful for compatibility with older selectors.",
            "Prefer a concrete pack such as frontend, architecture, skill-lifecycle, growth-content, or specialized.",
        ),
    }
    out: list[Choice] = []
    for name in names:
        summary, effect, best_for, tradeoff = metadata.get(
            name,
            (
                f"Installs the {name} pack.",
                f"Adds skills and workflows listed under {name} in pack.yaml.",
                f"You need the {name} capability set.",
                "Review the pack contents if you are unsure.",
            ),
        )
        out.append(Choice(name, name, summary, effect, best_for, tradeoff))
    return out

def _skill_class_choices(repo_root: Path) -> list[Choice]:
    classes = sorted({skill.taxonomy_class for skill in load_skill_catalog(repo_root) if skill.taxonomy_class})
    return [
        Choice(
            value=name,
            label=name,
            summary=f"Adds all skills classified as {name}.",
            effect=f"Includes every shipped skill with taxonomy class {name}.",
            best_for=f"You want the {name} capability group without picking each skill.",
            tradeoff="May add skills outside the selected packs.",
        )
        for name in classes
    ]

def _skill_tag_choices(repo_root: Path) -> list[Choice]:
    tags = sorted({tag for skill in load_skill_catalog(repo_root) for tag in skill.tags})
    return [
        Choice(
            value=tag,
            label=tag,
            summary=f"Adds skills tagged {tag}.",
            effect=f"Includes shipped skills whose taxonomy tags include {tag}.",
            best_for=f"You need the {tag} capability regardless of pack.",
            tradeoff="Tags are additive; review individual skills on the next screen.",
        )
        for tag in tags
    ]

def _skill_choices(repo_root: Path) -> list[Choice]:
    return [
        Choice(
            value=skill.name,
            label=skill.name,
            summary=f"{skill.taxonomy_class}; packs: {', '.join(skill.packs) or 'none'}; tags: {', '.join(skill.tags) or 'none'}.",
            effect=skill.description,
            best_for=f"You want {skill.name} available in the selected install footprint.",
            tradeoff="Unselecting a workflow-required skill may be overridden by the workflow dependency.",
        )
        for skill in load_skill_catalog(repo_root)
    ]

def _attach_choices() -> list[Choice]:
    return [
        Choice(
            "symlink",
            "Symlink adapters",
            "Repo adapter paths point at the managed LocalSetup library.",
            "Creates links such as `.agents/skills` so updates to the managed library are picked up immediately.",
            "You want the easiest update path and are comfortable with repo-local symlinks.",
            "The repo depends on the managed library path existing on this machine.",
        ),
        Choice(
            "portable",
            "Portable adapter copies",
            "Repo adapter paths get their own copied skill tree.",
            "Copies selected skills into adapter paths so the repo is more self-contained.",
            "You need fewer links to machine-global locations or plan to move the repo around.",
            "Updates require copying again, and the repo uses more disk space.",
        ),
    ]

def _dependency_choices() -> list[Choice]:
    return [
        Choice(
            "prompt-only",
            "Prompt only dependencies",
            "Reports missing dependencies without installing them.",
            "Leaves host dependency installation to you while still showing what is needed.",
            "You want the safest no-surprises install path.",
            "Some workflows may need manual dependency setup before they run.",
        ),
        Choice(
            "uv-sync",
            "Sync uv environment",
            "Prepares LocalSetup's uv-managed Python environment.",
            "Creates or updates the source checkout .venv from pyproject.toml and uv.lock.",
            "You want the installer to prepare Python tooling now.",
            "Takes longer and requires uv plus access to the configured package index or cache.",
        ),
    ]

def _global_root(repo_root: Path, home: Path) -> Path:
    return expand_user_path(load_pack_config(repo_root).global_root, home)

def _normal_pack_defaults(repo_root: Path, target_root: Path) -> list[str]:
    return resolve_package_selection(repo_root, preset="normal", target_root=target_root).packs

def _wizard_legacy_selector_present(state: WizardState) -> bool:
    return any(
        value is not None
        for value in (
            state.packs,
            state.preset,
            state.skills,
            state.skill_classes,
            state.skill_tags,
            state.exclude_skills,
            state.workflows,
        )
    )

def _wizard_global_selector_present(state: WizardState) -> bool:
    return any(
        value is not None
        for value in (
            state.global_packs,
            state.global_preset,
            state.global_skills,
            state.global_skill_classes,
            state.global_skill_tags,
            state.global_exclude_skills,
            state.global_workflows,
        )
    )

def _use_normal_global_fallback(state: WizardState) -> bool:
    return not _wizard_legacy_selector_present(state) and not _wizard_global_selector_present(state)

def _global_pack_defaults(state: WizardState) -> tuple[list[str], str | None]:
    if state.global_packs is not None:
        return state.global_packs, state.global_preset
    if state.packs is not None:
        return state.packs, state.global_preset
    if _use_normal_global_fallback(state):
        return _normal_pack_defaults(state.repo_root, state.caller_directory), "normal"

    selection = resolve_package_selection(
        state.repo_root,
        preset=state.global_preset if state.global_preset is not None else state.preset,
        skills=state.global_skills if state.global_skills is not None else state.skills,
        workflows=state.global_workflows if state.global_workflows is not None else state.workflows,
        skill_classes=state.global_skill_classes if state.global_skill_classes is not None else state.skill_classes,
        skill_tags=state.global_skill_tags if state.global_skill_tags is not None else state.skill_tags,
        exclude_skills=state.global_exclude_skills if state.global_exclude_skills is not None else state.exclude_skills,
        target_root=state.caller_directory,
    )
    return selection.packs, state.global_preset

def _registry_path(repo_root: Path, home: Path) -> Path:
    return expand_user_path(load_pack_config(repo_root).global_registry, home)

def _source_identity(repo_root: Path) -> str:
    import subprocess

    try:
        tag = subprocess.run(
            ["git", "-C", str(repo_root), "describe", "--tags", "--exact-match"],
            text=True,
            capture_output=True,
            check=False,
        )
        if tag.returncode == 0 and tag.stdout.strip():
            return tag.stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        if commit.returncode == 0 and commit.stdout.strip():
            return commit.stdout.strip()
    except OSError:
        pass
    return "unknown"

def _selector_payload(selectors: object) -> dict:
    return selectors if isinstance(selectors, dict) else {}

def _load_prior_defaults(state: WizardState) -> None:
    global_defaults_loaded = False
    registry_path = _registry_path(state.repo_root, state.home)
    if registry_path.exists():
        baseline = load_registry(registry_path).get("global_baseline", {})
        selectors = _selector_payload(baseline.get("selectors"))
        global_defaults_loaded = bool(baseline or selectors)
        prior_packs = list(selectors.get("packs") or baseline.get("packs") or [])
        if prior_packs and state.global_packs is None and state.packs is None:
            state.global_packs = prior_packs
        if state.global_preset is None and state.preset is None:
            state.global_preset = selectors.get("preset") or baseline.get("preset")
        if state.global_skills is None and state.skills is None:
            state.global_skills = list(selectors.get("skills") or [])
        if state.global_workflows is None and state.workflows is None:
            state.global_workflows = list(selectors.get("workflows") or baseline.get("workflows") or [])
        if state.global_skill_classes is None and state.skill_classes is None:
            state.global_skill_classes = list(selectors.get("skill_classes") or [])
        if state.global_skill_tags is None and state.skill_tags is None:
            state.global_skill_tags = list(selectors.get("skill_tags") or [])
        if state.global_exclude_skills is None and state.exclude_skills is None:
            state.global_exclude_skills = list(selectors.get("exclude_skills") or [])

    candidate = state.target_directory or state.caller_directory
    lock_path = candidate / ".localsetup" / "lock.json"
    lock = load_json(lock_path)
    if not lock:
        lock = load_json(legacy_target_lockfile_path(candidate))
    if not lock:
        return
    state.prior_target_directory = candidate
    state.prior_adapter_targets = list(lock.get("adapter_targets") or [])
    if state.target_directory is None:
        target_value = lock.get("target_root")
        state.target_directory = Path(str(target_value)).expanduser().resolve() if target_value else candidate
        state.prior_target_directory = state.target_directory
    if state.platforms is None:
        state.platforms = list(lock.get("platforms") or [])
    if state.attach_mode == "symlink":
        state.attach_mode = str(lock.get("attach_mode") or state.attach_mode)
    if state.dependency_mode == "prompt-only" and lock.get("dependency_mode"):
        state.dependency_mode = str(lock["dependency_mode"])
    if not global_defaults_loaded:
        baseline_selectors = _selector_payload(lock.get("global_baseline_selectors") or lock.get("selectors"))
        prior_global_packs = list(
            baseline_selectors.get("packs")
            or lock.get("global_baseline_packs")
            or lock.get("packs")
            or []
        )
        if prior_global_packs and state.global_packs is None and state.packs is None:
            state.global_packs = prior_global_packs
        if state.global_preset is None and state.preset is None:
            state.global_preset = baseline_selectors.get("preset") or lock.get("global_baseline_preset") or lock.get("preset")
        if state.global_skills is None and state.skills is None:
            state.global_skills = list(baseline_selectors.get("skills") or lock.get("global_baseline_skills") or [])
        if state.global_workflows is None and state.workflows is None:
            state.global_workflows = list(
                baseline_selectors.get("workflows") or lock.get("global_baseline_workflows") or []
            )
        if state.global_skill_classes is None and state.skill_classes is None:
            state.global_skill_classes = list(baseline_selectors.get("skill_classes") or [])
        if state.global_skill_tags is None and state.skill_tags is None:
            state.global_skill_tags = list(baseline_selectors.get("skill_tags") or [])
        if state.global_exclude_skills is None and state.exclude_skills is None:
            state.global_exclude_skills = list(baseline_selectors.get("exclude_skills") or [])
    selectors = _selector_payload(lock.get("repo_selectors") or lock.get("selectors"))
    prior_repo_packs = list(selectors.get("packs") or lock.get("repo_packs") or [])
    if prior_repo_packs and state.repo_packs is None and state.packs is None:
        state.repo_packs = prior_repo_packs
    if state.repo_preset is None and state.preset is None:
        state.repo_preset = selectors.get("preset") or lock.get("repo_preset")
    if state.repo_skills is None and state.skills is None:
        state.repo_skills = list(selectors.get("skills") or [])
    if state.repo_workflows is None and state.workflows is None:
        state.repo_workflows = list(selectors.get("workflows") or lock.get("repo_workflows") or [])
    if state.repo_skill_classes is None and state.skill_classes is None:
        state.repo_skill_classes = list(selectors.get("skill_classes") or [])
    if state.repo_skill_tags is None and state.skill_tags is None:
        state.repo_skill_tags = list(selectors.get("skill_tags") or [])
    if state.repo_exclude_skills is None and state.exclude_skills is None:
        state.repo_exclude_skills = list(selectors.get("exclude_skills") or [])

def _action_summary(actions: list[object]) -> list[str]:
    labels = {
        "ensure_dir": "Ensure managed skill library exists",
        "write_registry": "Write LocalSetup registry",
        "install_skills": "Install selected skills",
        "install_workflows": "Install selected workflows",
        "attach_repo_path": "Attach selected adapter",
    }
    return [f"{labels.get(getattr(action, 'kind'), getattr(action, 'kind'))}: {getattr(action, 'path')}" for action in actions]

def _detach_prior_adapters(state: WizardState) -> list[str]:
    if not state.prior_adapter_targets:
        return []
    target_root = state.prior_target_directory or state.target_directory
    if target_root is None:
        return []
    resolved_target = target_root.resolve(strict=False)
    global_root = _global_root(state.repo_root, state.home)
    removed: list[str] = []
    for target in state.prior_adapter_targets:
        path_value = target.get("path") if isinstance(target, dict) else None
        if not path_value:
            continue
        path = Path(str(path_value))
        if not path.is_absolute() and state.prior_target_directory:
            path = state.prior_target_directory / path
        try:
            path.parent.resolve(strict=False).relative_to(resolved_target)
        except ValueError:
            continue
        if not (path.exists() or path.is_symlink()):
            continue
        removed.extend(
            remove_managed_adapter_entries(
                path,
                global_root,
                known_global_roots=legacy_global_roots(state.home),
                recorded_packages=target.get("packages") if isinstance(target, dict) else None,
            )
        )
    return removed
