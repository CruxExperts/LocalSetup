"""Retain personal installation references independently of repository receipts."""
import hashlib
import json
from .installation_ownership import InstallationOwner


def owner_key(owner: InstallationOwner) -> str:
    encoded = json.dumps(owner.wire(), sort_keys=True, separators=(",", ":"))
    return "personal:" + hashlib.sha256(encoded.encode()).hexdigest()


def personal_selections(adapter: dict) -> dict[str, set[str]]:
    """Validate explicit owner selections against the coalesced physical union."""
    from .adapter_markers import is_safe_adapter_package_name

    keys = {owner_key(InstallationOwner(**raw)) for raw in adapter.get("owners", [])
            if raw.get("scope") == "personal"}
    def names(value):
        if not isinstance(value, list) or any(
            not isinstance(name, str) or not is_safe_adapter_package_name(name) for name in value
        ):
            raise ValueError("Invalid personal owner package selection")
        return set(value)
    union = names(adapter.get("packages", []))
    if "owner_packages" not in adapter:
        return {key: union.copy() for key in keys}
    selections = adapter["owner_packages"]
    if not isinstance(selections, dict) or set(selections) != keys:
        raise ValueError("Personal selections must name exactly the action owners")
    result = {key: names(value) for key, value in selections.items()}
    if set().union(*result.values()) != union:
        raise ValueError("Personal package union differs from owner selections")
    return result


def validate_personal_selection_consistency(adapters: list[dict]) -> None:
    seen = {}
    for adapter in adapters:
        for key, names in personal_selections(adapter).items():
            value = (names, adapter.get("mode", "symlink"))
            if key in seen and seen[key] != value:
                raise ValueError("Personal owner selection or mode differs across adapter paths")
            seen[key] = value


def record_personal_owners(registry: dict, adapters: list[dict], available: set[str]) -> None:
    """Update explicit personal owners; caller holds the shared package-root lock."""
    validate_personal_selection_consistency(adapters)
    selected: dict[str, dict] = {}
    for adapter in adapters:
        selections = personal_selections(adapter) if any(
            raw.get("scope") == "personal" for raw in adapter.get("owners", [])
        ) else {}
        for raw in adapter.get("owners", []):
            owner = InstallationOwner(**raw)
            if owner.scope != "personal":
                continue
            key = owner_key(owner)
            mode = adapter.get("mode", "symlink")
            if mode not in {"symlink", "portable"}:
                raise ValueError("Invalid personal adapter mode")
            record = selected.setdefault(key, {"owner": owner.wire(), "packages": set(), "paths": set(), "mode": mode, "mutable_paths": set(), "package_bindings": {}})
            if record["mode"] != mode:
                raise ValueError("Conflicting personal owner modes")
            names = selections[key]
            if not names <= available:
                raise ValueError("Personal owner references unavailable packages")
            record["packages"].update(names)
            from .package_bindings import merge_bindings
            record["package_bindings"] = merge_bindings(record["package_bindings"], {
                name: binding for name, binding in adapter.get("package_bindings", {}).items() if name in names
            })
            record["paths"].add(str(adapter["path"]))
            if adapter.get("mutable_copy", False):record["mutable_paths"].add(str(adapter["path"]))
    if not selected:
        return
    owners = registry.setdefault("personal_owners", {})
    for key, record in selected.items():
        names = record["packages"]
        ids = {record["package_bindings"].get(name, {}).get("package_id", name) for name in names}
        for name, package in registry["packages"].items():
            refs = set(package.get("refs", []))
            refs.discard(key)
            if name in ids:
                refs.add(key)
            package["refs"] = sorted(refs)
        owners[key] = {"owner": record["owner"], "packages": sorted(names), "paths": sorted(record["paths"]), "mode": record["mode"]}
        owners[key]["package_bindings"] = record["package_bindings"]
        if record["mutable_paths"]:owners[key]["mutable_paths"] = sorted(record["mutable_paths"])


def refuse_personal_overlap(registry: dict, paths: list[str]) -> None:
    """Do not let legacy repository removal consume a personal adapter marker."""
    selected = set(paths)
    if any(selected.intersection(record.get("paths", []))
           for record in registry.get("personal_owners", {}).values()):
        raise ValueError("Repository removal overlaps personal owners; shared-path removal is not yet qualified")
