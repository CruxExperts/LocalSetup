from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ls.core.docs import generate_alias_outputs
from ls.core.provenance import artifact_registry_entry
from ls.core.skills import skill_taxonomy_payload
from ls.core.workflows import workflow_catalog_payload

from .common import ARTIFACT_SOURCE_INPUTS, replace_managed_block, write_json, write_markdown


def write_artifact_registry(repo_root: Path, paths: list[Path]) -> None:
    registry_path = repo_root / "ls" / "docs" / "_generated" / "artifact-registry.json"
    existing = {}
    if registry_path.exists():
        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
            existing = {
                str(item.get("path")): item
                for item in payload.get("artifacts", [])
                if item.get("path") and (repo_root / str(item.get("path"))).exists()
            }
        except json.JSONDecodeError:
            existing = {}
    for path in sorted({p.resolve() for p in paths if p.exists()}, key=lambda p: p.as_posix()):
        rel = path.relative_to(repo_root).as_posix()
        artifact_type = "json" if path.suffix == ".json" else "markdown" if path.suffix == ".md" else path.suffix.lstrip(".")
        existing[rel] = artifact_registry_entry(
            repo_root,
            path,
            artifact_type=artifact_type,
            emitter="generate-docs",
            source_inputs=ARTIFACT_SOURCE_INPUTS,
            generated_commit_parent=True,
        )
    write_json(
        registry_path,
        {"schema_version": 1, "artifacts": [existing[key] for key in sorted(existing)]},
        repo_root,
        emitter="generate-docs",
    )


def write_skills_md(path: Path, major_minor: str, skills: list[dict[str, Any]], repo_root: Path) -> None:
    lines = [
        "---",
        "status: ACTIVE",
        f"version: {major_minor}",
        "owner_package: generate-docs",
        "---",
        "",
        "# Shipped skills catalog",
        "",
        "This page is generated from `ls/skills/*/SKILL.md`.",
        "",
        f"Total shipped skills: {len(skills)}",
        "",
        "| Skill ID | Class | Priority | Packs | Tags | Name | Version | Description |",
        "|---|---|---:|---|---|---|---|---|",
    ]

    for skill in skills:
        desc = str(skill["description"] or "").replace("|", "\\|")
        if not desc:
            desc = "No description in frontmatter."
        packs = ", ".join(f"`{pack}`" for pack in skill["packs"]) if skill["packs"] else "unassigned"
        tags = ", ".join(f"`{tag}`" for tag in skill["tags"]) if skill["tags"] else "n/a"
        lines.append(
            f"| `{skill['id']}` | `{skill['class']}` | {skill['sort_priority']} | {packs} | {tags} | `{skill['name']}` | `{skill['version'] or 'n/a'}` | {desc} |"
        )

    lines.append("")

    write_markdown(path, "\n".join(lines), repo_root)


def write_workflow_registry(path: Path, major_minor: str, workflows: list[dict[str, Any]], repo_root: Path) -> None:
    def doc_link(doc: str) -> str:
        if doc.startswith("localsetup://doc/"):
            target = doc.removeprefix("localsetup://doc/")
        elif doc.startswith("ls/docs/"):
            target = doc.removeprefix("ls/docs/")
        else:
            target = f"../../{doc}"
        return f"[{Path(doc).name}]({target})"

    lines = [
        "---",
        "status: ACTIVE",
        f"version: {major_minor}",
        "owner_package: generate-docs",
        "---",
        "",
        "# Workflow and module registry (LocalSetup)",
        "",
        "This page is generated from `ls/workflows/*/workflow.yaml`.",
        "",
        "For the framework rules, see [WORKFLOW_STANDARD.md](WORKFLOW_STANDARD.md).",
        "",
        "## Core",
        "",
        "| Name | Description | When to use | Impact review |",
        "|------|-------------|-------------|---------------|",
        "| Master rule / context | Always-loaded framework context | Always | No |",
        "| Skills index | List of capability skills and when to use | When discovering which skill to load | No |",
        "",
        "## Workflows",
        "",
        "| Workflow ID | Package | Name | Description | Aliases | Required skills | Primary docs/tools |",
        "|-------------|---------|------|-------------|---------|-----------------|--------------------|",
    ]
    for workflow in workflows:
        aliases = "; ".join(workflow["aliases"]) if workflow["aliases"] else "n/a"
        required_skills = "; ".join(f"`{name}`" for name in workflow["required_skills"]) or "n/a"
        docs = "; ".join(doc_link(str(doc)) for doc in workflow["required_docs"])
        tools = "; ".join(f"`{tool}`" for tool in workflow["required_tools"])
        primary = "; ".join(part for part in [docs, tools] if part) or "n/a"
        description = str(workflow["description"]).replace("|", "\\|") or "No description in frontmatter."
        lines.append(
            f"| `{workflow['id']}` | `{workflow['package']}` | {workflow['name']} | {description} | {aliases} | {required_skills} | {primary} |"
        )

    lines.extend(
        [
            "",
            "## Usage",
            "",
            "- Agents load the workflow package when a user invokes a workflow ID, package name, or alias.",
            "- Workflow packages install into the managed skill library because every package includes a valid `SKILL.md`.",
            "- Required skills listed in `workflow.yaml` are automatically selected when a workflow's pack is selected.",
            "- Historical publish workflow pointers are retired; use `ls-github-release-publishing` plus `ls-version-bump-commits`.",
            "",
        ]
    )
    write_markdown(path, "\n".join(lines), repo_root)


