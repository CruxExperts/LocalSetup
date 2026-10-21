"""Shared helpers for LocalSetup repair orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .aliases import collect_package_aliases, skill_alias
from .lockfile import save_json
from .skills import load_skill_catalog
from .workflows import load_workflow_catalog

def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def _default_backup_root(target_root: Path) -> Path:
    return target_root / ".localsetup" / "backups" / f"repair-{_stamp()}"

def _read_json(path: Path, warnings: list[str], blockers: list[str], label: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        blockers.append(f"{label} is not readable JSON: {path}: {exc}")
        return {}
    if not isinstance(payload, dict):
        blockers.append(f"{label} is not a JSON object: {path}")
        return {}
    return payload

def _latest_version(source_root: Path) -> str | None:
    version_path = source_root / "VERSION"
    try:
        return version_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None

def _known_package_names(source_root: Path) -> set[str]:
    skill_names = {skill.name for skill in load_skill_catalog(source_root)}
    workflow_root = source_root / "ls" / "workflows"
    workflow_names = {path.name for path in workflow_root.iterdir() if path.is_dir()} if workflow_root.exists() else set()
    return skill_names | workflow_names

def _known_skill_names(source_root: Path) -> set[str]:
    return {skill.name for skill in load_skill_catalog(source_root)}

def _known_workflow_names(source_root: Path) -> set[str]:
    return {workflow.package for workflow in load_workflow_catalog(source_root)}

def _normalize_package_names(source_root: Path, values: list[str], decisions: list[dict]) -> dict[str, Any]:
    known = _known_skill_names(source_root)
    known_workflows = _known_workflow_names(source_root)
    known_packages = _known_package_names(source_root)
    aliases = collect_package_aliases(source_root)
    normalized: list[str] = []
    normalized_skills: list[str] = []
    normalized_workflows: list[str] = []
    unknown: list[str] = []
    evidence: list[dict[str, str]] = []
    for value in values:
        name = str(value).strip()
        if not name:
            continue
        canonical = aliases.get(name, name)
        if canonical not in known_packages:
            alias_candidate = aliases.get(skill_alias(name), skill_alias(name))
            canonical = alias_candidate if alias_candidate in known_packages else canonical
        if canonical in known_packages:
            if canonical not in normalized:
                normalized.append(canonical)
            if canonical in known and canonical not in normalized_skills:
                normalized_skills.append(canonical)
            if canonical in known_workflows and canonical not in normalized_workflows:
                normalized_workflows.append(canonical)
            evidence.append({"value": name, "canonical": canonical, "kind": "workflow" if canonical in known_workflows else "skill"})
        else:
            unknown.append(name)
    if unknown:
        decisions.append(
            {
                "kind": "package_selection",
                "code": "unknown_package_selection",
                "reason": "visible package selection contains unknown or unmanaged names",
                "values": sorted(set(unknown)),
                "required": "choose explicit repo package selectors before applying repair",
            }
        )
    return {
        "repo_packages": normalized,
        "repo_skills": normalized_skills,
        "repo_workflows": normalized_workflows,
        "package_evidence": evidence,
    }
