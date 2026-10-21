from __future__ import annotations

from pathlib import Path
import re

from .manifests import _load_yaml


SKILL_NAME_RE = re.compile(r"^ls-[a-z0-9]+(?:-[a-z0-9]+)*$")


def legacy_skill_name(skill_name: str) -> str:
    return skill_name.replace("ls-", "localsetup-", 1) if skill_name.startswith("ls-") else skill_name


def skill_alias(skill_name: str) -> str:
    return skill_name.replace("localsetup-", "ls-", 1) if skill_name.startswith("localsetup-") else skill_name


def collect_skill_aliases(skills_root: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}
    current_skill_names: set[str] = set()
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        canonical_name = skill_alias(skill_dir.name)
        current_skill_names.add(canonical_name)
        aliases[legacy_skill_name(canonical_name)] = canonical_name

    pack_path = skills_root.parent / "config" / "pack.yaml"
    pack = _load_yaml(pack_path) if pack_path.exists() else {}
    policy = pack.get("alias_policy", {})
    if not isinstance(policy, dict):
        raise ValueError("alias_policy must be a mapping")
    renames = policy.get("skill_renames", {})
    if not isinstance(renames, dict):
        raise ValueError("alias_policy.skill_renames must be a mapping")
    for old, target in renames.items():
        if (not isinstance(old, str) or not SKILL_NAME_RE.fullmatch(old)
                or not isinstance(target, str) or not SKILL_NAME_RE.fullmatch(target)):
            raise ValueError(f"invalid skill rename: {old!r} -> {target!r}")
        if old in current_skill_names:
            raise ValueError(f"skill rename shadows a current package: {old}")
        if target in renames:
            raise ValueError(f"skill rename chain is not supported: {old} -> {target}")
        if target not in current_skill_names:
            raise ValueError(f"skill rename target is unavailable: {old} -> {target}")
        for alias in (old, legacy_skill_name(old)):
            if alias in current_skill_names or (alias in aliases and aliases[alias] != target):
                raise ValueError(f"skill rename alias collision: {alias}")
            aliases[alias] = target
    return aliases
