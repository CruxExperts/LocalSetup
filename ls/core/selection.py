from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .aliases import collect_skill_aliases
from .dependency_ledger import required_skill_closure
from .manifests import load_pack_config
from .skills import load_skill_catalog
from .workflows import load_workflow_catalog, required_skills_for_workflows, selected_workflow_names


PRESETS = {"core", "normal", "suggested", "all", "custom"}
PROFILE_PRESETS = {"normal"}


@dataclass(frozen=True)
class PackageSelection:
    preset: str
    packs: list[str]
    skills: list[str]
    workflows: list[str]
    selectors: dict[str, list[str] | str | None] = field(default_factory=dict)

    @property
    def packages(self) -> list[str]:
        return sorted(set([*self.skills, *self.workflows]))


def has_container_project(target_root: Path) -> bool:
    """Recognize project files without selecting an engine or privilege mode."""
    patterns = (
        "Dockerfile", "Dockerfile.*", "*.Dockerfile",
        "Containerfile", "Containerfile.*", "*.Containerfile",
        "compose.yml", "compose.yaml", "compose.*.yml", "compose.*.yaml",
        "docker-compose.yml", "docker-compose.yaml",
        "docker-compose.*.yml", "docker-compose.*.yaml",
    )
    return any(path.is_file() for pattern in patterns for path in target_root.glob(pattern))


def recommended_packs_for_target(target_root: Path) -> list[str]:
    signals = {
        "node": (target_root / "package.json").exists(),
        "python": (target_root / "pyproject.toml").exists() or (target_root / "uv.lock").exists(),
        "docker": has_container_project(target_root),
        "github_actions": (target_root / ".github" / "workflows").is_dir(),
        "ansible": any((target_root / name).exists() for name in ("ansible.cfg", "playbook.yml", "site.yml")),
        "terraform": any(target_root.glob("*.tf")),
        "nginx": any(target_root.glob("**/nginx*.conf")),
        "systemd": any(target_root.glob("**/*.service")),
    }
    packs = ["core"]
    if signals["node"] or signals["python"]:
        packs.append("dev")
    if signals["docker"] or signals["ansible"] or signals["terraform"] or signals["nginx"] or signals["systemd"]:
        packs.append("ops")
    if signals["github_actions"]:
        packs.append("publishing")
    return sorted(set(packs))


def resolve_pack_names(
    repo_root: Path,
    requested_packs: list[str] | None,
    *,
    preset: str | None = None,
    target_root: Path | None = None,
) -> list[str]:
    pack = load_pack_config(repo_root)
    selected_preset = preset or "core"
    if selected_preset not in PRESETS:
        raise ValueError(f"unknown preset: {selected_preset}")
    if requested_packs is not None:
        names = requested_packs
    elif selected_preset == "all":
        names = list(pack.packs)
    elif selected_preset in pack.selection_profiles:
        names = list(pack.selection_profiles[selected_preset].get("packs", []))
    elif selected_preset == "suggested":
        names = recommended_packs_for_target(target_root or repo_root)
    elif selected_preset == "custom":
        names = []
    elif selected_preset in PROFILE_PRESETS:
        raise ValueError(f"preset is not configured: {selected_preset}")
    else:
        names = ["core"]
    unknown = [name for name in names if name not in pack.packs]
    if unknown:
        raise ValueError(f"unknown pack(s): {', '.join(sorted(unknown))}")
    return list(dict.fromkeys(names))


def _catalog_maps(repo_root: Path) -> tuple[dict[str, object], dict[str, str]]:
    by_name = {skill.name: skill for skill in load_skill_catalog(repo_root)}
    aliases = {name: name for name in by_name}
    aliases.update(collect_skill_aliases(repo_root / "ls" / "skills"))
    return by_name, aliases


