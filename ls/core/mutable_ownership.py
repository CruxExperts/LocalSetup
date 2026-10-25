"""Independent mutable ownership receipts and selected-target apply preflight."""
from __future__ import annotations

from pathlib import Path

from .adapter_markers import is_safe_adapter_package_name
from .lockfile import load_json
from .manifests import load_pack_config
from .mutable_adapters import check_existing
from .mutable_packages import MutablePackageError
from .paths import expand_user_path, target_lockfile_path, legacy_target_lockfile_path
from .registry import load_registry


def mark_mutable_receipts(rows: list[dict]) -> None:
    """Called after writes, inside the transaction that commits these receipt rows."""
    for row in rows:
        if check_existing(Path(row['path'])) is not None:
            row['mutable_copy'] = True


def _names(value):
    if not isinstance(value, list) or any(not isinstance(n, str) or not is_safe_adapter_package_name(n) for n in value):
        raise MutablePackageError('Invalid recorded mutable package selection')
    return set(value)


def _records(registry, lock):
    def repository_row(row):
        if 'owners' not in row:return True
        return any(owner.get('scope') == 'repo' for owner in row['owners'])
    rows = []
    for target in registry.get('targets', {}).values():
        rows.extend(row for row in target.get('adapters', []) if repository_row(row))
    for key in ('adapter_targets', 'personal_adapter_targets'):
        rows.extend(row for row in lock.get(key, [])
                    if repository_row(row) or 'personal_owners' not in registry)
    for owner in registry.get('personal_owners', {}).values():
        mutable_paths = owner.get('mutable_paths', [])
        if not isinstance(mutable_paths, list) or not set(mutable_paths) <= set(owner.get('paths', [])):
            raise MutablePackageError('Invalid recorded mutable owner paths')
        for path in owner.get('paths', []):
            rows.append({'path': path, 'packages': owner.get('packages', []),
                         'package_bindings': owner.get('package_bindings', {}),
                         'mode': owner.get('mode'), 'mutable_copy': path in mutable_paths})
    return rows


def require_owned_copies(source: Path, home: Path, paths, *, target: Path | None = None) -> set[str]:
    """Validate selected physical paths; unrelated recorded adapters do not block work."""
    selected = {str(Path(p).absolute()) for p in paths}
    if not selected:return set()
    try:
        registry = load_registry(expand_user_path(load_pack_config(source).global_registry, home))
        lock = {}
        if target is not None:
            receipt = target_lockfile_path(target)
            if not receipt.exists() and not receipt.is_symlink():receipt = legacy_target_lockfile_path(target)
            lock = load_json(receipt)
        expected = {};mutable = set()
        for row in _records(registry, lock):
            raw = row.get('path')
            if not isinstance(raw, str) or raw not in selected:continue
            originals = {name for name, binding in row.get('package_bindings', {}).items()
                         if binding.get('disposition') == 'external_present'}
            expected.setdefault(raw, set()).update(_names(row.get('packages', [])) - originals)
            flag = row.get('mutable_copy', False)
            if not isinstance(flag, bool):raise MutablePackageError('Invalid recorded mutable owner designation')
            if flag:
                if row.get('mode', 'symlink') != 'portable':
                    raise MutablePackageError('Recorded mutable owner requires portable copies')
                mutable.add(raw)
        for raw in sorted(mutable):
            path = Path(raw)
            if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
                raise MutablePackageError('Mutable ownership path has unsafe ancestors')
            baseline = check_existing(path, required=True)
            if set(baseline) != expected[raw]:
                raise MutablePackageError('Mutable baseline differs from recorded physical package ownership')
        return mutable
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        raise MutablePackageError('Recorded mutable copies require preservation review before this operation') from exc


def retire_empty_baselines(paths, registry: dict) -> None:
    """Retire empty receipts only after the last mutable owner leaves.

    Caller holds the package lock and has journaled each adapter marker before
    writing its final package selection. The supplied registry is the pending
    authoritative ownership state, not historical target receipts.
    """
    from .adapter_markers import ADAPTER_MARKER_JSON
    from .lockfile import save_json
    retained = {row.get('path') for row in _records(registry, {}) if row.get('mutable_copy')}
    for path in paths:
        if str(path) in retained or check_existing(path) != {}:continue
        marker = path / ADAPTER_MARKER_JSON
        payload = load_json(marker)
        payload.pop('mutable_packages')
        save_json(marker, payload)
