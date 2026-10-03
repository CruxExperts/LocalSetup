from __future__ import annotations

from pathlib import Path
import shutil

from .adapter_markers import (
    ADAPTER_MARKER_JSON,
    adapter_marker_packages,
    adapter_marker_state,
    is_safe_adapter_package_name,
)
from .manifests import load_platforms
from .paths import expand_user_path, repo_path
from .provenance import is_managed_package

_is_safe_adapter_package_name = is_safe_adapter_package_name


def validate_platform_selectors(repo_root: Path, platform_ids: list[str] | None) -> list[str]:
    platforms = load_platforms(repo_root)
    known_ids = {platform.platform_id for platform in platforms}
    requested_ids = set(platform_ids or [])
    unknown_ids = sorted(requested_ids - known_ids)
    if unknown_ids:
        available_ids = ", ".join(sorted(known_ids))
        raise ValueError(
            f"unknown platform selector(s): {', '.join(unknown_ids)}; "
            f"registered selectors: {available_ids}"
        )
    return sorted(requested_ids)


def adapter_targets(
    repo_root: Path,
    home: Path,
    platform_ids: list[str] | None = None,
    *,
    target_root: Path | None = None,
) -> list[dict]:
    validate_platform_selectors(repo_root, platform_ids)
    selected = set(platform_ids or [])
    if not selected:
        return []
    attachment_root = target_root or repo_root
    targets_by_path: dict[Path, dict] = {}
    for platform in load_platforms(repo_root):
        if platform.platform_id not in selected:
            continue
        for rel in platform.repo_paths:
            physical_path = repo_path(attachment_root, rel, f"{platform.platform_id}.repo_paths")
            target = targets_by_path.setdefault(
                physical_path,
                {
                    "platform": platform.platform_id,
                    "platforms": [],
                    "repo_path": physical_path,
                    "global_paths": [],
                    "verify_rules": [],
                    "rollback_targets": [],
                },
            )
            target["platforms"].append(platform.platform_id)
            for global_path in [expand_user_path(path, home) for path in platform.global_paths]:
                if global_path not in target["global_paths"]:
                    target["global_paths"].append(global_path)
            for rule in platform.verify_rules:
                if rule not in target["verify_rules"]:
                    target["verify_rules"].append(rule)
            for rollback_target in [
                repo_path(attachment_root, path, f"{platform.platform_id}.rollback_targets")
                for path in platform.rollback_targets
            ]:
                if rollback_target not in target["rollback_targets"]:
                    target["rollback_targets"].append(rollback_target)
    return list(targets_by_path.values())


def legacy_global_roots(home: Path) -> list[Path]:
    return [home / ".local" / "share" / "agents" / "skills" / "localsetup"]


def _symlink_target(repo_path: Path) -> Path | None:
    if not repo_path.is_symlink():
        return None
    link_target = repo_path.readlink()
    if not link_target.is_absolute():
        link_target = repo_path.parent / link_target
    return link_target.resolve(strict=False)


