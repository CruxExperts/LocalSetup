from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from .audit import is_prunable_dead_url
from .audit import NO_LICENSE_DESCRIPTION_SOURCE_REGISTRIES


def apply_fixes(index_path: Path, results: list[dict], *, prune_dead_urls: bool = False) -> tuple[int, int]:
    """Write fetched descriptions and optional dead URL pruning back to the index."""
    with open(index_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    skills = data.get("skills", [])
    fix_map: dict[tuple[str, str], str] = {}
    dead_keys: set[tuple[str, str]] = set()
    for result in results:
        if (
            result["action"] == "fixable"
            and result["fetched_desc"]
            and result.get("source_registry") not in NO_LICENSE_DESCRIPTION_SOURCE_REGISTRIES
        ):
            fix_map[(result["name"], result["url"])] = result["fetched_desc"]
        if prune_dead_urls and is_prunable_dead_url(result):
            dead_keys.add((result["name"], result["url"]))

    updated_count = 0
    pruned_count = 0
    kept_skills = []
    for skill in skills:
        name = skill.get("name", "")
        url = skill.get("url", "")
        key = (name, url)
        if key in dead_keys:
            pruned_count += 1
            continue
        if key in fix_map:
            new_desc = fix_map[key]
            skill["description"] = new_desc
            skill["summary_short"] = new_desc[:120]
            skill["summary_long"] = new_desc
            quality_signals = skill.get("quality_signals", {})
            quality_signals["has_description"] = True
            quality_signals["description_length"] = len(new_desc)
            skill["quality_signals"] = quality_signals
            updated_count += 1
        kept_skills.append(skill)

    if prune_dead_urls:
        data["skills"] = kept_skills
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(index_path, "w", encoding="utf-8") as f:
        f.write("# Public skill index - refresh periodically from PUBLIC_SKILL_REGISTRY.urls.\n")
        f.write("# Used by ls-find-public-skills to recommend similar public skills when\n")
        f.write("# the user is creating or importing a skill. Schema: sources, updated (ISO8601), skills.\n")
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False, width=1000)

    return updated_count, pruned_count
