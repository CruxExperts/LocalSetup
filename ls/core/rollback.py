from __future__ import annotations

from pathlib import Path

from .adapters import validate_platform_selectors
from .lockfile import load_json
from .manifests import load_pack_config
from .paths import expand_user_path, legacy_target_lockfile_path, repo_path, target_lockfile_path


def _require_under_global_root(path: Path, global_root: Path, *, binding: dict | None = None) -> None:
    if binding is not None:
        from .skills_external import binding_artifact_path
        from .package_bindings import verify_binding
        if path.absolute() != binding_artifact_path(global_root, binding).absolute():
            raise RuntimeError("External rollback package differs from its recorded binding")
        # Removal verifies the recorded bytes and ownership, not permission to
        # execute them under today's integration contract.
        verify_binding(global_root, binding, allow_stale_contract=True)
        return
    resolved_path = path.resolve(strict=False)
    resolved_root = global_root.resolve(strict=False)
    try:
        if resolved_path.parent != resolved_root:
            raise ValueError("managed packages must be direct children of the package root")
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise RuntimeError(f"refusing to rollback managed package outside global root: {path}") from exc


def _require_adapter_under_target_root(path: Path, target_root: Path) -> None:
    resolved_parent = path.parent.resolve(strict=False)
    resolved_root = target_root.resolve(strict=False)
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError as exc:
        raise RuntimeError(f"refusing to rollback adapter outside target root: {path}") from exc


def _shared_runtime_helper_path(repo_root: Path, global_root: Path) -> Path | None:
    source = repo_root / "ls" / "lib" / "deps.py"
    target = global_root.parent / "lib" / "deps.py"
    if not source.is_file() or not target.is_file() or target.is_symlink():
        return None
    if source.read_bytes() != target.read_bytes():
        return None
    return target


def _rollback_locked(
    repo_root: Path,
    home: Path,
    platform_ids: list[str] | None = None,
    *,
    target_root: Path | None = None,
) -> dict:
    validate_platform_selectors(repo_root, platform_ids)
    if platform_ids:
        raise ValueError("platform-scoped rollback is not supported in the current framework; run full rollback to remove shared managed state")

    attachment_root = target_root or repo_root
    pack = load_pack_config(repo_root)
    lock_path = repo_path(attachment_root, pack.lockfile, "repo.lockfile")
    if lock_path.name != "lock.json" or lock_path.parent.name != ".localsetup":
        lock_path = target_lockfile_path(attachment_root)
    lock = load_json(lock_path)
    legacy_lock = legacy_target_lockfile_path(attachment_root)
    if not lock and legacy_lock.exists():
        lock = load_json(legacy_lock)
    global_root = expand_user_path(pack.global_root, home)
    package_paths = [Path(value) for value in [*lock.get("installed_skills", []), *lock.get("installed_workflows", [])]]
    adapter_paths = [Path(value) if Path(value).is_absolute() else attachment_root / value
                     for value in lock.get("adapter_state", [])]
    from .mutable_ownership import require_owned_copies
    require_owned_copies(repo_root, home, adapter_paths, target=attachment_root)
    bindings = lock.get("package_bindings", {})
    for path in package_paths:
        _require_under_global_root(path, global_root, binding=bindings.get(path.name))
    for path in adapter_paths:_require_adapter_under_target_root(path, attachment_root)
    from .shared_rollback import shared_rollback
    return shared_rollback(repo_root, home, attachment_root, lock, lock_path, legacy_lock)


def rollback(
    repo_root: Path, home: Path, platform_ids: list[str] | None = None, *, target_root: Path | None = None,
) -> dict:
    from .locking import package_root_lock
    from .paths import global_layout
    with package_root_lock(global_layout(home).localsetup_home):
        return _rollback_locked(repo_root, home, platform_ids, target_root=target_root)
