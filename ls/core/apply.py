from __future__ import annotations

import json
from pathlib import Path
import os
import shutil
import time
import uuid

from .apply_journal import (
    RollbackError,
    prepare_legacy_lockfile_backup,
    remove_legacy_lockfile,
    cleanup_backups,
    cleanup_staging,
    journal_path,
    record_file_state,
    remove_path,
    restore_failed_mutations,
    staging_root,
    write_journal,
)
from .apply_lock import build_lock_payload
from .apply_packages import install_managed_packages
from .apply_preflight import (
    SAFE_ADAPTER_STATUS_CODES,
    codex_agent_source,
    preflight_install_plan,
    unsafe_same_name_adapter_entries,
)
from .lockfile import load_json, save_json
from .locking import package_root_lock
from .manifests import load_pack_config
from .models import DeployPlan
from .paths import ensure_dir, legacy_target_lockfile_path, repo_path, target_lockfile_path
from .path_contract import paths_manifest_path, write_paths_manifest
from .adapters import (
    ADAPTER_MARKER_JSON,
    adapter_marker_state,
    adapter_path_state,
    legacy_global_roots,
    remove_managed_adapter_entries,
    _is_safe_adapter_package_name,
)
from .package_cleanup import is_package_backup_artifact
from .registry import upsert_target
from .provenance import is_managed_package
from .source import source_commit


_prepare_legacy_lockfile_backup = prepare_legacy_lockfile_backup
_remove_legacy_lockfile = remove_legacy_lockfile
_cleanup_backups = cleanup_backups
_cleanup_staging = cleanup_staging
_codex_agent_source = codex_agent_source
_journal_path = journal_path
_remove_path = remove_path
_staging_root = staging_root
_unsafe_same_name_adapter_entries = unsafe_same_name_adapter_entries
_write_journal = write_journal


def _same_filesystem_replace(src: Path, dest: Path) -> None:
    os.replace(src, dest)


def _record_file_state(journal: dict, journal_path: Path, path: Path) -> None:
    record_file_state(journal, journal_path, path, _same_filesystem_replace)


def _restore_failed_mutations(journal: dict) -> None:
    restore_failed_mutations(journal, _same_filesystem_replace)


def _record_rollback_errors(journal: dict, exc: Exception) -> None:
    try:
        _restore_failed_mutations(journal)
    except RollbackError as rollback_exc:
        journal["rollback_errors"] = rollback_exc.errors
        exc.add_note(str(rollback_exc))
    except Exception as rollback_exc:
        journal["rollback_errors"] = [str(rollback_exc)]
        exc.add_note(f"rollback failed unexpectedly: {rollback_exc}")


def _persist_failed_journal(journal: dict, journal_path: Path, exc: Exception) -> None:
    try:
        _write_journal(journal_path, journal)
    except Exception as persistence_exc:
        message = f"failed to persist failed transaction journal: {persistence_exc}"
        journal.setdefault("journal_persistence_errors", []).append(message)
        exc.add_note(message)
        try:
            _write_journal(journal_path, journal)
        except Exception as retry_exc:
            exc.add_note(f"failed transaction journal retry also failed: {retry_exc}")


def _copy_backup(path: Path, backup: Path) -> None:
    if path.is_symlink():
        backup.symlink_to(path.readlink())
    elif path.is_dir():
        shutil.copytree(path, backup, symlinks=True)
    elif path.is_file():
        shutil.copy2(path, backup)
    else:
        raise RuntimeError(f"cannot back up unsupported filesystem node: {path}")
    if not (backup.exists() or backup.is_symlink()):
        raise RuntimeError(f"backup was not created: {backup}")