def _is_repo_local_symlink_adapter(repo_path: Path, target_root: Path | None) -> bool:
    if target_root is None or not repo_path.is_symlink() or not repo_path.exists():
        return False
    target = _symlink_target(repo_path)
    if target is None or not target.is_dir():
        return False
    try:
        target.relative_to(target_root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _visible_adapter_packages(repo_path: Path, global_root: Path, *, target_root: Path | None = None) -> list[str]:
    if repo_path.is_symlink() and _symlink_target(repo_path) == global_root.resolve(strict=False):
        if global_root.exists():
            return sorted(path.name for path in global_root.iterdir() if path.is_dir())
        return []
    if not repo_path.is_dir() or (repo_path.is_symlink() and not _is_repo_local_symlink_adapter(repo_path, target_root)):
        return []
    names: list[str] = []
    for child in sorted(repo_path.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_symlink() or child.is_dir():
            names.append(child.name)
    return names


def _is_managed_portable_adapter_entry(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and is_managed_package(path)


def _is_managed_symlink_adapter_entry(path: Path, global_root: Path) -> bool:
    expected = global_root / path.name
    return path.is_symlink() and _symlink_target(path) == expected.resolve(strict=False)


def _managed_visible_adapter_packages(repo_path: Path, global_root: Path, *, target_root: Path | None = None) -> list[str]:
    if repo_path.is_symlink() and _symlink_target(repo_path) == global_root.resolve(strict=False):
        if global_root.exists():
            return sorted(path.name for path in global_root.iterdir() if path.is_dir())
        return []
    if not repo_path.is_dir() or (repo_path.is_symlink() and not _is_repo_local_symlink_adapter(repo_path, target_root)):
        return []
    marker = adapter_marker_state(repo_path)
    marker_mode = marker["mode"]
    legacy_portable = (repo_path / ".localsetup-portable").exists()
    names: set[str] = set()
    for child in sorted(repo_path.iterdir()):
        if child.name.startswith("."):
            continue
        if (marker_mode == "portable" or legacy_portable) and _is_managed_portable_adapter_entry(child):
            names.add(child.name)
        elif _is_managed_symlink_adapter_entry(child, global_root):
            names.add(child.name)
    return sorted(names)


def _adapter_package_integrity(repo_path: Path, global_root: Path, *, target_root: Path | None = None) -> list[dict]:
    if not repo_path.is_dir() or (repo_path.is_symlink() and not _is_repo_local_symlink_adapter(repo_path, target_root)):
        return []
    marker = adapter_marker_state(repo_path)
    marker_mode = marker["mode"]
    if marker["exists"] and marker["error"]:
        return [
            {
                "package": None,
                "path": str(repo_path / ADAPTER_MARKER_JSON),
                "expected_target": None,
                "mode": marker_mode,
                "ok": False,
                "reason": marker["error"],
            }
        ]
    if marker_mode not in {"symlink", "portable"}:
        return []
    rows: list[dict] = []
    marker_packages = adapter_marker_packages(repo_path) or set()
    managed_children: set[str] = set()
    for child in sorted(repo_path.iterdir()):
        if child.name.startswith("."):
            continue
        if marker_mode == "portable" and _is_managed_portable_adapter_entry(child):
            managed_children.add(child.name)
        elif _is_managed_symlink_adapter_entry(child, global_root):
            managed_children.add(child.name)
    for package_name in sorted(marker_packages | managed_children):
        child = repo_path / package_name
        expected = global_root / package_name
        row = {
            "package": package_name,
            "path": str(child),
            "expected_target": str(expected),
            "mode": marker_mode,
            "ok": False,
            "reason": None,
        }
        if not (child.exists() or child.is_symlink()):
            row["is_symlink"] = False
            row["is_directory"] = False
            row["resolved_target"] = None
            row["reason"] = "adapter marker package is missing"
            rows.append(row)
            continue
        if marker_mode == "portable":
            row["is_symlink"] = child.is_symlink()
            row["is_directory"] = child.is_dir()
            row["resolved_target"] = str(child.resolve(strict=False))
            if _is_managed_portable_adapter_entry(child):
                row["ok"] = True
            elif child.is_dir() and not child.is_symlink():
                row["reason"] = "portable adapter package lacks LocalSetup provenance"
            else:
                row["reason"] = "portable adapter package is not a directory copy"
        elif child.is_symlink():
            resolved = _symlink_target(child)
            expected_resolved = expected.resolve(strict=False)
            row["is_symlink"] = True
            row["is_directory"] = child.is_dir()
            row["resolved_target"] = str(resolved) if resolved else None
            if resolved == expected_resolved:
                row["ok"] = True
            else:
                row["reason"] = "package symlink target differs from managed package"
        elif child.is_dir():
            row["is_symlink"] = False
            row["is_directory"] = True
            row["resolved_target"] = str(child.resolve(strict=False))
            row["reason"] = "symlink adapter package is not a symlink"
        else:
            row["is_symlink"] = child.is_symlink()
            row["is_directory"] = child.is_dir()
            row["resolved_target"] = str(child.resolve(strict=False))
            row["reason"] = "adapter package is not a supported filesystem node"
        rows.append(row)
    return rows


def _child_is_custom_skill(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (path / "SKILL.md").is_file()


def _child_is_managed_adapter_package(
    path: Path,
    global_root: Path,
    marker_mode: str | None,
    marker_packages: set[str] | None = None,
) -> bool:
    expected = global_root / path.name
    if marker_mode == "portable" or ((path.parent / ".localsetup-portable").exists() and not (path.parent / ADAPTER_MARKER_JSON).exists()):
        return _is_managed_portable_adapter_entry(path)
    return path.is_symlink() and _symlink_target(path) == expected.resolve(strict=False)


def _child_is_repo_local_symlink(path: Path, target_root: Path | None) -> bool:
    if target_root is None or not path.is_symlink() or not path.exists():
        return False
    target = _symlink_target(path)
    if target is None:
        return False
    try:
        target.relative_to(target_root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _child_is_supported_custom_entry(path: Path, target_root: Path | None) -> bool:
    if path.is_symlink():
        return _child_is_repo_local_symlink(path, target_root)
    return path.is_file() or path.is_dir()


def _directory_adapter_classification(repo_path: Path, global_root: Path, *, target_root: Path | None = None) -> dict:
    marker = adapter_marker_state(repo_path)
    marker_mode = marker["mode"]
    marker_packages = adapter_marker_packages(repo_path)
    visible = [child for child in sorted(repo_path.iterdir()) if not child.name.startswith(".")]
    managed_entries = [
        child.name for child in visible if _child_is_managed_adapter_package(child, global_root, marker_mode, marker_packages)
    ]
    custom_entries = [
        child.name
        for child in visible
        if child.name not in managed_entries and _child_is_custom_skill(child)
    ]
    unsafe_entries = [
        child.name
        for child in visible
        if child.name not in managed_entries and child.name not in custom_entries and not _child_is_supported_custom_entry(child, target_root)
    ]
    unknown_entries = [
        child.name
        for child in visible
        if child.name not in managed_entries and child.name not in custom_entries and child.name not in unsafe_entries
    ]
    if unsafe_entries:
        return {
            "status_code": "unsafe_adapter_content",
            "collision_reason": "adapter directory contains unsupported or unsafe entries",
            "custom_entries": custom_entries,
            "managed_entries": managed_entries,
            "unknown_entries": unknown_entries,
            "unsafe_entries": unsafe_entries,
        }
    if marker["exists"] and marker["error"]:
        return {
            "status_code": "unmarked_framework_adapter",
            "collision_reason": marker["error"],
            "custom_entries": custom_entries,
            "managed_entries": managed_entries,
            "unknown_entries": unknown_entries,
            "unsafe_entries": unsafe_entries,
        }
    if marker_mode == "portable":
        status = "managed_portable_adapter" if not custom_entries else "mixed_managed_custom_adapter"
        if unknown_entries:
            status = "mixed_managed_custom_adapter" if managed_entries or custom_entries else "shared_adapter_directory"
        return {
            "status_code": status,
            "collision_reason": None,
            "custom_entries": custom_entries,
            "managed_entries": managed_entries,
            "unknown_entries": unknown_entries,
            "unsafe_entries": unsafe_entries,
        }
    if marker_mode == "symlink":
        status = "managed_scoped_adapter" if not custom_entries and not unknown_entries else "mixed_managed_custom_adapter"
        return {
            "status_code": status,
            "collision_reason": None,
            "custom_entries": custom_entries,
            "managed_entries": managed_entries,
            "unknown_entries": unknown_entries,
            "unsafe_entries": unsafe_entries,
        }
    if (repo_path / ".localsetup-portable").exists():
        return {
            "status_code": "managed_portable_adapter",
            "collision_reason": None,
            "custom_entries": custom_entries,
            "managed_entries": managed_entries,
            "unknown_entries": unknown_entries,
            "unsafe_entries": unsafe_entries,
        }
    if visible and all(_child_is_custom_skill(child) for child in visible):
        return {
            "status_code": "custom_repo_skills",
            "collision_reason": None,
            "custom_entries": [child.name for child in visible],
            "managed_entries": [],
            "unknown_entries": [],
            "unsafe_entries": unsafe_entries,
        }
    return {
        "status_code": "shared_adapter_directory",
        "collision_reason": None,
        "custom_entries": custom_entries,
        "managed_entries": managed_entries,
        "unknown_entries": unknown_entries
        or [child.name for child in visible if child.name not in custom_entries and child.name not in managed_entries],
        "unsafe_entries": unsafe_entries,
    }


def adapter_classification(
    repo_path: Path,
    global_root: Path,
    *,
    known_global_roots: list[Path] | None = None,
    target_root: Path | None = None,
) -> dict:
    managed_roots = [global_root, *(known_global_roots or [])]
    resolved_roots = {root.resolve(strict=False) for root in managed_roots}
    exists = repo_path.exists() or repo_path.is_symlink()
    if not exists:
        return {"status_code": "absent", "collision_reason": None, "custom_entries": [], "managed_entries": [], "unknown_entries": [], "unsafe_entries": []}
    if repo_path.is_symlink():
        target = _symlink_target(repo_path)
        if not repo_path.exists():
            return {"status_code": "dangling_symlink", "collision_reason": "dangling symlink", "custom_entries": [], "managed_entries": [], "unknown_entries": [], "unsafe_entries": []}
        if target == global_root.resolve(strict=False):
            return {"status_code": "legacy_monolithic_symlink", "collision_reason": None, "custom_entries": [], "managed_entries": [], "unknown_entries": [], "unsafe_entries": []}
        if target in (resolved_roots - {global_root.resolve(strict=False)}):
            return {"status_code": "legacy_monolithic_symlink", "collision_reason": None, "custom_entries": [], "managed_entries": [], "unknown_entries": [], "unsafe_entries": []}
        if _is_repo_local_symlink_adapter(repo_path, target_root):
            return _directory_adapter_classification(repo_path, global_root, target_root=target_root)
        return {"status_code": "unsupported_node", "collision_reason": "symlink points outside managed library", "custom_entries": [], "managed_entries": [], "unknown_entries": [], "unsafe_entries": []}
    if repo_path.is_file():
        return {"status_code": "regular_file", "collision_reason": "regular file", "custom_entries": [], "managed_entries": [], "unknown_entries": [], "unsafe_entries": []}
    if not repo_path.is_dir():
        return {"status_code": "unsupported_node", "collision_reason": "unsupported filesystem node", "custom_entries": [], "managed_entries": [], "unknown_entries": [], "unsafe_entries": []}
    return _directory_adapter_classification(repo_path, global_root, target_root=target_root)


def adapter_path_state(
    repo_path: Path,
    global_root: Path,
    *,
    known_global_roots: list[Path] | None = None,
    target_root: Path | None = None,
) -> dict:
    managed_roots = [global_root, *(known_global_roots or [])]
    resolved_roots = {root.resolve(strict=False) for root in managed_roots}
    exists = repo_path.exists() or repo_path.is_symlink()
    is_symlink = repo_path.is_symlink()
    is_dangling_symlink = is_symlink and not repo_path.exists()
    is_repo_local_symlink_adapter = _is_repo_local_symlink_adapter(repo_path, target_root)
    points_to_global = False
    points_to_legacy_global = False
    is_monolithic_global_symlink = False
    if is_symlink:
        link_target = _symlink_target(repo_path)
        points_to_global = link_target == global_root.resolve(strict=False)
        points_to_legacy_global = bool(link_target and link_target in (resolved_roots - {global_root.resolve(strict=False)}))
        is_monolithic_global_symlink = bool(link_target and link_target in resolved_roots)
    is_scoped_symlink_adapter = (
        repo_path.is_dir()
        and (not is_symlink or is_repo_local_symlink_adapter)
        and (repo_path / ADAPTER_MARKER_JSON).exists()
    )
    is_portable_copy = (
        repo_path.is_dir()
        and not is_symlink
        and (adapter_marker_state(repo_path)["mode"] == "portable"
             or (repo_path / ".localsetup-portable").exists())
    )
    is_unmanaged_directory = (
        repo_path.is_dir()
        and (not is_symlink or is_repo_local_symlink_adapter)
        and not is_portable_copy
        and not is_scoped_symlink_adapter
    )
    is_regular_file = repo_path.exists() and repo_path.is_file() and not is_symlink
    is_other = repo_path.exists() and not (
        repo_path.is_file() or repo_path.is_dir() or is_symlink
    )
    classification = adapter_classification(
        repo_path,
        global_root,
        known_global_roots=known_global_roots,
        target_root=target_root,
    )
    collision_reason = classification["collision_reason"]
    if is_dangling_symlink:
        collision_reason = "dangling symlink"
    elif is_symlink and not is_monolithic_global_symlink and not is_repo_local_symlink_adapter:
        collision_reason = "symlink points outside managed library"
    elif is_regular_file:
        collision_reason = "regular file"
    elif is_unmanaged_directory and classification["status_code"] == "unmanaged_adapter_directory":
        collision_reason = "unmanaged adapter directory"
    elif is_other:
        collision_reason = "unsupported filesystem node"
    package_integrity = _adapter_package_integrity(repo_path, global_root, target_root=target_root)
    package_integrity_failures = [row for row in package_integrity if not row.get("ok")]
    return {
        "exists": exists,
        "status_code": classification["status_code"],
        "custom_entries": classification.get("custom_entries", []),
        "managed_entries": classification.get("managed_entries", []),
        "unknown_entries": classification.get("unknown_entries", []),
        "unsafe_entries": classification.get("unsafe_entries", []),
        "is_symlink": is_symlink,
        "is_repo_local_symlink_adapter": is_repo_local_symlink_adapter,
        "is_dangling_symlink": is_dangling_symlink,
        "points_to_global": points_to_global,
        "points_to_legacy_global": points_to_legacy_global,
        "is_monolithic_global_symlink": is_monolithic_global_symlink,
        "is_scoped_symlink_adapter": is_scoped_symlink_adapter,
        "is_portable_copy": is_portable_copy,
        "is_unmanaged_directory": is_unmanaged_directory,
        "is_regular_file": is_regular_file,
        "is_other": is_other,
        "collision_reason": collision_reason,
        "visible_packages": _visible_adapter_packages(repo_path, global_root, target_root=target_root),
        "managed_visible_packages": _managed_visible_adapter_packages(repo_path, global_root, target_root=target_root),
        "package_integrity": package_integrity,
        "package_integrity_ok": not package_integrity_failures,
        "package_integrity_failures": package_integrity_failures,
    }


def remove_managed_adapter_entries(
    repo_path: Path,
    global_root: Path,
    *,
    known_global_roots: list[Path] | None = None,
    recorded_packages: list[str] | None = None,
    preserve_directory: bool = False,
) -> list[str]:
    from .mutable_adapters import check_existing
    check_existing(repo_path)
    removed: list[str] = []
    state = adapter_path_state(repo_path, global_root, known_global_roots=known_global_roots)
    if not (repo_path.exists() or repo_path.is_symlink()):
        return removed
    if repo_path.is_symlink():
        if state["points_to_global"] or state["points_to_legacy_global"]:
            repo_path.unlink()
            removed.append(str(repo_path))
        return removed
    if not repo_path.is_dir():
        return removed

    marker = adapter_marker_state(repo_path)
    marker_mode = marker["mode"]
    marker_packages = adapter_marker_packages(repo_path) or set()
    candidates = set(marker_packages) | {str(name) for name in recorded_packages or [] if is_safe_adapter_package_name(str(name))}
    if not candidates:
        candidates = set(state.get("managed_visible_packages", []))
    for package_name in sorted(candidates):
        child = repo_path / package_name
        if not (child.exists() or child.is_symlink()):
            continue
        if _child_is_managed_adapter_package(child, global_root, marker_mode, None):
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
            removed.append(str(child))

    for metadata_name in [ADAPTER_MARKER_JSON, ".localsetup-portable"]:
        metadata = repo_path / metadata_name
        if metadata.exists() and metadata.is_file() and not metadata.is_symlink():
            metadata.unlink()
            removed.append(str(metadata))

    try:
        if not preserve_directory and repo_path.exists() and repo_path.is_dir() and not any(repo_path.iterdir()):
            repo_path.rmdir()
            removed.append(str(repo_path))
    except OSError:
        pass
    return removed


def adapter_status(
    repo_root: Path,
    home: Path,
    global_root: Path,
    platform_ids: list[str] | None = None,
    *,
    target_root: Path | None = None,
) -> list[dict]:
    status: list[dict] = []
    known_roots = legacy_global_roots(home)
    for target in adapter_targets(repo_root, home, platform_ids=platform_ids, target_root=target_root):
        repo_path = target["repo_path"]
        path_state = adapter_path_state(repo_path, global_root, known_global_roots=known_roots, target_root=target_root)
        status.append(
            {
                "platform": target["platform"],
                "platforms": target["platforms"],
                "repo_path": str(repo_path),
                **path_state,
                "verify_rules": target["verify_rules"],
            }
        )
    return status


def recorded_adapter_status(lock: dict, global_root: Path, platform_ids: list[str] | None = None, *, target_root: Path | None = None) -> list[dict]:
    if not isinstance(lock, dict):
        lock = {}
    recorded = lock.get("adapter_targets") if isinstance(lock, dict) else None
    if "adapter_targets" not in lock:
        recorded = [
            {"platform": None, "platforms": lock.get("platforms", []), "path": path, "mode": lock.get("attach_mode", "symlink"), "global_root": str(global_root)}
            for path in lock.get("adapter_state", [])
        ]
    statuses: list[dict] = []
    for item in recorded:
        clients = ([owner['client'] for owner in item['owners'] if owner.get('scope') == 'repo']
                   if 'owners' in item else item.get('platforms', [item['platform']] if item.get('platform') else []))
        if platform_ids is not None and not set(clients).intersection(platform_ids):continue
        path = Path(str(item["path"]))
        root = target_root or (Path(lock['target_root']) if lock.get('target_root') else None)
        if not path.is_absolute() and root is not None:path = root / path
        expected_global = Path(str(item.get("global_root") or global_root))
        statuses.append(
            {
                "platform": item.get("platform"),
                "platforms": clients,
                "repo_path": str(path),
                "expected_mode": item.get("mode", lock.get("attach_mode", "symlink")),
                "expected_packages": item.get("packages", lock.get("repo_packages", lock.get("adapter_packages", []))),
                **adapter_path_state(path, expected_global, target_root=root),
                "verify_rules": item.get("verify_rules", []),
            }
        )
    return statuses


def personal_adapter_targets(repo_root: Path, home: Path, platform_ids: list[str] | None) -> list[dict]:
    """Plan personal discovery paths without inferring any client selection."""
    from .installation_ownership import InstallationOwner
    selected = set(validate_platform_selectors(repo_root, platform_ids))
    targets: dict[Path, dict] = {}
    for platform in load_platforms(repo_root):
        if platform.platform_id not in selected:
            continue
        for value in platform.global_paths:
            path = expand_user_path(value, home)
            target = targets.setdefault(path, {"path": path, "platforms": [], "owners": []})
            if platform.platform_id not in target["platforms"]:
                target["platforms"].append(platform.platform_id)
                target["owners"].append(InstallationOwner("personal", str(home.resolve(strict=False)), platform.platform_id).wire())
    return sorted(targets.values(), key=lambda target: str(target["path"]))
