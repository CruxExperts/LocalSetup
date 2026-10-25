from __future__ import annotations

from pathlib import Path
from typing import Any

from .lockfile import load_json, save_json
from .personal_registry import record_personal_owners
from .provenance import load_package_marker, marker_public_snapshot
from .package_digests import legacy_package_digest
from .package_bindings import package_record_id


REGISTRY_VERSION = 2


def load_registry(path: Path) -> dict[str, Any]:
    data = load_json(path)
    if not data:
        return {"version": REGISTRY_VERSION, "managed_by": "localsetup", "targets": {}, "packages": {}}
    if int(data.get("version", 1)) < REGISTRY_VERSION:
        return {
            "version": REGISTRY_VERSION,
            "managed_by": data.get("managed_by", "localsetup"),
            "source_commit": data.get("source_commit"),
            "targets": {},
            "packages": {},
            "legacy": data,
        }
    data.setdefault("targets", {})
    data.setdefault("packages", {})
    return data


def upsert_target(
    registry_path: Path,
    *,
    target_root: Path,
    source_commit: str,
    package_paths: list[Path],
    adapter_targets: list[dict[str, Any]],
    global_baseline: dict[str, Any] | None = None,
    repo_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    target_id = str(target_root.resolve(strict=False))
    package_names = [package_record_id(path) for path in package_paths]
    current_packages = set(package_names)
    registry["source_commit"] = source_commit
    if adapter_targets:
        registry["canonical_package_root"] = adapter_targets[0].get("global_root")
    elif package_paths and not package_names[0].startswith("external:"):
        registry["canonical_package_root"] = str(package_paths[0].parent)
    elif package_paths:
        registry["canonical_package_root"] = str(package_paths[0].parents[3])
    if global_baseline is not None:
        registry["global_baseline"] = {
            "source_commit": source_commit,
            **global_baseline,
        }
    for package_name, package in list(registry.get("packages", {}).items()):
        if package_name in current_packages:
            continue
        refs = [str(ref) for ref in package.get("refs", []) if str(ref) != target_id]
        if refs:
            package["refs"] = sorted(refs)
        else:
            registry["packages"].pop(package_name, None)
    package_snapshots = {
        package_record_id(path): marker_public_snapshot(load_package_marker(path))
        for path in package_paths
    }
    registry["targets"][target_id] = {
        "target_root": str(target_root),
        "source_commit": source_commit,
        "packages": package_names,
        "package_provenance": package_snapshots,
        "adapters": adapter_targets,
        "global_baseline": global_baseline or {},
        "repo_selection": repo_selection or {},
        "lock_path": str(target_root / ".localsetup" / "lock.json"),
        "registry_path": str(registry_path),
    }
    for path in package_paths:
        package = registry["packages"].setdefault(package_record_id(path), {"path": str(path), "refs": [], "digest": None})
        package["name"] = path.name
        package["path"] = str(path)
        package["digest"] = legacy_package_digest(path)
        package["provenance"] = marker_public_snapshot(load_package_marker(path))
        refs = set(str(ref) for ref in package.get("refs", []))
        refs.add(target_id)
        package["refs"] = sorted(refs)
    record_personal_owners(registry, adapter_targets, {path.name for path in package_paths})
    save_json(registry_path, registry)
    return registry


def remove_target(registry_path: Path, *, target_root: Path) -> dict[str, Any]:
    registry = load_registry(registry_path)
    target_id = str(target_root.resolve(strict=False))
    registry.get("targets", {}).pop(target_id, None)
    for package_name, package in list(registry.get("packages", {}).items()):
        refs = [ref for ref in package.get("refs", []) if ref != target_id]
        if refs:
            package["refs"] = refs
        else:
            registry["packages"].pop(package_name, None)
    if registry.get("targets") or registry.get("packages") or registry.get("personal_owners"):
        save_json(registry_path, registry)
    elif registry_path.exists():
        registry_path.unlink()
    return registry


def package_has_other_refs(registry: dict[str, Any], package_name: str, *, target_root: Path) -> bool:
    target_id = str(target_root.resolve(strict=False))
    package = registry.get("packages", {}).get(package_name, {})
    return any(ref != target_id for ref in package.get("refs", []))