def _historical_recorded_packages(attachment_root: Path, historical_path: Path) -> tuple[bool, list[str]]:
    recorded = False
    packages: set[str] = set()
    for lock_path in (target_lockfile_path(attachment_root), legacy_target_lockfile_path(attachment_root)):
        payload = load_json(lock_path)
        if not isinstance(payload, dict):
            continue
        if str(historical_path) in {str(item) for item in payload.get("adapter_state", [])}:
            recorded = True
        for item in payload.get("adapter_targets", []):
            if isinstance(item, dict) and str(item.get("path")) == str(historical_path):
                recorded = True
                packages.update(str(name) for name in item.get("packages", []) if name)
        packages.update(str(name) for name in payload.get("repo_packages", []) if name)
        packages.update(str(name) for name in payload.get("adapter_packages", []) if name)
    return recorded, sorted(packages)


def _retire_historical_adapter(
    action,
    *,
    source: Path,
    attachment_root: Path,
    home: Path,
    journal: dict,
    journal_path: Path,
) -> list[str]:
    path = action.path
    if not (path.exists() or path.is_symlink()):
        return []
    global_root = Path(action.details["global_root"])
    from .historical_ownership import retained_historical_action
    retained = retained_historical_action(source, home, attachment_root, path, global_root)
    if retained is not None:
        from .repository_overlap import write_overlap
        from .adapter_markers import adapter_marker_packages
        replacement, expected = retained
        old = adapter_marker_packages(path) or set()
        write_overlap(source, home, attachment_root, replacement, journal, journal_path)
        removed = [str(path / name) for name in sorted(old - set(expected))]
        action.details.update(disposition='preserved-current-personal', removed=removed)
        return removed
    known_roots = legacy_global_roots(home)
    state = adapter_path_state(path, global_root, known_global_roots=known_roots, target_root=attachment_root)
    marker = adapter_marker_state(path) if path.is_dir() and not path.is_symlink() else {"exists": False}
    recorded, recorded_packages = _historical_recorded_packages(attachment_root, path)
    proven = bool(
        state["points_to_global"]
        or state["points_to_legacy_global"]
        or state.get("managed_visible_packages")
        or (marker.get("exists") and not marker.get("error") and marker.get("mode") in {"symlink", "portable"})
        or (recorded and not path.is_symlink())
    )
    if not proven:
        action.details["disposition"] = "preserved-unproven"
        return []

    if not path.is_symlink() and not path.is_dir():
        raise RuntimeError(f"historical adapter is not a supported symlink or directory: {path}")
    backup = path.with_name(f".{path.name}.localsetup-backup-{uuid.uuid4().hex}")
    existed = path.exists() or path.is_symlink()
    _copy_backup(path, backup)
    transition_id = str(action.details["id"])
    journal["touched"].append(
        {"kind": "adapter", "path": str(path), "backup": str(backup), "existed": existed, "transition": transition_id}
    )
    _write_journal(journal_path, journal)
    removed = remove_managed_adapter_entries(
        path,
        global_root,
        known_global_roots=known_roots,
        recorded_packages=recorded_packages,
    )
    action.details["disposition"] = "retired-managed-entries" if removed else "preserved-no-managed-entries"
    action.details["removed"] = removed
    return removed


def _install_managed_packages(
    repo_root: Path,
    global_root: Path,
    package_names: list[str],
    source_subdir: str,
    *,
    home: Path | None = None,
    staging_root: Path | None = None,
    journal: dict | None = None,
    journal_path: Path | None = None,
) -> list[str]:
    return install_managed_packages(
        repo_root,
        global_root,
        package_names,
        source_subdir,
        home=home,
        replace_func=_same_filesystem_replace,
        staging_root=staging_root,
        journal=journal,
        journal_path=journal_path,
    )


def _install_managed_skills(repo_root: Path, global_root: Path, skill_names: list[str]) -> list[str]:
    return _install_managed_packages(repo_root, global_root, skill_names, "skills")


def _install_managed_workflows(repo_root: Path, global_root: Path, workflow_names: list[str]) -> list[str]:
    return _install_managed_packages(repo_root, global_root, workflow_names, "workflows")