def write_workflow_quick_ref(path: Path, major_minor: str, workflows: list[dict[str, Any]], repo_root: Path) -> None:
    lines = [
        "---",
        "status: ACTIVE",
        f"version: {major_minor}",
        "owner_package: generate-docs",
        "---",
        "",
        "# Workflow quick reference",
        "",
        "This page is generated from `ls/workflows/*/workflow.yaml`.",
        "",
        "## Workflows",
        "",
        "| Workflow ID | Name | Aliases | Package | Required skills |",
        "|------------|------|---------|---------|-----------------|",
    ]
    for workflow in workflows:
        aliases = "; ".join(workflow["aliases"]) if workflow["aliases"] else "n/a"
        required_skills = "; ".join(f"`{name}`" for name in workflow["required_skills"]) or "n/a"
        lines.append(
            f"| `{workflow['id']}` | {workflow['name']} | {aliases} | `{workflow['package']}` | {required_skills} |"
        )
    lines.extend(["", "## Common Phrases", ""])
    for workflow in workflows:
        for alias in workflow["aliases"]:
            lines.append(f"- \"{alias}\" -> `{workflow['id']}`")
    lines.append("")
    write_markdown(path, "\n".join(lines), repo_root)


def write_facts_json(path: Path, facts: dict[str, Any], repo_root: Path) -> None:
    output = {k: v for k, v in facts.items() if k != "generated_at"}
    write_json(path, output, repo_root)


def write_workflow_catalog_json(path: Path, repo_root: Path) -> None:
    write_json(path, workflow_catalog_payload(repo_root), repo_root)


def write_skill_taxonomy_json(path: Path, repo_root: Path) -> None:
    write_json(path, skill_taxonomy_payload(repo_root), repo_root)


def write_internal_snapshot(path: Path, facts: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Internal docs snapshot",
        "",
        "Local-only generated report. Do not commit.",
        "",
        f"- generated_at: {facts['generated_at']}",
        f"- version: {facts['version']}",
        f"- platform_count: {facts['platform_count']}",
        f"- skill_count: {facts['skill_count']}",
        f"- workflow_count: {facts['workflow_count']}",
        "",
        "## Platforms",
    ]
    for platform in facts["platforms"]:
        lines.append(f"- {platform['id']}: {platform['display_name']}")
    lines.append("")
    lines.append("## Skills")
    for skill in facts["skills"]:
        lines.append(f"- {skill['id']}")
    lines.append("")
    lines.append("## Workflows")
    for workflow in facts["workflows"]:
        lines.append(f"- {workflow['id']}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def update_facts_blocks(repo_root: Path, facts: dict[str, Any]) -> None:
    platforms = ", ".join([platform["id"] for platform in facts["platforms"]])
    readme_block = "\n".join(
        [
            "| Fact | Value |",
            "|---|---|",
            f"| Current version | `{facts['version']}` |",
            f"| Supported platforms | `{platforms}` |",
            f"| Shipped skills | `{facts['skill_count']}` |",
            f"| Workflow packages | `{facts['workflow_count']}` |",
            "| Source | `ls/docs/_generated/facts.json` |",
        ]
    )
    docs_index_block = "\n".join(
        [
            f"- Current version: `{facts['version']}`",
            f"- Supported platforms: `{platforms}`",
            f"- Shipped skills: `{facts['skill_count']}`",
            f"- Workflow packages: `{facts['workflow_count']}`",
            "- Source: `ls/docs/_generated/facts.json`",
        ]
    )

    replace_managed_block(repo_root / "README.md", "facts-block", readme_block)
    replace_managed_block(repo_root / "ls" / "docs" / "README.md", "facts-block", docs_index_block)
    replace_managed_block(repo_root / "ls" / "docs" / "FEATURES.md", "facts-block", docs_index_block)


def generate_alias_output_paths(repo_root: Path) -> list[Path]:
    alias_outputs = generate_alias_outputs(repo_root)
    return [Path(output) for key, output in alias_outputs.items() if key != "count"]
