from __future__ import annotations

from pathlib import Path

from .aliases import collect_skill_aliases
from .adapters import adapter_targets, personal_adapter_targets, validate_platform_selectors
from .installation_ownership import resolve_skill_scope
from .manifests import load_pack_config, load_platforms
from .models import DeployPlan, PlanAction
from .client_registry.historical import HISTORICAL_ADAPTERS
from .paths import expand_user_path
from .selection import resolve_package_selection
from .skills import load_skill_catalog


def build_install_plan(
    repo_root: Path,
    home: Path,
    packs: list[str] | None = None,
    preset: str | None = None,
    skills: list[str] | None = None,
    skill_classes: list[str] | None = None,
    skill_tags: list[str] | None = None,
    exclude_skills: list[str] | None = None,
    workflows: list[str] | None = None,
    global_packs: list[str] | None = None,
    global_preset: str | None = None,
    global_skills: list[str] | None = None,
    global_skill_classes: list[str] | None = None,
    global_skill_tags: list[str] | None = None,
    global_exclude_skills: list[str] | None = None,
    global_workflows: list[str] | None = None,
    repo_packs: list[str] | None = None,
    repo_preset: str | None = None,
    repo_skills: list[str] | None = None,
    repo_skill_classes: list[str] | None = None,
    repo_skill_tags: list[str] | None = None,
    repo_exclude_skills: list[str] | None = None,
    repo_workflows: list[str] | None = None,
    attach_mode: str = "symlink",
    platform_ids: list[str] | None = None,
    target_root: Path | None = None,
    skill_scope: str | None = None,
) -> DeployPlan:
    if attach_mode not in {"symlink", "portable"}:
        raise ValueError(f"unsupported attach mode: {attach_mode}")

    pack = load_pack_config(repo_root)
    platforms = load_platforms(repo_root)
    validate_platform_selectors(repo_root, platform_ids)
    selected_ids = set(platform_ids or [])
    selected_platforms = [p for p in platforms if p.platform_id in selected_ids]
    if "hermes-agent" in selected_ids and attach_mode != "portable":
        raise ValueError("Hermes requires independent copies; select --mode portable")
    attachment_root = target_root or repo_root
    scope = resolve_skill_scope(attachment_root, skill_scope)
    global_root = expand_user_path(pack.global_root, home)

    legacy_selector_present = any(
        value is not None
        for value in (packs, preset, skills, skill_classes, skill_tags, exclude_skills, workflows)
    )
    global_selector_present = any(
        value is not None
        for value in (
            global_packs,
            global_preset,
            global_skills,
            global_skill_classes,
            global_skill_tags,
            global_exclude_skills,
            global_workflows,
        )
    )
    repo_selector_present = any(
        value is not None
        for value in (
            repo_packs,
            repo_preset,
            repo_skills,
            repo_skill_classes,
            repo_skill_tags,
            repo_exclude_skills,
            repo_workflows,
        )
    )
    global_selection_preset = global_preset if global_preset is not None else preset
    if not legacy_selector_present and not global_selector_present:
        global_selection_preset = "normal"

    global_selection = resolve_package_selection(
        repo_root,
        packs=global_packs if global_packs is not None else packs,
        preset=global_selection_preset,
        skills=global_skills if global_skills is not None else skills,
        skill_classes=global_skill_classes if global_skill_classes is not None else skill_classes,
        skill_tags=global_skill_tags if global_skill_tags is not None else skill_tags,
        exclude_skills=global_exclude_skills if global_exclude_skills is not None else exclude_skills,
        workflows=global_workflows if global_workflows is not None else workflows,
        target_root=attachment_root,
        home=home,
    )
    if selected_platforms:
        if repo_selector_present:
            repo_selection = resolve_package_selection(
                repo_root,
                packs=repo_packs if repo_packs is not None else packs,
                preset=repo_preset if repo_preset is not None else preset,
                skills=repo_skills if repo_skills is not None else skills,
                skill_classes=repo_skill_classes if repo_skill_classes is not None else skill_classes,
                skill_tags=repo_skill_tags if repo_skill_tags is not None else skill_tags,
                exclude_skills=repo_exclude_skills if repo_exclude_skills is not None else exclude_skills,
                workflows=repo_workflows if repo_workflows is not None else workflows,
                target_root=attachment_root,
                home=home,
            )
        elif legacy_selector_present:
            repo_selection = resolve_package_selection(
                repo_root,
                packs=packs,
                preset=preset,
                skills=skills,
                skill_classes=skill_classes,
                skill_tags=skill_tags,
                exclude_skills=exclude_skills,
                workflows=workflows,
                target_root=attachment_root,
                home=home,
            )
        else:
            repo_selection = resolve_package_selection(repo_root, target_root=attachment_root, home=home)
    else:
        repo_selection = resolve_package_selection(repo_root, preset="custom", target_root=attachment_root, home=home)

    from .skills_external import binding_artifact_path
    from .provenance_source import framework_version
    bindings = dict(global_selection.bindings)
    for name, binding in repo_selection.bindings.items():
        if name in bindings and bindings[name] != binding:
            raise ValueError(f"Conflicting custom skill origins for {name}")
        bindings[name] = binding
    for selection in (global_selection, repo_selection):
        for name in selection.packages:
            if name in bindings and name not in selection.bindings:
                raise ValueError(f"Framework and custom skill share visible name {name}")
    bindings = {name: {**binding, "artifact_path": str(binding_artifact_path(global_root, binding)),
                       "integrated_framework_version": framework_version(repo_root),
                       "disposition": "managed_projection"} for name, binding in bindings.items()}
    sort_order = {skill.name: (skill.sort_priority, skill.name) for skill in load_skill_catalog(repo_root)}
    selected_names = sorted(
        set([*global_selection.skills, *repo_selection.skills]),
        key=lambda name: sort_order.get(name, (1_000_000, name)),
    )
    selected_workflows = list(dict.fromkeys([*global_selection.workflows, *repo_selection.workflows]))
    legacy_aliases = collect_skill_aliases(repo_root / "ls" / "skills")
    selected_aliases = {alias: name for alias, name in legacy_aliases.items() if name in selected_names}
    actions: list[PlanAction] = [
        PlanAction("ensure_dir", global_root),
        PlanAction("write_registry", expand_user_path(pack.global_registry, home), {"pack_id": pack.pack_id}),
        PlanAction("install_skills", global_root, {"skills": selected_names, "package_bindings": bindings}),
    ]
    if selected_workflows:
        actions.append(PlanAction("install_workflows", global_root, {"workflows": selected_workflows}))
    if "codex" in selected_ids:
        codex_agents = ["guardian_subagent"]
        actions.append(PlanAction("install_codex_agents", home / ".codex" / "agents", {"agents": codex_agents}))
    else:
        codex_agents = []

    for platform_id in sorted(selected_ids if scope != "personal" else set()):
        for transition in HISTORICAL_ADAPTERS.get(platform_id, ()):
            actions.append(
                PlanAction(
                    "retire_historical_adapter",
                    attachment_root / transition["path"],
                    {
                        "id": transition["id"],
                        "platform": platform_id,
                        "replacement": str(attachment_root / transition["replacement"]),
                        "global_root": str(global_root),
                    },
                )
            )

    for target in adapter_targets(repo_root, home, platform_ids=platform_ids if scope != "personal" else [], target_root=attachment_root):
        actions.append(
            PlanAction(
                "attach_repo_path",
                target["repo_path"],
                {
                    "platform": target["platform"],
                    "platforms": target["platforms"],
                    "mode": attach_mode,
                    "global_root": str(global_root),
                    "packages": repo_selection.packages,
                    "package_bindings": {name: bindings[name] for name in repo_selection.bindings},
                    "verify_rules": target["verify_rules"],
                },
            )
        )

    if scope in {"personal", "both"}:
        for target in personal_adapter_targets(repo_root, home, platform_ids):
            actions.append(PlanAction("attach_personal_path", target["path"], {
                "platforms": target["platforms"], "owners": target["owners"],
                "mode": attach_mode, "global_root": str(global_root),
                "packages": repo_selection.packages,
                    "package_bindings": {name: bindings[name] for name in repo_selection.bindings},
            }))

    for action in actions:
        if action.kind in {"attach_repo_path", "attach_personal_path"}:
            action.details["package_bindings"] = {
                name: {**binding, "disposition": "external_present"}
                if Path(binding["source_path"]).absolute() == (action.path / name).absolute()
                else dict(binding)
                for name, binding in action.details.get("package_bindings", {}).items()
            }
        if "hermes-agent" in action.details.get("platforms", []):
            action.details["mutable_copy"] = True

    rollback = {
        "package_bindings": bindings,
        "skill_scope": scope,
        "created_paths": [str(global_root)],
        "repo_links": [str(a.path) for a in actions if a.kind == "attach_repo_path"],
        "preset": global_selection.preset,
        "packs": global_selection.packs,
        "selectors": global_selection.selectors,
        "global_baseline_selectors": global_selection.selectors,
        "global_baseline_packs": global_selection.packs,
        "global_baseline_skills": global_selection.skills,
        "global_baseline_workflows": global_selection.workflows,
        "global_baseline_packages": global_selection.packages,
        "repo_selectors": repo_selection.selectors,
        "repo_packs": repo_selection.packs,
        "repo_skills": repo_selection.skills,
        "repo_workflows": repo_selection.workflows,
        "repo_packages": repo_selection.packages,
        "platforms": [platform.platform_id for platform in selected_platforms],
        "target_root": str(attachment_root),
        "global_only": not selected_platforms,
        "attach_mode": attach_mode,
        "aliases": selected_aliases,
        "catalog_aliases": legacy_aliases,
        "skills": selected_names,
        "workflows": selected_workflows,
        "packages": sorted(set([*global_selection.packages, *repo_selection.packages])),
        "adapter_packages": repo_selection.packages,
        "codex_agents": codex_agents,
    }
    personal_paths = {a.path for a in actions if a.kind == 'attach_personal_path'}
    for action in actions:
        if action.kind == 'retire_historical_adapter' and action.path in personal_paths:
            action.details['disposition'] = 'delegated-current-personal'
    return DeployPlan(actions=actions, rollback_metadata=rollback)