def _install_codex_agents(repo_root: Path, agents_root: Path, agent_names: list[str]) -> list[str]:
    ensure_dir(agents_root)
    installed: list[str] = []
    for agent_name in sorted(set(str(name) for name in agent_names)):
        src = _codex_agent_source(repo_root, agent_name)
        dest = agents_root / f"{agent_name}.toml"
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        installed.append(str(dest))
    return installed


def _prune_unreferenced_managed_packages(
    global_root: Path,
    registry: dict,
    *,
    journal: dict,
    journal_path: Path,
) -> list[str]:
    if not global_root.exists():
        return []
    referenced = {
        name
        for name, package in registry.get("packages", {}).items()
        if isinstance(package, dict) and package.get("refs")
    }
    removed: list[str] = []
    for path in sorted(global_root.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".localsetup-") or is_package_backup_artifact(path) or path.name in referenced:
            continue
        if not is_managed_package(path):
            continue
        backup = path.with_name(f".{path.name}.localsetup-backup-{uuid.uuid4().hex}")
        _copy_backup(path, backup)
        journal.setdefault("touched", []).append(
            {"kind": "managed_package", "path": str(path), "backup": str(backup), "existed": True}
        )
        _write_journal(journal_path, journal)
        _remove_path(path)
        removed.append(str(path))
    return removed


