"""Detach explicit personal owners and reconcile their current receipts."""
import copy
import os
import uuid
from pathlib import Path

from .apply_journal import record_file_state, write_journal, restore_failed_mutations, cleanup_backups
from .installation_ownership import InstallationOwner
from .locking import package_root_lock
from .lockfile import load_json, save_json
from .manifests import load_pack_config
from .models import PlanAction
from .paths import expand_user_path, global_layout
from .personal_adapter import selection, write
from .personal_inventory import personal_inventory
from .personal_registry import owner_key, personal_selections
from .registry import load_registry


def _without_owners(adapters, selected):
    result = []
    for adapter in adapters:
        personal = [o for o in adapter.get('owners', []) if o.get('scope') == 'personal']
        if not personal or not any(owner_key(InstallationOwner(**o)) in selected for o in personal):
            result.append(copy.deepcopy(adapter))
            continue
        selections = personal_selections(adapter)
        owners = [o for o in personal if owner_key(InstallationOwner(**o)) not in selected]
        if not owners:
            continue
        row = copy.deepcopy(adapter)
        row['owners'] = owners
        row['platforms'] = sorted(o['client'] for o in owners)
        row['owner_packages'] = {owner_key(InstallationOwner(**o)): sorted(selections[owner_key(InstallationOwner(**o))]) for o in owners}
        row['packages'] = sorted(set().union(*(set(v) for v in row['owner_packages'].values())))
        row['package_bindings'] = {name: binding for name, binding in row.get('package_bindings', {}).items()
                                   if name in row['packages']}
        if 'platform' in row:row['platform'] = row['platforms'][0]
        result.append(row)
    return result


def _plan(source, home, clients):
    if clients is None:
        raise ValueError('Personal detach requires explicit client selection')
    inventory = personal_inventory(source, home, clients)
    blockers = [issue for issue in inventory['issues'] if issue.startswith('invalid personal')]
    owners = inventory['owners']
    recorded = {row['owner']['client'] for row in owners}
    blockers.extend(f'no recorded personal owner: {c}' for c in sorted(set(clients) - recorded))
    pack = load_pack_config(source)
    registry_path = expand_user_path(pack.global_registry, home)
    registry = load_registry(registry_path)
    updated = copy.deepcopy(registry)
    selected = {owner_key(InstallationOwner(**row['owner'])) for row in owners}
    actions = {}
    receipts = {}
    for row in owners:
        for raw_path in row['paths']:
            action = actions.setdefault(raw_path, PlanAction('attach_personal_path', Path(raw_path), {
                'owners': [], 'packages': [], 'mode': row['mode'],
                'global_root': str(expand_user_path(pack.global_root, home))}))
            if action.details['mode'] != row['mode']:blockers.append('Conflicting personal owner modes')
            action.details['owners'].append(row['owner'])
    try:
        for action in actions.values():selection(source, home, action)
        for target_id, target in updated['targets'].items():
            adapters = _without_owners(target.get('adapters', []), selected)
            if adapters == target.get('adapters', []):continue
            root = Path(target_id)
            path = root / '.localsetup/lock.json'
            if not root.is_absolute() or str(root.resolve()) != target_id or path.parent.is_symlink() or path.is_symlink() or not path.is_file():
                raise ValueError(f'Unavailable or unsafe affected personal receipt: {path}')
            if target.get('lock_path') != str(path):
                raise ValueError('Recorded personal receipt path does not match target')
            lock = load_json(path)
            old = lock.get('personal_adapter_targets', [])
            replacement = _without_owners(old, selected)
            if old == replacement:
                raise ValueError(f'Personal ownership differs from affected receipt: {path}')
            lock['personal_adapter_targets'] = replacement
            repo_clients = set()
            for adapter in lock.get('adapter_targets', []):
                if 'owners' in adapter:
                    repo_clients.update(o['client'] for o in adapter['owners'] if o.get('scope') == 'repo')
                else:
                    repo_clients.update(adapter.get('platforms') or ([adapter['platform']] if adapter.get('platform') else []))
            personal_clients = {o['client'] for a in replacement for o in a.get('owners', [])}
            lock['platforms'] = sorted(repo_clients | personal_clients)
            if lock.get('skill_scope') == 'both' and not replacement:lock['skill_scope'] = 'repo'
            receipts[path] = lock
            target['adapters'] = adapters
        for key in selected:updated.get('personal_owners', {}).pop(key, None)
        for package in updated['packages'].values():
            package['refs'] = [ref for ref in package.get('refs', []) if ref not in selected]
    except (ValueError, OSError, TypeError, KeyError) as exc:
        blockers.append(str(exc))
    payload = {'schema_version': 1, 'ok': not blockers, 'applied': False, 'blockers': blockers,
               'owners': sorted(selected), 'paths': sorted(actions), 'receipts': sorted(str(p) for p in receipts),
               'packages_preserved': True}
    return payload, list(actions.values()), registry_path, updated, receipts


def _mutable_preflight(source, home, payload, actions):
    from .mutable_ownership import require_owned_copies
    try:require_owned_copies(source, home, [a.path for a in actions])
    except ValueError as exc:
        return payload | {'ok': False, 'blockers': [*payload.get('blockers', []), str(exc)]}
    return payload


def _execute(source, home, planner, *, apply=False, operation="personal-detach", require_unchanged=False):
    payload, actions, registry_path, updated, receipts = planner()
    payload = _mutable_preflight(source, home, payload, actions)
    if not apply or not payload['ok'] or (not actions and not receipts):return payload
    initial = payload
    state = global_layout(home).localsetup_home
    with package_root_lock(state):
        payload, actions, registry_path, updated, receipts = planner()
        payload = _mutable_preflight(source, home, payload, actions)
        if not payload['ok'] or (not actions and not receipts):return payload
        if require_unchanged and payload != initial:
            return payload | {'ok': False, 'blockers': ['stale_scope_retirement: recorded ownership changed']}
        path = state / 'state' / operation / (uuid.uuid4().hex + '.json')
        journal = {'version': 1, 'operation': operation, 'status': 'started', 'touched': []}
        try:
            for receipt in [registry_path, *receipts]:record_file_state(journal, path, receipt, os.replace)
            for action in actions:write(source, home, action, journal, path)
            from .mutable_ownership import retire_empty_baselines
            retire_empty_baselines([action.path for action in actions], updated)
            for receipt, lock in receipts.items():save_json(receipt, lock)
            save_json(registry_path, updated)
            journal['status'] = 'committed';write_journal(path, journal)
        except Exception as exc:
            journal['status'] = 'failed'
            try:restore_failed_mutations(journal, os.replace)
            except Exception as recovery:journal['rollback_error'] = str(recovery)
            write_journal(path, journal)
            return payload | {'ok': False, 'blockers': [str(exc)], 'journal': str(path),
                              'recovery_ok': 'rollback_error' not in journal}
        warnings = []
        try:cleanup_backups(journal)
        except OSError as exc:warnings.append(f'personal detach committed; backup cleanup failed: {exc}')
        return payload | {'applied': True, 'journal': str(path), 'warnings': warnings}


def detach_personal(source: Path, home: Path, clients: list[str], *, apply: bool = False) -> dict:
    return _execute(source, home, lambda: _plan(source, home, clients), apply=apply)
