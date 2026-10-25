"""Resolve explicit custom package origins without relaxing adapter ownership."""
from __future__ import annotations

import json
from pathlib import Path

from .skills_external import binding_artifact_path


def package_record_id(path: Path) -> str:
    from .provenance import load_package_marker
    marker = load_package_marker(path) or {}
    external = marker.get("external_source")
    if external is None:
        return path.name
    if not isinstance(external, dict) or external.get("name") != path.name:
        raise ValueError("Invalid external package identity")
    # Validate identity independently of the supplied filesystem location.
    binding_artifact_path(Path("/unused"), external)
    return external["package_id"]


def adapter_bindings(path: Path) -> dict[str, dict]:
    marker = path / ".localsetup-adapter.json"
    if not marker.exists():
        return {}
    if marker.is_symlink() or marker.stat().st_size > 1024 * 1024:
        raise ValueError("Unsafe adapter binding receipt")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Adapter binding receipt must be an object")
    bindings = payload.get("package_bindings", {})
    if not isinstance(bindings, dict) or any(not isinstance(v, dict) for v in bindings.values()):
        raise ValueError("Invalid adapter package bindings")
    root = Path(payload.get("global_root", ""))
    for name, binding in bindings.items():
        if name != binding.get("name"):
            raise ValueError("Adapter binding visible name differs")
        binding_artifact_path(root, binding)
        if binding.get("disposition") not in {"managed_projection", "external_present"}:
            raise ValueError("Unknown external binding disposition")
        if binding["disposition"] == "external_present" and Path(binding["source_path"]).absolute() != (path / name).absolute():
            raise ValueError("External original is not at the recorded adapter path")
    return bindings


def package_source(root: Path, name: str, *, bindings: dict | None = None,
                   adapter: Path | None = None) -> Path:
    selected = bindings if bindings is not None else adapter_bindings(adapter) if adapter is not None else {}
    binding = selected.get(name)
    if binding is None:
        return root / name
    if binding.get("name") != name:
        raise ValueError("External binding visible name differs")
    return binding_artifact_path(root, binding)


def merge_bindings(*groups: dict[str, dict]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for group in groups:
        for name, binding in group.items():
            if name in merged and merged[name].get("package_id") != binding.get("package_id"):
                raise ValueError(f"Shared adapter has conflicting origins for {name}")
            merged[name] = binding
    return merged


def merge_binding_selections(*selections) -> dict[str, dict]:
    identities = {}
    for names, bindings in selections:
        for name in names:
            identity = bindings.get(name, {}).get("package_id", f"framework:{name}")
            if name in identities and identities[name] != identity:
                raise ValueError(f"Shared adapter has conflicting origins for {name}")
            identities[name] = identity
    return merge_bindings(*(bindings for _names, bindings in selections))


def verify_binding(root: Path, binding: dict, *, projection: Path | None = None,
                   allow_stale_contract: bool = False) -> None:
    from .skills_external import CONTRACT_VERSION, source_digest, source_files
    from .provenance import load_package_marker
    artifact = binding_artifact_path(root, binding)
    if any(path.is_symlink() for path in (artifact, *artifact.parents)):
        raise ValueError("External artifact uses a symlink alias")
    marker = load_package_marker(artifact) or {}
    recorded = marker.get("external_source", {})
    if (recorded.get("package_id") != binding["package_id"]
            or marker.get("integration_contract") != binding["integration_contract"]
            or source_digest(source_files(artifact, managed_snapshot=True)) != binding["source_digest"]):
        raise ValueError("External snapshot provenance or content differs")
    if projection is not None:
        if projection.is_symlink():
            if projection.resolve() != artifact.resolve():
                raise ValueError("External projection link differs")
        elif source_digest(source_files(projection, managed_snapshot=True)) != binding["source_digest"]:
            raise ValueError("External projection content differs; preserve local edits")
    if not allow_stale_contract and binding["integration_contract"] != CONTRACT_VERSION:
        raise ValueError("External integration contract requires alignment")


def resolve_recorded_package(root: Path, home: Path, target: Path, selector: str,
                             *, source_root: Path | None = None) -> Path:
    """Resolve an installed explicit origin, never an unselected source guess."""
    from .lockfile import load_json
    from .paths import target_lockfile_path, legacy_target_lockfile_path, expand_user_path
    from .registry import load_registry
    candidates = {}
    lock_path = target_lockfile_path(target)
    if not lock_path.exists():
        lock_path = legacy_target_lockfile_path(target)
    records = []
    if lock_path.exists():
        records.append(load_json(lock_path).get("package_bindings", {}))
    if selector.startswith("personal:") and source_root is not None:
        from .manifests import load_pack_config
        registry = load_registry(expand_user_path(load_pack_config(source_root).global_registry, home))
        records.extend(row.get("package_bindings", {}) for row in registry.get("personal_owners", {}).values())
    for bindings in records:
        for binding in bindings.values():
            if binding.get("selector") == selector:
                candidates[binding["package_id"]] = binding
    if len(candidates) != 1:
        raise ValueError("Custom package selector is not installed unambiguously in the selected scope")
    binding = next(iter(candidates.values()))
    verify_binding(root, binding)
    return binding_artifact_path(root, binding)