def _normalize_skill_selectors(repo_root: Path, values: list[str] | None, field_name: str) -> list[str]:
    if not values:
        return []
    by_name, aliases = _catalog_maps(repo_root)
    out: list[str] = []
    unknown: list[str] = []
    for value in values:
        canonical = aliases.get(value)
        if canonical is None or canonical not in by_name:
            unknown.append(value)
        elif canonical not in out:
            out.append(canonical)
    if unknown:
        raise ValueError(f"unknown {field_name}: {', '.join(sorted(unknown))}")
    return out


def _normalize_workflow_selectors(repo_root: Path, values: list[str] | None, field_name: str) -> list[str]:
    if not values:
        return []
    aliases: dict[str, str] = {}
    for workflow in load_workflow_catalog(repo_root):
        aliases[workflow.package] = workflow.package
        if workflow.workflow_id:
            aliases[workflow.workflow_id] = workflow.package
        for alias in workflow.aliases:
            aliases[alias] = workflow.package
    out: list[str] = []
    unknown: list[str] = []
    for value in values:
        canonical = aliases.get(value)
        if canonical is None:
            unknown.append(value)
        elif canonical not in out:
            out.append(canonical)
    if unknown:
        raise ValueError(f"unknown {field_name}: {', '.join(sorted(unknown))}")
    return out


def _skills_for_classes(repo_root: Path, classes: list[str] | None) -> list[str]:
    if not classes:
        return []
    wanted = set(classes)
    known = {skill.taxonomy_class for skill in load_skill_catalog(repo_root)}
    unknown = sorted(wanted - known)
    if unknown:
        raise ValueError(f"unknown skill class(es): {', '.join(unknown)}")
    return [skill.name for skill in load_skill_catalog(repo_root) if skill.taxonomy_class in wanted]


def _skills_for_tags(repo_root: Path, tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    wanted = set(tags)
    known = {tag for skill in load_skill_catalog(repo_root) for tag in skill.tags}
    unknown = sorted(wanted - known)
    if unknown:
        raise ValueError(f"unknown skill tag(s): {', '.join(unknown)}")
    return [skill.name for skill in load_skill_catalog(repo_root) if wanted.intersection(skill.tags)]


def resolve_package_selection(
    repo_root: Path,
    *,
    packs: list[str] | None = None,
    preset: str | None = None,
    skills: list[str] | None = None,
    skill_classes: list[str] | None = None,
    skill_tags: list[str] | None = None,
    exclude_skills: list[str] | None = None,
    workflows: list[str] | None = None,
    target_root: Path | None = None,
) -> PackageSelection:
    selected_preset = preset or "core"
    pack_names = resolve_pack_names(repo_root, packs, preset=selected_preset, target_root=target_root)
    selected_workflows = list(dict.fromkeys([*selected_workflow_names(repo_root, pack_names), *_normalize_workflow_selectors(repo_root, workflows, "workflow selector")]))
    selected: set[str] = set()
    pack = load_pack_config(repo_root)
    for pack_name in pack_names:
        selected.update(pack.packs.get(pack_name, []))
    selected.update(_normalize_skill_selectors(repo_root, skills, "skill selector"))
    selected.update(_skills_for_classes(repo_root, skill_classes))
    selected.update(_skills_for_tags(repo_root, skill_tags))
    selected.difference_update(_normalize_skill_selectors(repo_root, exclude_skills, "excluded skill"))
    selected.update(required_skills_for_workflows(repo_root, selected_workflows))
    selected = set(required_skill_closure(repo_root, selected))

    sort_order = {skill.name: (skill.sort_priority, skill.name) for skill in load_skill_catalog(repo_root)}
    ordered_skills = sorted(selected, key=lambda name: sort_order.get(name, (1_000_000, name)))
    return PackageSelection(
        preset=selected_preset,
        packs=pack_names,
        skills=ordered_skills,
        workflows=selected_workflows,
        selectors={
            "preset": selected_preset,
            "packs": pack_names,
            "skills": skills or [],
            "workflows": workflows or [],
            "skill_classes": skill_classes or [],
            "skill_tags": skill_tags or [],
            "exclude_skills": exclude_skills or [],
        },
    )
