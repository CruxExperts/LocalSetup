"""Bounded common configuration, explicit precedence, and effective-value origins."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat

from ..paths import global_layout


REPO_FILE = "localsetup.json"
REPO_SECTIONS = {"installation", "testing", "context_index"}
REPO_INSTALL = {"repo_packs", "repo_preset", "repo_skills", "repo_skill_classes",
                "repo_skill_tags", "repo_exclude_skills", "repo_workflows"}


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def paths(home: Path, project: Path) -> tuple[Path, Path]:
    return global_layout(home).localsetup_home / "config/framework.json", project / REPO_FILE


def read_document(path: Path) -> dict:
    from ..application_profiles.service import _bounded_json
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("Framework configuration must be a bounded regular JSON file")
        value = _bounded_json(stream.read(1024 * 1024 + 1), label="framework configuration", limit=1024 * 1024)
    if not isinstance(value, dict):
        raise ValueError("Framework configuration must be an object")
    return value


def _check_user_owner(path: Path, home: Path):
    """Auto-loaded user preferences must not be redirected by another account."""
    if not path.is_relative_to(home):
        raise ValueError("User configuration must remain under its owning home")
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("User configuration and its home-relative parents must be owned and not group/world writable")
        if part == home:
            break


def _origins(value, source, output, prefix=""):
    for key, item in value.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
            _origins(item, source, output, dotted)
        else:
            output[dotted] = source


def resolve(project: Path, home: Path, *, source_root: Path | None = None,
            explicit: Path | None = None, include_repo: bool = True) -> dict:
    source = source_root or Path(__file__).resolve().parents[3]
    effective, origins, loaded = {}, {}, []
    for path, scope in [(paths(home, project)[0], "user"),
                        *([(paths(home, project)[1], "repo")] if include_repo else []),
                        *(([(explicit, "invocation")]) if explicit is not None else [])]:
        if not path.exists() and not path.is_symlink():
            if scope == "invocation":
                raise ValueError("Explicit framework configuration does not exist")
            continue
        document = read_document(path)
        if scope == "user":
            _check_user_owner(path, home)
        from .validation import validate_document
        validate_document(document, source, scope=scope)
        if scope == "repo":
            selected = set(document) - {"$schema", "schema_version", "preset"}
            install = document.get("installation", {})
            if selected - REPO_SECTIONS or set(install) - REPO_INSTALL:
                raise ValueError("Repository configuration cannot override user-level paths, trust or protected domain settings")
        preset = document.get("preset")
        if preset:
            presets = json.loads((source / "ls/config/framework-presets.json").read_text())
            if preset not in presets:
                raise ValueError(f"Unknown framework preset: {preset}")
            # Presets are preferences, subject to the same validation and scope.
            if set(presets[preset]) - REPO_SECTIONS:
                raise ValueError("Framework preset contains a protected domain")
            effective = merge(effective, presets[preset])
            _origins(presets[preset], f"preset:{preset}", origins)
        settings = {k: v for k, v in document.items() if k not in {"$schema", "schema_version", "preset"}}
        effective = merge(effective, settings)
        _origins(settings, str(path), origins)
        loaded.append({"path": str(path), "scope": scope})
    if effective:
        from .validation import validate_document
        validate_document({"schema_version": 1, **effective}, source, scope="effective")
    return {"schema_version": 1, "settings": effective, "origins": origins, "loaded": loaded}


def section(name: str, project: Path, home: Path, *, source_root: Path | None = None):
    return resolve(project, home, source_root=source_root)["settings"].get(name)


def legacy_merge(common: dict | None, legacy: dict | None, *, domain: str) -> dict | None:
    if common is None:
        return legacy
    def conflicts(left, right, prefix=""):
        result = []
        for key in left.keys() & right.keys():
            field = f"{prefix}.{key}" if prefix else key
            if isinstance(left[key], dict) and isinstance(right[key], dict):
                result.extend(conflicts(left[key], right[key], field))
            elif left[key] != right[key]:
                result.append(field)
        return result
    overlap = conflicts(common, legacy or {})
    if overlap:
        raise ValueError(f"Conflicting legacy {domain} settings: {', '.join(sorted(overlap))}; migrate them to the common configuration")
    return merge(legacy or {}, common)
