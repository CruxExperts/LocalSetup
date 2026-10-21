from __future__ import annotations

from pathlib import Path
from typing import Any

from .manifests import load_pack_config
from .selection import has_container_project, recommended_packs_for_target
from .skills import load_skill_catalog, parse_skill_frontmatter
from .workflows import load_workflow_catalog


def skill_payload(repo_root: Path, query: str | None = None) -> dict[str, Any]:
    rows = []
    for skill in load_skill_catalog(repo_root):
        if query and query.lower() not in f"{skill.name} {skill.description}".lower():
            continue
        frontmatter = parse_skill_frontmatter(skill.path / "SKILL.md")
        rows.append(
            {
                "name": skill.name,
                "description": skill.description,
                "class": skill.taxonomy_class,
                "sort_priority": skill.sort_priority,
                "tags": skill.tags,
                "owner_scope": skill.owner_scope,
                "packs": skill.packs,
                "path": str(skill.path.relative_to(repo_root)),
                "risk": frontmatter.get("risk", "low"),
                "permissions": frontmatter.get("permissions", []),
            }
        )
    return {"count": len(rows), "skills": rows}


def workflow_payload(repo_root: Path, query: str | None = None) -> dict[str, Any]:
    rows = []
    for workflow in load_workflow_catalog(repo_root):
        haystack = f"{workflow.package} {workflow.workflow_id} {workflow.display_name} {workflow.description} {' '.join(workflow.aliases)}"
        if query and query.lower() not in haystack.lower():
            continue
        rows.append(
            {
                "package": workflow.package,
                "workflow_id": workflow.workflow_id,
                "display_name": workflow.display_name,
                "description": workflow.description,
                "aliases": workflow.aliases,
                "required_skills": workflow.required_skills,
                "packs": workflow.packs,
                "path": str(workflow.path.relative_to(repo_root)),
            }
        )
    return {"count": len(rows), "workflows": rows}


def pack_reasoning(repo_root: Path, packs: list[str] | None = None) -> dict[str, Any]:
    pack = load_pack_config(repo_root)
    selected = packs or ["core"]
    return {
        "packs": [
            {
                "pack": name,
                "skills": pack.packs.get(name, []),
                "workflows": pack.workflow_packs.get(name, []),
                "reason": "selected explicitly" if packs else "default pack",
            }
            for name in selected
        ]
    }


def graph_payload(repo_root: Path) -> dict[str, Any]:
    pack = load_pack_config(repo_root)
    edges = []
    for pack_name, skills in pack.packs.items():
        edges.extend({"from": pack_name, "to": skill, "type": "pack_skill"} for skill in skills)
    for pack_name, workflows in pack.workflow_packs.items():
        edges.extend({"from": pack_name, "to": workflow, "type": "pack_workflow"} for workflow in workflows)
    for workflow in load_workflow_catalog(repo_root):
        edges.extend({"from": workflow.package, "to": skill, "type": "workflow_requires_skill"} for skill in workflow.required_skills)
    return {"edges": edges}


def adopt_recommendations(target_root: Path) -> dict[str, Any]:
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
    return {"target_root": str(target_root), "signals": signals, "recommended_packs": recommended_packs_for_target(target_root)}
