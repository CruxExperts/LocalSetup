"""Opt-in mutable-copy receipts within the existing adapter transaction marker."""
from __future__ import annotations

import json
from pathlib import Path

from .adapter_markers import ADAPTER_MARKER_JSON
from .mutable_packages import MutablePackageError, capture_baselines, require_unchanged


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:raise MutablePackageError('Ambiguous mutable adapter receipt')
        result[key] = value
    return result


def check_existing(path: Path, *, required: bool = False) -> dict | None:
    """Validate opted-in package copies against their last committed baseline.

    A recorded mutable owner must pass required=True even if its marker vanished.
    Caller validates ancestors and coordinates native writers for the transaction.
    """
    marker = path / ADAPTER_MARKER_JSON
    if not marker.exists() and not marker.is_symlink():
        if required:raise MutablePackageError('Recorded mutable adapter receipt is missing')
        return None
    if marker.is_symlink() or not marker.is_file():
        raise MutablePackageError('Unsafe mutable adapter receipt')
    try:
        with marker.open('rb') as handle:raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:raise MutablePackageError('Mutable adapter receipt exceeds supported size')
        payload = json.loads(raw, object_pairs_hook=_pairs)
    except (OSError, ValueError, RecursionError) as exc:
        raise MutablePackageError('Mutable adapter receipt requires preservation review') from exc
    if not isinstance(payload, dict):raise MutablePackageError('Invalid mutable adapter receipt')
    if 'mutable_packages' not in payload:
        if required:raise MutablePackageError('Recorded mutable adapter baseline is missing')
        return None
    baseline = payload['mutable_packages']
    names = payload.get('packages')
    if (payload.get('mode') != 'portable' or not isinstance(baseline, dict)
            or not isinstance(names, list) or any(not isinstance(n, str) for n in names)
            or len(names) != len(set(names)) or set(names) != set(baseline)):
        raise MutablePackageError('Mutable adapter receipt does not match its package selection')
    require_unchanged(path, baseline)
    return baseline


def prepare_write(path: Path, library: Path, names: list[str], mode: str, *, mutable: bool = False,
                  bindings: dict | None = None) -> dict | None:
    """Check drift before any write and qualify the intended independent payload."""
    if mutable and (path.is_symlink() or (path.exists() and not path.is_dir())):
        raise MutablePackageError("Mutable adapter must be a regular directory")
    prior = check_existing(path)
    if not mutable and prior is None:return None
    if mode != 'portable':raise MutablePackageError('Mutable adapters require portable independent copies')
    if not bindings:
        return capture_baselines(library, names)
    from .package_bindings import package_source
    result = {}
    for name in names:
        source = package_source(library, name, bindings=bindings)
        result.update(capture_baselines(source.parent, [name]))
    return result


def receipt_fields(path: Path, expected: dict | None) -> dict:
    """Check completed copies before committing the new baseline with their marker."""
    if expected is None:return {}
    actual = capture_baselines(path, list(expected))
    if actual != expected:
        raise MutablePackageError('Mutable adapter copies differ from their prepared payload')
    return {'mutable_packages': actual}
