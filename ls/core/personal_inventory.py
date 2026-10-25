"""Read recorded personal ownership and verify its filesystem exposure."""
from pathlib import Path
from .adapter_markers import ADAPTER_MARKER_JSON, is_safe_adapter_package_name
from .installation_ownership import InstallationOwner
from .lockfile import load_json
from .manifests import load_pack_config
from .paths import expand_user_path
from .personal_adapter import check_path, managed_entry
from .personal_registry import owner_key
from .provenance import is_managed_package, package_digest
from .registry import load_registry


def _links(path: Path) -> dict[str, str]:
    return {str(p.relative_to(path)): str(p.readlink()) for p in path.rglob("*") if p.is_symlink()}


def _inventory(repo_root: Path, home: Path, platform_ids: list[str] | None = None,
               *, expected: list[dict] | None = None,
               allow_legacy_alias_retirements: dict[str, set[str]] | None = None,
               allow_source_drift: bool = False) -> dict:
    if platform_ids == []:
        return {"ok": True, "owners": [], "adapters": [], "issues": []}
    pack = load_pack_config(repo_root);global_root = expand_user_path(pack.global_root, home)
    registry = load_registry(expand_user_path(pack.global_registry, home))
    records = registry.get("personal_owners", {});selected = None if platform_ids is None else set(platform_ids)
    if not isinstance(records, dict):raise ValueError("personal_owners must be an object")
    issues = [];by_path: dict[str, list[dict]] = {};owners = []
    for key, record in sorted(records.items()):
        try:
            owner = InstallationOwner(**record["owner"])
            if owner.scope != "personal" or owner.root != str(home.resolve()) or key != owner_key(owner):
                raise ValueError("Personal owner identity mismatch")
            names = record["packages"];paths = record["paths"];mode = record.get("mode", "symlink")
            if not isinstance(names, list) or any(not isinstance(n, str) or not is_safe_adapter_package_name(n) for n in names):
                raise ValueError("Invalid personal package selection")
            if not isinstance(paths, list) or not paths or mode not in {"symlink", "portable"}:
                raise ValueError("Invalid personal paths or mode")
            for path in paths:check_path(Path(path), home)
            row = {"owner": owner.wire(), "packages": names, "paths": paths, "mode": mode}
            row["package_bindings"] = record.get("package_bindings", {})
            if selected is None or owner.client in selected:owners.append(row)
            for path in paths:by_path.setdefault(path, []).append(row)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            issues.append(f"invalid personal ownership record {key}: {exc}")
    for adapter in expected or []:
        for raw in adapter.get("owners", []):
            owner = InstallationOwner(**raw)
            if (selected is None or owner.client in selected) and owner_key(owner) not in records:
                issues.append(f"missing personal owner record: {owner.client}")
    adapters = []
    for raw_path, rows in sorted(by_path.items()):
        active = [row for row in rows if selected is None or row["owner"]["client"] in selected]
        if not active:continue
        path = Path(raw_path);expected_names = {name for row in rows for name in row["packages"]}
        selections = [(row["packages"], row["package_bindings"]) for row in rows]
        modes = {row["mode"] for row in rows};errors = []
        for target in registry.get("targets", {}).values():
            for adapter in target.get("adapters", []):
                repo_owner = any(o.get("scope") == "repo" for o in adapter.get("owners", []))
                if "owners" not in adapter:repo_owner = bool(adapter.get("platforms") or adapter.get("platform"))
                if adapter.get("path") == raw_path and repo_owner:
                    expected_names.update(adapter.get("packages", []));modes.add(adapter.get("mode", "symlink"))
                    selections.append((adapter.get("packages", []), adapter.get("package_bindings", {})))
        try:
            marker = path / ADAPTER_MARKER_JSON
            if marker.is_symlink() or not marker.is_file():raise ValueError("missing or unsafe adapter marker")
            value = load_json(marker)
            if not isinstance(value, dict):raise ValueError("adapter marker must be an object")
            if value.get("managed_by") != "localsetup" or Path(value.get("global_root", "")).resolve() != global_root.resolve():
                raise ValueError("adapter marker identity differs from managed library")
            if len(modes) != 1 or value.get("mode") not in modes:raise ValueError("adapter mode differs from owners")
            marker_names = set(value.get("packages", []))
            from .package_bindings import adapter_bindings, merge_binding_selections, verify_binding
            bindings = adapter_bindings(path)
            recorded_bindings = merge_binding_selections(*selections)
            if {n: b["package_id"] for n, b in bindings.items()} != {n: b["package_id"] for n, b in recorded_bindings.items()}:
                raise ValueError("Adapter bindings differ from recorded owners")
            originals = {name for name, binding in bindings.items() if binding.get("disposition") == "external_present"}
            marker_names.update(originals)
            missing_marker_names = expected_names - marker_names
            allowed_aliases = (allow_legacy_alias_retirements or {}).get(raw_path, set())
            if missing_marker_names - allowed_aliases or marker_names - expected_names:
                raise ValueError("adapter marker differs from owner union")
            visible = {entry.name for entry in path.iterdir() if not entry.name.startswith(".") and managed_entry(entry, global_root, value["mode"])}
            visible.update(originals)
            if visible != expected_names:raise ValueError("visible managed packages differ from owner union")
            for name in sorted(expected_names):
                if not isinstance(name, str) or not is_safe_adapter_package_name(name):raise ValueError("invalid retained package")
                from .package_bindings import package_source
                entry = path / name;source = package_source(global_root, name, adapter=path)
                if name in originals:
                    from .skills_external import inspect_external_skill, validate_source_binding
                    if allow_source_drift:
                        current = inspect_external_skill(entry, bindings[name]["scope"])
                        if current.origin_id != bindings[name]["origin_id"] or current.compatibility == "alignment-required":
                            raise ValueError("External original requires compatibility alignment")
                    else:
                        validate_source_binding(bindings[name])
                    continue
                if name in bindings:
                    verify_binding(global_root, bindings[name], projection=entry, allow_stale_contract=allow_source_drift)
                if not source.is_dir() or not is_managed_package(source):raise ValueError(f"missing managed library package: {name}")
                if value["mode"] == "symlink":
                    if not entry.is_symlink() or entry.resolve(strict=False) != source.resolve(strict=False):
                        raise ValueError(f"adapter link differs from managed library: {name}")
                elif entry.is_symlink() or not entry.is_dir() or not is_managed_package(entry):
                    raise ValueError(f"missing portable managed package: {name}")
                elif package_digest(entry) != package_digest(source) or _links(entry) != _links(source):
                    raise ValueError(f"portable package content differs: {name}")
        except (ValueError, TypeError, KeyError, OSError) as exc:errors.append(str(exc))
        adapters.append({"path": raw_path, "owners": [row["owner"] for row in rows],
                         "requested_packages": sorted({n for row in active for n in row["packages"]}),
                         "expected_visible_packages": sorted(expected_names), "ok": not errors, "issues": errors})
        issues.extend(f"personal adapter {raw_path}: {error}" for error in errors)
    return {"ok": not issues, "owners": owners, "adapters": adapters, "issues": issues}


def personal_inventory(repo_root: Path, home: Path, platform_ids: list[str] | None = None,
                       *, expected: list[dict] | None = None,
                       allow_legacy_alias_retirements: dict[str, set[str]] | None = None,
                       allow_source_drift: bool = False) -> dict:
    try:return _inventory(repo_root, home, platform_ids, expected=expected,
                          allow_legacy_alias_retirements=allow_legacy_alias_retirements,
                          allow_source_drift=allow_source_drift)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError) as exc:
        return {"ok": False, "owners": [], "adapters": [],
                "issues": [f"invalid personal inventory data: {type(exc).__name__}"]}