def _write_scoped_adapter(adapter_path: Path, global_root: Path, package_names: list[str], *, mode: str, mutable: bool = False) -> None:
    from .mutable_adapters import prepare_write, receipt_fields
    expected = prepare_write(adapter_path, global_root, package_names, mode, mutable=mutable)
    if adapter_path.is_symlink() and adapter_path.exists() and adapter_path.is_dir():
        pass
    else:
        ensure_dir(adapter_path)
    portable_marker = adapter_path / ".localsetup-portable"
    if mode != "portable" and portable_marker.exists():
        portable_marker.unlink()
    selected = set(package_names)
    old_marker = adapter_path / ADAPTER_MARKER_JSON
    old_packages: set[str] = set()
    if old_marker.is_file():
        try:
            old_payload = json.loads(old_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old_payload = {}
        old_raw = old_payload.get("packages") if isinstance(old_payload, dict) else None
        if isinstance(old_raw, list):
            old_packages = {str(item) for item in old_raw if _is_safe_adapter_package_name(str(item))}
    for stale_package in sorted(old_packages - selected):
        stale_path = adapter_path / stale_package
        if _is_current_managed_adapter_entry(stale_path, global_root, mode):
            _remove_path(stale_path)
    for package_name in sorted(set(package_names)):
        source = global_root / package_name
        if not source.is_dir():
            raise RuntimeError(f"selected package is missing from managed library: {source}")
        target = adapter_path / package_name
        if target.exists() or target.is_symlink():
            _remove_path(target)
        if mode == "portable":
            shutil.copytree(source, target)
        else:
            target.symlink_to(source, target_is_directory=True)
    save_json(
        adapter_path / ADAPTER_MARKER_JSON,
        {
            "version": 1,
            "managed_by": "localsetup",
            "mode": mode,
            "global_root": str(global_root),
            "packages": sorted(set(package_names)),
            **receipt_fields(adapter_path, expected),
        },
    )
    if mode == "portable":
        (adapter_path / ".localsetup-portable").write_text("managed_by=localsetup\n", encoding="utf-8")


def _is_current_managed_adapter_entry(path: Path, global_root: Path, mode: str) -> bool:
    if mode == "portable":
        return path.is_dir() and not path.is_symlink() and is_managed_package(path)
    if not path.is_symlink():
        return False
    link_target = path.readlink()
    if not link_target.is_absolute():
        link_target = path.parent / link_target
    return link_target.resolve(strict=False) == (global_root / path.name).resolve(strict=False)


def apply_plan(
    repo_root: Path,
    plan: DeployPlan,
    home: Path,
    dry_run: bool = False,
    dependency_info: dict | None = None,
    target_root: Path | None = None,
) -> dict:
    if dry_run:
        return _apply_plan_unlocked(repo_root, plan, home, dry_run=True, dependency_info=dependency_info, target_root=target_root)
    with package_root_lock(home / ".local" / "share" / "localsetup") as lock:
        result = _apply_plan_unlocked(
            repo_root,
            plan,
            home,
            dry_run=False,
            dependency_info=dependency_info,
            target_root=target_root,
        )
        result["package_root_lock"] = lock
        return result


def _apply_plan_unlocked(
    repo_root: Path,
    plan: DeployPlan,
    home: Path,
    dry_run: bool = False,
    dependency_info: dict | None = None,
    target_root: Path | None = None,
) -> dict:
    executed: list[str] = []
    installed_skills: list[str] = []
    installed_workflows: list[str] = []
    installed_codex_agents: list[str] = []
    metadata_target_root = plan.rollback_metadata.get("target_root")
    metadata_attachment_root = Path(metadata_target_root) if metadata_target_root else None
    if target_root is not None and metadata_attachment_root is not None:
        if target_root.resolve(strict=False) != metadata_attachment_root.resolve(strict=False):
            raise ValueError("target_root does not match install plan target_root")
    attachment_root = target_root or metadata_attachment_root or repo_root
    preflight = preflight_install_plan(repo_root, plan, home, target_root=attachment_root)
    if not preflight["ok"]:
        raise RuntimeError(f"install preflight failed: {preflight['blockers']}")
    preserved_codex_agents = preflight["preserved_codex_agents"]
    preserved_codex_agent_targets = {(row["name"], row["path"]) for row in preserved_codex_agents}
    from .adapter_coalescing import paired_repository_actions
    pairs = paired_repository_actions(plan)
    txid = uuid.uuid4().hex
    journal_path = _journal_path(attachment_root, txid)
    journal = {
        "version": 1,
        "txid": txid,
        "status": "started",
        "started_at_unix": int(time.time()),
        "source_commit": source_commit(repo_root),
        "target_root": str(attachment_root),
        "touched": [],
    }
    if not dry_run:
        ensure_dir(journal_path.parent)
        _write_journal(journal_path, journal)
    try:
        for action in plan.actions:
            if action.kind == "ensure_dir":
                if not dry_run:
                    ensure_dir(action.path)
                executed.append(f"ensure_dir:{action.path}")
            elif action.kind == "write_registry":
                if not dry_run:
                    ensure_dir(action.path.parent)
                    journal["touched"].append({"kind": "registry", "path": str(action.path)})
                    _write_journal(journal_path, journal)
                executed.append(f"write_registry:{action.path}")
            elif action.kind == "install_skills":
                if not dry_run:
                    installed_skills = _install_managed_packages(
                        repo_root,
                        action.path,
                        action.details["skills"],
                        "skills",
                        home=home,
                        staging_root=_staging_root(action.path, txid),
                        journal=journal,
                        journal_path=journal_path,
                    )
                executed.append(f"install_skills:{action.path}")
            elif action.kind == "install_workflows":
                if not dry_run:
                    installed_workflows = _install_managed_packages(
                        repo_root,
                        action.path,
                        action.details["workflows"],
                        "workflows",
                        home=home,
                        staging_root=_staging_root(action.path, txid),
                        journal=journal,
                        journal_path=journal_path,
                    )
                executed.append(f"install_workflows:{action.path}")
            elif action.kind == "install_codex_agents":
                install_names = [
                    str(name)
                    for name in action.details.get("agents", [])
                    if (str(name), str(action.path / f"{name}.toml")) not in preserved_codex_agent_targets
                ]
                if install_names:
                    if not dry_run:
                        ensure_dir(action.path)
                        for name in install_names:
                            _record_file_state(journal, journal_path, action.path / f"{name}.toml")
                        installed_codex_agents = _install_codex_agents(repo_root, action.path, install_names)
                    executed.append(f"install_codex_agents:{action.path}")
            elif action.kind == "retire_historical_adapter":
                if any(a.kind == 'attach_personal_path' and a.path == action.path for a in plan.actions):
                    action.details['disposition'] = 'delegated-current-personal'
                elif not dry_run:
                    _retire_historical_adapter(
                        action,
                        source=repo_root,
                        attachment_root=attachment_root,
                        home=home,
                        journal=journal,
                        journal_path=journal_path,
                    )
                executed.append(f"retire_historical_adapter:{action.path}")
            elif action.kind == "attach_personal_path":
                if not dry_run:
                    from .personal_adapter import write
                    pair = pairs.get(action.path)
                    retiring = any(a.kind == 'retire_historical_adapter' and a.path == action.path for a in plan.actions)
                    write(repo_root, home, action, journal, journal_path,
                          repository_target=attachment_root if pair or retiring else None,
                          repository_packages=pair.details.get("packages", []) if pair else [] if retiring else None)
                executed.append(f"attach_personal_path:{action.path}")
            elif action.kind == "attach_repo_path":
                if action.path in pairs:
                    executed.append(f"attach_repo_path:{action.path}")
                    continue
                if not dry_run:
                    from .repository_overlap import write_overlap
                    if write_overlap(repo_root, home, attachment_root, action, journal, journal_path):
                        executed.append(f"attach_repo_path:{action.path}")
                        continue
                    ensure_dir(action.path.parent)
                    mode = action.details.get("mode", "symlink")
                    global_root = Path(action.details["global_root"])
                    package_names = [str(name) for name in action.details.get("packages", [])]
                    state = adapter_path_state(
                        action.path,
                        global_root,
                        known_global_roots=legacy_global_roots(home),
                        target_root=attachment_root,
                    )
                    backup = action.path.with_name(f".{action.path.name}.localsetup-backup-{uuid.uuid4().hex}")
                    existed = action.path.exists() or action.path.is_symlink()
                    in_place = state["status_code"] in {
                        "custom_repo_skills",
                        "managed_scoped_adapter",
                        "managed_portable_adapter",
                        "mixed_managed_custom_adapter",
                        "shared_adapter_directory",
                    }
                    if existed:
                        if not in_place and state["collision_reason"]:
                            raise RuntimeError(f"refusing to replace {state['collision_reason']} at adapter path: {action.path}")
                        _copy_backup(action.path, backup)
                    journal["touched"].append(
                        {
                            "kind": "adapter",
                            "path": str(action.path),
                            "backup": str(backup),
                            "existed": existed,
                        }
                    )
                    _write_journal(journal_path, journal)
                    if existed and not in_place:
                        _remove_path(action.path)
                    _write_scoped_adapter(action.path, global_root, package_names, mode=mode,
                                          mutable=action.details.get("mutable_copy", False))
                executed.append(f"attach_repo_path:{action.path}")
    except Exception as exc:
        if not dry_run:
            journal["status"] = "failed"
            journal["failed_at_unix"] = int(time.time())
            journal["error"] = str(exc)
            _cleanup_staging(journal)
            _record_rollback_errors(journal, exc)
            _persist_failed_journal(journal, journal_path, exc)
        raise

    pack = load_pack_config(repo_root)
    lockfile_path = repo_path(attachment_root, pack.lockfile, "repo.lockfile")
    if lockfile_path.name != "lock.json" or lockfile_path.parent.name != ".localsetup":
        lockfile_path = target_lockfile_path(attachment_root)
    lock_payload = build_lock_payload(
        repo_root=repo_root,
        home=home,
        attachment_root=attachment_root,
        pack=pack,
        plan=plan,
        installed_skills=installed_skills,
        installed_workflows=installed_workflows,
        installed_codex_agents=installed_codex_agents,
        preserved_codex_agents=preserved_codex_agents,
        dependency_info=dependency_info,
    )
    registry_actions = [a for a in plan.actions if a.kind == "write_registry"]
    if registry_actions:
        lock_payload["registry_path"] = str(registry_actions[0].path)
    global_roots = [a.path for a in plan.actions if a.kind in {"install_skills", "install_workflows"}]
    if global_roots:
        lock_payload["package_root"] = str(global_roots[0])
    legacy_lockfile = legacy_target_lockfile_path(attachment_root)
    if legacy_lockfile.exists() and legacy_lockfile != lockfile_path:
        lock_payload["migration_origin"] = {"legacy_lockfile": str(legacy_lockfile)}
    if not dry_run:
        try:
            from .mutable_ownership import mark_mutable_receipts
            mark_mutable_receipts([*lock_payload["adapter_targets"], *lock_payload["personal_adapter_targets"]])
            _record_file_state(journal, journal_path, paths_manifest_path(home))
            paths_manifest = write_paths_manifest(repo_root, home)
            journal["touched"].append({"kind": "paths_manifest", "path": str(paths_manifest["manifest"])})
            _write_journal(journal_path, journal)
            if registry_actions:
                _record_file_state(journal, journal_path, registry_actions[0].path)
                registry_payload = upsert_target(
                    registry_actions[0].path,
                    target_root=attachment_root,
                    source_commit=source_commit(repo_root),
                    package_paths=[Path(path) for path in [*installed_skills, *installed_workflows]],
                    adapter_targets=[*lock_payload["adapter_targets"], *lock_payload["personal_adapter_targets"]],
                    global_baseline={
                        "selectors": lock_payload["global_baseline_selectors"],
                        "packs": lock_payload["global_baseline_packs"],
                        "skills": lock_payload["global_baseline_skills"],
                        "workflows": lock_payload["global_baseline_workflows"],
                        "packages": lock_payload["global_baseline_packages"],
                    },
                    repo_selection={
                        "selectors": lock_payload["repo_selectors"],
                        "packs": lock_payload["repo_packs"],
                        "skills": lock_payload["repo_skills"],
                        "workflows": lock_payload["repo_workflows"],
                        "packages": lock_payload["repo_packages"],
                    },
                )
                if global_roots:
                    pruned = _prune_unreferenced_managed_packages(
                        global_roots[0],
                        registry_payload,
                        journal=journal,
                        journal_path=journal_path,
                    )
                    if pruned:
                        lock_payload["pruned_packages"] = pruned
            _record_file_state(journal, journal_path, lockfile_path)
            legacy_backup = None
            if legacy_lockfile.exists() and legacy_lockfile != lockfile_path:
                legacy_backup = _prepare_legacy_lockfile_backup(legacy_lockfile, attachment_root, txid)
                journal["touched"].append(
                    {
                        "kind": "legacy_lockfile",
                        "path": str(legacy_lockfile),
                        "backup": legacy_backup,
                        "existed": True,
                    }
                )
                _write_journal(journal_path, journal)
                _remove_legacy_lockfile(legacy_lockfile)
                lock_payload["migration_origin"]["backup"] = legacy_backup
            journal["touched"].append({"kind": "lockfile", "path": str(lockfile_path)})
            _write_journal(journal_path, journal)
            save_json(lockfile_path, lock_payload)
            journal["status"] = "committed"
            journal["committed_at_unix"] = int(time.time())
            _cleanup_staging(journal)
            _cleanup_backups(journal)
            _write_journal(journal_path, journal)
        except Exception as exc:
            journal["status"] = "failed"
            journal["failed_at_unix"] = int(time.time())
            journal["error"] = str(exc)
            _cleanup_staging(journal)
            _record_rollback_errors(journal, exc)
            _persist_failed_journal(journal, journal_path, exc)
            raise
    return {
        "executed": executed,
        "lockfile": str(lockfile_path),
        "paths_manifest": str(paths_manifest_path(home)) if dry_run else str(paths_manifest["manifest"]),
        "dry_run": dry_run,
        "transaction": txid if not dry_run else None,
        "journal": str(journal_path) if not dry_run else None,
        "preflight": preflight,
        "installed_codex_agents": installed_codex_agents,
        "preserved_codex_agents": preserved_codex_agents,
    }
