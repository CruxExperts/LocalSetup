"""Preserve personal owners when updating a repository on a shared path."""
from pathlib import Path
from .installation_ownership import InstallationOwner
from .manifests import load_pack_config
from .models import PlanAction
from .paths import expand_user_path
from .personal_adapter import selection, write
from .personal_registry import owner_key
from .registry import load_registry


def overlap_action(source: Path, home: Path, action):
    registry = load_registry(expand_user_path(load_pack_config(source).global_registry, home))
    records = registry.get('personal_owners', {})
    if not isinstance(records, dict):raise ValueError('Personal owners must be a mapping')
    if any(not isinstance(row, dict) or not isinstance(row.get('paths'), list) for row in records.values()):
        raise ValueError('Invalid personal owner paths')
    rows = [row for row in records.values() if str(action.path) in row['paths']]
    if not rows:return None
    mode = action.details.get('mode', 'symlink')
    if any(row.get('mode', 'symlink') != mode for row in rows):
        raise ValueError('Repository adapter mode conflicts with a personal owner')
    owner_packages = {owner_key(InstallationOwner(**row['owner'])): row['packages'] for row in rows}
    from .package_bindings import merge_binding_selections
    bindings = merge_binding_selections(
        (action.details.get('packages', []), action.details.get('package_bindings', {})),
        *((row.get('packages', []), row.get('package_bindings', {})) for row in rows))
    proxy = PlanAction('attach_personal_path', action.path, {
        'owners': [row['owner'] for row in rows], 'owner_packages': owner_packages,
        'packages': sorted({name for row in rows for name in row['packages']}),
        'mode': mode, 'global_root': action.details['global_root'],
        'package_bindings': bindings,
    })
    return proxy


def expected_overlap(source: Path, home: Path, target: Path, action) -> list[str] | None:
    proxy = overlap_action(source, home, action)
    if proxy is None:return None
    return selection(source, home, proxy, repository_target=target, repository_packages=action.details.get('packages', []))


def check_overlap(source: Path, home: Path, target: Path, action) -> bool:
    return expected_overlap(source, home, target, action) is not None


def write_overlap(source: Path, home: Path, target: Path, action, journal: dict, journal_path: Path,
                  *, all_actions: list | None = None) -> bool:
    proxy = overlap_action(source, home, action)
    if proxy is None:return False
    proxy.details['legacy_alias_retirements'] = list(action.details.get('legacy_alias_retirements', []))
    action_context = [*(all_actions or [action]), proxy]
    write(source, home, proxy, journal, journal_path, repository_target=target,
          repository_packages=action.details.get('packages', []), attachment_target=target,
          all_actions=action_context)
    return True
