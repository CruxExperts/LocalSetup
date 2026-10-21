"""Conservative reconciliation for obsolete package names inside adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapter_markers import adapter_marker_packages
from .aliases import collect_package_aliases
from .installation_ownership import InstallationOwner
from .package_digests import FRAMED_PACKAGE_DIGEST_ALGORITHM, package_digests
from .personal_registry import owner_key, personal_selections
from .provenance import (
    load_package_marker,
    marker_public_snapshot,
    source_provenance_hash,
)


_PROOF_FIELDS = (
    "package_name",
    "package_type",
    "source_path",
    "source_provenance_hash",
    "package_digest_framed",
)


def package_alias_shadows(source_root: Path, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """List physical alias paths that shadow a selected current package."""
    aliases = collect_package_aliases(source_root)
    shadows: list[dict[str, str]] = []
    for row in rows:
        raw_path = row.get("path")
        names = row.get("packages", [])
        if not isinstance(raw_path, str) or not isinstance(names, list):
            continue
        path = Path(raw_path)
        selected = {aliases.get(str(name), str(name)) for name in names if isinstance(name, str)}
        if not path.is_dir() or path.is_symlink():
            continue
        for alias, canonical in sorted(aliases.items()):
            candidate = path / alias
            if canonical in selected and (candidate.exists() or candidate.is_symlink()):
                shadows.append({"path": str(candidate), "alias": alias, "canonical": canonical})
    unique = {(row["path"], row["alias"], row["canonical"]): row for row in shadows}
    return [unique[key] for key in sorted(unique)]


def _path_value(value: Any, target_root: Path) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = target_root / path
    return str(path.resolve(strict=False))


def _packages(row: dict[str, Any]) -> set[str]:
    values = row.get("packages", [])
    if not isinstance(values, list):
        return set()
    return {name for name in values if isinstance(name, str)}


def _target_lock(target_root: Path) -> dict[str, Any]:
    from .lockfile import load_json

    for path in (target_root / ".localsetup" / "lock.json", target_root / "localsetup.lock.json"):
        if path.is_file() and not path.is_symlink():
            value = load_json(path)
            return value if isinstance(value, dict) else {}
    return {}


def _matching_snapshots(alias: str, target_root: Path, lock: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    lock_snapshots = lock.get("package_provenance", {})
    if isinstance(lock_snapshots, dict) and isinstance(lock_snapshots.get(alias), dict):
        snapshots.append(lock_snapshots[alias])
    target_id = str(target_root.resolve(strict=False))
    target_record = registry.get("targets", {}).get(target_id, {})
    if isinstance(target_record, dict):
        target_snapshots = target_record.get("package_provenance", {})
        if isinstance(target_snapshots, dict) and isinstance(target_snapshots.get(alias), dict):
            snapshots.append(target_snapshots[alias])
    package_record = registry.get("packages", {}).get(alias, {})
    if isinstance(package_record, dict) and isinstance(package_record.get("provenance"), dict):
        snapshots.append(package_record["provenance"])
    return snapshots


def _path_recorded(
    alias: str,
    canonical: str,
    path: Path,
    target_root: Path,
    lock: dict[str, Any],
    registry: dict[str, Any],
) -> bool:
    expected = str(path.resolve(strict=False))
    if alias in (adapter_marker_packages(path) or set()):
        return True
    has_alias_snapshot = bool(_matching_snapshots(alias, target_root, lock, registry))
    for key in ("adapter_targets", "personal_adapter_targets"):
        rows = lock.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or _path_value(row.get("path"), target_root) != expected:
                continue
            packages = _packages(row)
            if alias in packages or (canonical in packages and has_alias_snapshot):
                return True
    state = lock.get("adapter_state", [])
    if isinstance(state, list) and any(_path_value(item, target_root) == expected for item in state):
        packages = _packages({"packages": lock.get("repo_packages", lock.get("adapter_packages", []))})
        if alias in packages or (canonical in packages and has_alias_snapshot):
            return True
    for target_id, record in registry.get("targets", {}).items():
        if not isinstance(record, dict):
            continue
        for row in record.get("adapters", []):
            if not isinstance(row, dict) or _path_value(row.get("path"), target_root) != expected:
                continue
            packages = _packages(row)
            if alias in packages or (canonical in packages and has_alias_snapshot):
                return True
    for key, record in registry.get("personal_owners", {}).items():
        if not isinstance(record, dict):
            continue
        packages = _packages(record)
        if (alias in packages or (canonical in packages and has_alias_snapshot)) and any(
            _path_value(item, target_root) == expected for item in record.get("paths", [])
        ):
            return True
    return False


def _other_owner_uses_path_alias(
    alias: str,
    path: Path,
    target_root: Path,
    registry: dict[str, Any],
    actions: list[Any],
) -> bool:
    current_id = str(target_root.resolve(strict=False))
    current_path = str(path.resolve(strict=False))
    updated_paths = {str(action.path.resolve(strict=False)) for action in actions
                     if action.kind in {"attach_repo_path", "attach_personal_path", "repair_repo_path"}}
    selected_owners = set()
    for action in actions:
        if action.kind != "attach_personal_path" or str(action.path.resolve(strict=False)) != current_path:
            continue
        for raw_owner in action.details.get("owners", []):
            try:
                owner = InstallationOwner(**raw_owner)
            except (TypeError, ValueError):
                continue
            if owner.scope == "personal":
                selected_owners.add(owner_key(owner))

    for key, record in registry.get("personal_owners", {}).items():
        if not isinstance(record, dict) or alias not in _packages(record):
            continue
        paths = {_path_value(item, target_root) for item in record.get("paths", [])}
        if current_path in paths and key not in selected_owners:
            return True

    for target_id, record in registry.get("targets", {}).items():
        if not isinstance(record, dict):
            continue
        for adapter in record.get("adapters", []):
            if not isinstance(adapter, dict) or alias not in _packages(adapter):
                continue
            recorded_path = _path_value(adapter.get("path"), target_root)
            if recorded_path != current_path:
                continue
            if target_id != current_id or current_path not in updated_paths:
                return True
    return False


def _has_symlink(path: Path) -> bool:
    for child in path.rglob("*"):
        try:
            if child.is_symlink():
                return True
        except OSError:
            return True
    return False


def _proven_unchanged_package(
    source_root: Path,
    alias: str,
    canonical: str,
    artifact: Path,
    snapshots: list[dict[str, Any]],
    global_root: Path,
) -> tuple[bool, str]:
    if artifact.is_symlink():
        expected_target = (global_root / alias).resolve(strict=False)
        link_target = artifact.resolve(strict=False)
        if link_target != expected_target or not expected_target.is_dir():
            return False, "alias entry is not a link to its recorded managed package"
        artifact_for_proof = expected_target
    elif artifact.is_dir():
        artifact_for_proof = artifact
    else:
        return False, "alias path is not a regular package directory"
    if _has_symlink(artifact_for_proof):
        return False, "package contains links whose unchanged content cannot be verified"
    marker = load_package_marker(artifact_for_proof)
    if not isinstance(marker, dict) or marker.get("legacy_marker"):
        return False, "package has no complete provenance marker"
    if marker.get("package_name") != alias or marker.get("package_type") not in {"skill", "workflow"}:
        return False, "package provenance does not identify the obsolete alias"
    if marker.get("emitter") not in {"package-install", "migration"}:
        return False, "package provenance was not created by an installation owner"
    source_path_raw = marker.get("source_path")
    if not isinstance(source_path_raw, str):
        return False, "package provenance does not identify its source"
    source_path = Path(source_path_raw)
    expected_parent = source_root / "ls" / ("skills" if marker["package_type"] == "skill" else "workflows")
    if source_path.parent.resolve(strict=False) != expected_parent.resolve(strict=False):
        return False, "package provenance belongs to a different source tree"
    if source_path.name not in {alias, canonical}:
        return False, "package provenance source does not match the alias mapping"
    if marker.get("source_provenance_hash") != source_provenance_hash(marker):
        return False, "package provenance checksum is invalid"
    _legacy, actual_framed = package_digests(artifact_for_proof)
    marker_digest = marker.get("package_digest_framed")
    if (not isinstance(marker_digest, dict)
            or marker_digest.get("algorithm") != FRAMED_PACKAGE_DIGEST_ALGORITHM
            or marker_digest.get("value") != actual_framed):
        return False, "package content differs from its provenance digest"
    if not snapshots:
        return False, "installation receipt has no package provenance snapshot"
    live_snapshot = marker_public_snapshot(marker) or {}
    for snapshot in snapshots:
        if any(snapshot.get(field) != live_snapshot.get(field) for field in _PROOF_FIELDS):
            return False, "installation receipt and package provenance disagree"
    return True, "package ownership and unchanged content are recorded"


def plan_alias_retirements(
    source_root: Path,
    home: Path,
    target_root: Path,
    actions: list[Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return proven obsolete entries to retire and unresolved shadows to report."""
    from .manifests import load_pack_config
    from .paths import expand_user_path
    from .registry import load_registry

    aliases = collect_package_aliases(source_root)
    lock = _target_lock(target_root)
    registry_path = expand_user_path(load_pack_config(source_root).global_registry, home)
    registry = load_registry(registry_path) if registry_path.exists() else {}
    retirements: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    for action in actions:
        if action.kind not in {"attach_repo_path", "attach_personal_path", "repair_repo_path"}:
            continue
        path = action.path
        if not path.is_dir() or path.is_symlink():
            continue
        global_root = Path(action.details.get("global_root", ""))
        names = action.details.get("packages", [])
        if action.kind == "attach_personal_path":
            try:
                names = set().union(*personal_selections(action.details).values())
            except (ValueError, TypeError, KeyError):
                # The owning personal inventory emits the detailed selection error.
                continue
        selected = {aliases.get(str(name), str(name)) for name in names if isinstance(name, str)}
        for alias, canonical in sorted(aliases.items()):
            if canonical not in selected:
                continue
            candidate = path / alias
            if not (candidate.exists() or candidate.is_symlink()):
                continue
            row = {"path": str(candidate), "alias": alias, "canonical": canonical}
            snapshots = _matching_snapshots(alias, target_root, lock, registry)
            if not _path_recorded(alias, canonical, path, target_root, lock, registry):
                conflicts.append({**row, "reason": "no installation receipt records this alias at the adapter path"})
                continue
            if _other_owner_uses_path_alias(alias, path, target_root, registry, actions):
                conflicts.append({**row, "reason": "another recorded owner still uses this alias at the shared path"})
                continue
            proven, reason = _proven_unchanged_package(
                source_root, alias, canonical, candidate, snapshots, global_root
            )
            if not proven:
                conflicts.append({**row, "reason": reason})
                continue
            retirements.append(row)
    return sorted(retirements, key=lambda row: row["path"]), sorted(conflicts, key=lambda row: row["path"])


def revalidate_alias_retirements(
    source_root: Path,
    home: Path,
    target_root: Path,
    actions: list[Any],
    action: Any,
) -> list[str]:
    """Recheck planned deletions immediately before mutation to reject stale proof."""
    planned = set(action.details.get("legacy_alias_retirements", []))
    if not planned:
        return []
    retirements, conflicts = plan_alias_retirements(source_root, home, target_root, actions)
    action_path = str(action.path.resolve(strict=False))
    proven = {
        row["alias"] for row in retirements
        if str(Path(row["path"]).parent.resolve(strict=False)) == action_path
    }
    failed = [row for row in conflicts
              if str(Path(row["path"]).parent.resolve(strict=False)) == action_path
              and row["alias"] in planned]
    if failed or not planned.issubset(proven):
        paths = sorted(row["path"] for row in failed)
        missing = sorted(planned - proven)
        raise ValueError(
            "legacy package alias proof changed before adapter update: "
            + ", ".join(paths or [str(action.path / name) for name in missing])
        )
    return sorted(planned)
