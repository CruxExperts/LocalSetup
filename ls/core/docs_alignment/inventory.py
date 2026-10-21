from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from ls.core.sdk_payload.ownership import upstream_documents
from ls.core.manifests import load_pack_config, load_platforms
from ls.core.skills import load_skill_catalog, skill_taxonomy_payload
from ls.core.workflows import load_workflow_catalog, workflow_catalog_payload

from .assets import collect_asset_manifest
from .constants import GENERATED_DIR, SCHEMA_VERSION
from .io import _classify_doc, _frontmatter, _managed_blocks, _markdown_files, _read_text, _rel

def _cli_commands(repo_root: Path) -> list[str]:
    """Inventory root command declarations without importing the target repository."""
    source = repo_root / "ls/core/cli_parser.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    builders = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_parser"]
    if len(builders) != 1:
        raise ValueError("CLI inventory requires the canonical parser builder")
    commands = set()
    for node in ast.walk(builders[0]):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "sub"
                and node.func.attr == "add_parser"):
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise ValueError("CLI inventory requires literal root command names")
            commands.add(node.args[0].value)
    if not commands:
        raise ValueError("CLI inventory found no root commands; review parser ownership")
    return sorted(commands)


def collect_inventory(repo_root: Path) -> dict[str, Any]:
    docs = []
    upstream = upstream_documents(repo_root)
    for path in _markdown_files(repo_root):
        text = _read_text(path)
        fm = _frontmatter(text)
        docs.append(
            {
                "path": _rel(repo_root, path),
                "class": "upstream" if _rel(repo_root, path) in upstream else _classify_doc(repo_root, path),
                **({"upstream": upstream[_rel(repo_root, path)]} if _rel(repo_root, path) in upstream else {}),
                "status": str(fm.get("status", "")),
                "version": str(fm.get("version", "")),
                "owner_skill": str(fm.get("owner_skill", "")),
                "owner_package": str(fm.get("owner_package", "")),
                "managed_blocks": _managed_blocks(text),
                "has_frontmatter": bool(fm),
            }
        )

    pack = load_pack_config(repo_root)
    platforms = load_platforms(repo_root)
    skills = load_skill_catalog(repo_root)
    workflows = load_workflow_catalog(repo_root)
    commands = _cli_commands(repo_root)
    ci = sorted(_rel(repo_root, path) for path in (repo_root / ".github/workflows").glob("*.yml"))
    ci.extend(sorted(_rel(repo_root, path) for path in (repo_root / ".github/workflows").glob("*.yaml")))

    return {
        "schema_version": SCHEMA_VERSION,
        "repo": ".",
        "docs": docs,
        "counts": {
            "docs": len(docs),
            "upstream_docs": sum(1 for row in docs if row["class"] == "upstream"),
            "public_docs": sum(1 for row in docs if row["class"] == "public"),
            "framework_docs": sum(1 for row in docs if row["class"] == "framework"),
            "generated_docs": sum(1 for row in docs if row["class"] == "generated"),
            "skills": len(skills),
            "workflows": len(workflows),
            "platforms": len(platforms),
            "workflow_packs": sum(len(items) for items in pack.workflow_packs.values()),
        },
        "skills": [
            {
                "name": skill.name,
                "path": _rel(repo_root, skill.path),
                "packs": skill.packs,
                "class": skill.taxonomy_class,
                "sort_priority": skill.sort_priority,
                "tags": skill.tags,
                "owner_scope": skill.owner_scope,
            }
            for skill in skills
        ],
        "workflows": [
            {
                "package": workflow.package,
                "workflow_id": workflow.workflow_id,
                "path": _rel(repo_root, workflow.path),
                "packs": workflow.packs,
            }
            for workflow in workflows
        ],
        "platforms": [{"id": platform.platform_id, "repo_paths": platform.repo_paths} for platform in platforms],
        "assets": collect_asset_manifest(repo_root)["assets"],
        "ci_workflows": ci,
        "cli_commands": commands,
    }


def collect_truth_map(repo_root: Path) -> dict[str, Any]:
    version = _read_text(repo_root / "VERSION").strip()
    pyproject = _read_text(repo_root / "pyproject.toml") if (repo_root / "pyproject.toml").exists() else ""
    pyproject_version = ""
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    if match:
        pyproject_version = match.group(1)
    facts_path = repo_root / GENERATED_DIR / "facts.json"
    facts = json.loads(_read_text(facts_path)) if facts_path.exists() else {}
    taxonomy_path = repo_root / GENERATED_DIR / "skill-taxonomy.json"
    generated_skill_taxonomy = json.loads(_read_text(taxonomy_path)) if taxonomy_path.exists() else {}
    platforms = load_platforms(repo_root)
    skills = load_skill_catalog(repo_root)
    workflows = load_workflow_catalog(repo_root)
    active_doc_owners = [
        {
            "path": row["path"],
            "owner_skill": row["owner_skill"],
            "owner_package": row["owner_package"],
        }
        for row in collect_inventory(repo_root)["docs"]
        if row["class"] == "framework" and row["status"] == "ACTIVE"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "truths": {
            "version": {
                "value": version,
                "sources": ["VERSION", "pyproject.toml", "ls/docs/_generated/facts.json"],
                "pyproject_version": pyproject_version,
                "facts_version": facts.get("version", ""),
            },
            "major_minor": {
                "value": ".".join(version.split(".")[:2]) if "." in version else version,
                "sources": ["VERSION"],
            },
            "platform_count": {
                "value": len(platforms),
                "sources": ["ls/config/platforms.yaml"],
            },
            "skill_count": {
                "value": len(skills),
                "sources": ["ls/skills/ls-*/SKILL.md", "ls/config/pack.yaml"],
            },
            "skill_taxonomy": {
                "value": skill_taxonomy_payload(repo_root),
                "sources": ["ls/config/pack.yaml", "ls/skills/ls-*/SKILL.md"],
            },
            "workflow_count": {
                "value": len(workflows),
                "sources": ["ls/workflows/ls-*/workflow.yaml"],
            },
            "workflow_catalog": {
                "value": workflow_catalog_payload(repo_root),
                "sources": ["ls/workflows/*/workflow.yaml"],
            },
            "active_doc_owners": {
                "value": active_doc_owners,
                "sources": ["ls/docs/**/*.md frontmatter"],
            },
        },
        "generated_facts": facts,
        "generated_skill_taxonomy": generated_skill_taxonomy,
    }
