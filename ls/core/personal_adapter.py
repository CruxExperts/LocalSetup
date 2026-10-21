"""Personal adapters preserve neighbors and independent installation owners."""
import os
import shutil
from pathlib import Path
from .adapter_markers import ADAPTER_MARKER_JSON, adapter_marker_packages, adapter_marker_state, is_safe_adapter_package_name
from .adapters import adapter_path_state
from .apply_journal import record_file_state, record_node_state, remove_path
from .provenance import is_managed_package
from .installation_ownership import InstallationOwner
from .lockfile import save_json
from .manifests import load_pack_config
from .paths import expand_user_path
from .personal_registry import owner_key, personal_selections
from .registry import load_registry


def check_path(path: Path, home: Path) -> None:
    home = home.absolute();path = path.absolute()
    try:parts = path.relative_to(home).parts
    except ValueError:raise ValueError("Personal adapter escapes home") from None
    if not parts or ".." in parts:
        raise ValueError("Invalid personal adapter path")
    current = home
    for part in (None, *parts):
        if part is not None:current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError("Personal adapter ancestors must be regular directories")


def selection(repo_root: Path, home: Path, action, *, repository_target: Path | None = None,
              repository_packages: list[str] | None = None) -> list[str]:
    check_path(action.path, home)
    mode = action.details.get("mode", "symlink")
    if mode not in {"symlink", "portable"}:
        raise ValueError("Invalid personal adapter mode")
    marker = action.path / ADAPTER_MARKER_JSON
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise ValueError("Unsafe personal adapter marker")
    state = adapter_marker_state(action.path)
    if state["error"]:
        raise ValueError("Personal adapter marker requires preservation review")
    owners = [InstallationOwner(**raw) for raw in action.details["owners"]]
    if not owners or any(owner.scope != "personal" or owner.root != str(home.resolve()) for owner in owners):
        raise ValueError("Personal adapter owner does not match home")
    selected = {owner_key(owner) for owner in owners}
    names = set().union(*personal_selections(action.details).values())
    registry = load_registry(expand_user_path(load_pack_config(repo_root).global_registry, home))
    for key, record in registry.get("personal_owners", {}).items():
        if key not in selected and str(action.path) in record.get("paths", []):
            if record.get("mode", "symlink") != mode:
                raise ValueError("Personal adapter mode conflicts with another owner")
            names.update(record.get("packages", []))
    names.update(repository_packages or [])
    for target_id, target in registry.get("targets", {}).items():
        if repository_target is not None and target_id == str(repository_target.resolve()):continue
        for adapter in target.get("adapters", []):
            repository_owned = any(o.get("scope") == "repo" for o in adapter.get("owners", []))
            if "owners" not in adapter:
                repository_owned = bool(adapter.get("platforms") or adapter.get("platform"))
            if adapter.get("path") == str(action.path) and repository_owned:
                if adapter.get("mode", "symlink") != mode:
                    raise ValueError("Personal adapter mode conflicts with a repository owner")
                names.update(adapter.get("packages", []))
    if any(not isinstance(name, str) or not is_safe_adapter_package_name(name) for name in names):
        raise ValueError("Invalid personal adapter package name")
    global_root = Path(action.details["global_root"])
    state = adapter_path_state(action.path, global_root, target_root=home)
    if state["collision_reason"]:
        raise ValueError("Personal adapter content requires preservation review")
    for name in names:
        entry = action.path / name
        if entry.exists() or entry.is_symlink():
            if not managed_entry(entry, global_root, adapter_marker_state(action.path)["mode"]):
                raise ValueError("Personal adapter has a custom package name collision")
    return sorted(names)


def managed_entry(entry: Path, global_root: Path, mode: str | None) -> bool:
    if entry.is_symlink():
        return entry.resolve(strict=False) == (global_root / entry.name).resolve(strict=False)
    return mode == "portable" and entry.is_dir() and is_managed_package(entry)


def write(repo_root: Path, home: Path, action, journal: dict, journal_path: Path, *,
          repository_target: Path | None = None, repository_packages: list[str] | None = None,
          attachment_target: Path | None = None, all_actions: list | None = None) -> None:
    names = selection(repo_root, home, action, repository_target=repository_target,
                      repository_packages=repository_packages)
    from .adapter_alias_reconciliation import revalidate_alias_retirements
    retirement_aliases = revalidate_alias_retirements(
        repo_root, home, attachment_target or repository_target or repo_root,
        all_actions or [action], action
    )
    write_entries(home, action, names, journal, journal_path,
                  retire_aliases=retirement_aliases)


def write_entries(
    boundary: Path,
    action,
    names: list[str],
    journal: dict,
    journal_path: Path,
    *,
    retire_aliases: list[str] | None = None,
) -> None:
    """Write a validated physical selection; caller owns selection authority and locking."""
    check_path(action.path, boundary)
    if any(not isinstance(name, str) or not is_safe_adapter_package_name(name) for name in names):
        raise ValueError("Invalid adapter package name")
    global_root = Path(action.details["global_root"])
    if any(not (global_root / name).is_dir() for name in names):
        raise ValueError("Personal adapter package is missing from the managed library")
    from .mutable_adapters import prepare_write, receipt_fields
    expected = prepare_write(action.path, global_root, names, action.details.get("mode", "symlink"),
                             mutable=action.details.get("mutable_copy", False))
    action.path.mkdir(parents=True, exist_ok=True)
    old = adapter_marker_packages(action.path) or set()
    old_mode = adapter_marker_state(action.path)["mode"]
    mode = action.details.get("mode", "symlink")
    retire = set(retire_aliases or []) - set(names)
    for name in sorted(old | set(names) | retire):
        check_path(action.path, boundary)
        entry = action.path / name
        managed = managed_entry(entry, global_root, old_mode)
        if name not in names and not managed and name not in retire:continue
        record_node_state(journal, journal_path, entry, os.replace)
        if managed or name in retire:remove_path(entry)
        if name in names:
            if mode == "portable":shutil.copytree(global_root / name, entry, symlinks=True)
            else:entry.symlink_to(global_root / name, target_is_directory=True)
    check_path(action.path, boundary)
    marker = action.path / ADAPTER_MARKER_JSON
    record_file_state(journal, journal_path, marker, os.replace)
    save_json(marker, {"version": 1, "managed_by": "localsetup", "mode": mode,
                       "global_root": str(global_root), "packages": names,
                       **receipt_fields(action.path, expected)})
