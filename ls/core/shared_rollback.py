"""Journal repository rollback while preserving independent adapter owners."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from .adapter_markers import ADAPTER_MARKER_JSON, adapter_marker_packages, adapter_marker_state
from .adapters import (
    _child_is_managed_adapter_package,
    adapter_path_state,
    legacy_global_roots,
    remove_managed_adapter_entries,
)
from .apply_journal import (
    cleanup_backups,
    journal_path,
    record_file_state,
    record_node_state,
    remove_path,
    restore_failed_mutations,
    write_journal,
)
from .manifests import load_pack_config
from .package_cleanup import is_package_backup_artifact
from .paths import expand_user_path
from .provenance import is_managed_package
from .registry import load_registry, package_has_other_refs, remove_target
from .repository_overlap import write_overlap
from .shared_detach import shared_detach_actions


def _snapshot_adapter(path, global_root, journal, receipt, recorded_packages=None):
    if path.is_symlink():
        record_file_state(journal, receipt, path, os.replace)
    elif path.is_dir():
        mode = adapter_marker_state(path)["mode"]
        from .adapter_markers import is_safe_adapter_package_name

        candidates = (adapter_marker_packages(path) or set()) | {
            name for name in recorded_packages or [] if is_safe_adapter_package_name(name)
        }
        if not candidates:
            candidates = set(adapter_path_state(path, global_root).get("managed_visible_packages", []))
        for name in sorted(candidates):
            entry = path / name
            if _child_is_managed_adapter_package(entry, global_root, mode, None):
                record_node_state(journal, receipt, entry, os.replace)
        for name in (ADAPTER_MARKER_JSON, ".localsetup-portable"):
            entry = path / name
            if entry.is_file() and not entry.is_symlink():
                record_file_state(journal, receipt, entry, os.replace)


def _registry_empty_after_target_removal(registry, target):
    target_id = str(target.resolve(strict=False))
    other_targets = any(key != target_id for key in registry.get("targets", {}))
    other_package_refs = any(
        ref != target_id
        for package in registry.get("packages", {}).values()
        for ref in package.get("refs", [])
    )
    return not other_targets and not other_package_refs and not registry.get("personal_owners")


def _empty_directory_cleanup(paths):
    removed = []
    warnings = []
    for path in dict.fromkeys(paths):
        try:
            if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
                continue
            path.rmdir()
            removed.append(str(path))
        except OSError as exc:
            warnings.append(f"rollback committed; empty-directory cleanup failed for {path}: {exc}")
    return removed, warnings


def shared_rollback(source, home, target, lock, lock_path, legacy_path):
    pack = load_pack_config(source)
    global_root = expand_user_path(pack.global_root, home)
    registry_path = expand_user_path(pack.global_registry, home)
    registry = load_registry(registry_path)
    paths = [Path(raw) if Path(raw).is_absolute() else target / raw for raw in lock.get("adapter_state", [])]
    recorded = {str(row["path"]): row for row in lock.get("adapter_targets", [])}
    shared = shared_detach_actions(
        source,
        home,
        target,
        [{"repo_path": path} for path in paths],
        recorded,
        global_root,
        lock.get("attach_mode", "symlink"),
    )

    from .rollback import _require_under_global_root, _shared_runtime_helper_path

    package_paths = [
        Path(raw)
        for raw in [*lock.get("installed_skills", []), *lock.get("installed_workflows", [])]
    ]
    if shared or registry_path.exists():
        package_candidates = (
            sorted(global_root.iterdir(), key=lambda path: path.name) if global_root.exists() else []
        )
    else:
        package_candidates = package_paths
    candidates = []
    for path in dict.fromkeys(package_candidates):
        if (
            path.name.startswith(".localsetup-")
            or is_package_backup_artifact(path)
            or package_has_other_refs(registry, path.name, target_root=target)
            or not path.exists()
            or not is_managed_package(path)
        ):
            continue
        _require_under_global_root(path, global_root)
        candidates.append(path)

    helper_path = (
        _shared_runtime_helper_path(source, global_root)
        if _registry_empty_after_target_removal(registry, target)
        else None
    )
    adapter_paths_existed = {path: path.exists() or path.is_symlink() for path in paths}
    receipt = journal_path(target, "rollback-" + uuid.uuid4().hex)
    journal = {"version": 1, "operation": "rollback", "status": "started", "touched": []}
    removed = []
    warnings = []

    try:
        for path in dict.fromkeys([lock_path, legacy_path, registry_path]):
            record_file_state(journal, receipt, path, os.replace)
        for path in paths:
            _snapshot_adapter(path, global_root, journal, receipt, recorded.get(str(path), {}).get("packages"))
        for path in candidates:
            record_node_state(journal, receipt, path, os.replace)
        if helper_path is not None:
            record_file_state(journal, receipt, helper_path, os.replace)

        for path in paths:
            if str(path) not in shared:
                continue
            action, expected = shared[str(path)]
            old = adapter_marker_packages(path) or set()
            write_overlap(source, home, target, action, journal, receipt)
            removed.extend(str(path / name) for name in sorted(old - set(expected)))

        for path in candidates:
            remove_path(path)
            removed.append(str(path))

        for path in paths:
            if str(path) in shared:
                continue
            removed.extend(
                remove_managed_adapter_entries(
                    path,
                    global_root,
                    known_global_roots=legacy_global_roots(home),
                    preserve_directory=True,
                )
            )

        registry_existed = registry_path.exists()
        registry_payload = remove_target(registry_path, target_root=target)
        if registry_existed and not registry_path.exists():
            removed.append(str(registry_path))

        if helper_path is not None and not (
            registry_payload.get("targets")
            or registry_payload.get("packages")
            or registry_payload.get("personal_owners")
        ):
            helper_path.unlink()
            removed.append(str(helper_path))

        for path in dict.fromkeys([lock_path, legacy_path]):
            if path.exists() or path.is_symlink():
                path.unlink()
                removed.append(str(path))

        journal["status"] = "committed"
        write_journal(receipt, journal)
    except Exception as original:
        journal["status"] = "failed"
        recovery_error = None
        try:
            restore_failed_mutations(journal, os.replace)
        except Exception as exc:
            recovery_error = exc
            journal["rollback_error"] = str(exc)
            if hasattr(original, "add_note"):
                original.add_note(f"Rollback restoration failed; inspect recovery journal and backups: {receipt}")
        if recovery_error is None:
            for path, existed in adapter_paths_existed.items():
                if existed:
                    continue
                try:
                    if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                        path.rmdir()
                except OSError as exc:
                    if hasattr(original, "add_note"):
                        original.add_note(f"Empty adapter cleanup failed for {path}: {exc}")
        try:
            write_journal(receipt, journal)
        except Exception as exc:
            note = f"Rollback recovery journal could not be updated at {receipt}: {exc}"
            if hasattr(original, "add_note"):
                original.add_note(note)
        if recovery_error is not None:
            raise original
        raise

    try:
        cleanup_backups(journal)
    except OSError as exc:
        warnings.append(f"rollback committed; backup cleanup failed: {exc}")

    empty_paths = [*paths, global_root]
    if helper_path is not None:
        empty_paths.append(helper_path.parent)
    empty_removed, empty_warnings = _empty_directory_cleanup(empty_paths)
    removed.extend(empty_removed)
    warnings.extend(empty_warnings)
    return {"removed": removed, "warnings": warnings, "journal": str(receipt)}
