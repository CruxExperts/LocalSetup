"""Add an ownership scope using recorded requests and the existing transaction."""
from pathlib import Path

from .adapters import adapter_targets, personal_adapter_targets
from .apply_preflight import preflight_install_plan
from .installation_ownership import InstallationOwner, resolve_skill_scope
from .models import PlanAction
from .manifests import load_pack_config
from .paths import expand_user_path
from .personal_registry import owner_key
from .personal_update import _build_recorded_plan
from .registry import load_registry


def build_additive_scope_plan(source: Path, home: Path, target: Path):
    """Plan repo/personal -> both, without retiring or reselecting existing owners."""
    scope = resolve_skill_scope(target, None)
    if scope not in {'repo', 'personal'}:
        raise ValueError('Additive migration requires a repository or personal installation')
    plan = _build_recorded_plan(source, home, target, scope)
    requests = {}
    bindings_by_client = {}
    from .package_bindings import merge_binding_selections
    old_kind = 'attach_repo_path' if scope == 'repo' else 'attach_personal_path'
    for action in plan.actions:
        if action.kind != old_kind:continue
        details = action.details
        for client in details['platforms']:
            key = owner_key(InstallationOwner('personal', str(home.resolve()), client))
            packages = details.get('owner_packages', {}).get(key, details['packages'])
            request = (tuple(sorted(packages)), details['mode'])
            if client in requests and requests[client] != request:
                raise ValueError('Recorded client has differing path selections or modes')
            requests[client] = request
            selected_bindings = {n: b for n, b in details.get('package_bindings', {}).items() if n in packages}
            bindings_by_client[client] = merge_binding_selections(
                (packages, bindings_by_client.get(client, selected_bindings)), (packages, selected_bindings))
    if not requests:
        raise ValueError('No recorded owners are available to migrate')
    metadata = plan.rollback_metadata
    global_root = next(a.details['global_root'] for a in plan.actions if a.kind == old_kind)
    if scope == 'repo':
        registry = load_registry(expand_user_path(load_pack_config(source).global_registry, home))
        existing = registry.get('personal_owners', {})
        for client in requests:
            key = owner_key(InstallationOwner('personal', str(home.resolve()), client))
            if key in existing:
                raise ValueError('Personal owner already exists; reconcile its selection before migration')
        for row in personal_adapter_targets(source, home, sorted(requests)):
            selections = {owner_key(InstallationOwner(**owner)): list(requests[owner['client']][0])
                          for owner in row['owners']}
            modes = {requests[client][1] for client in row['platforms']}
            if len(modes) != 1:raise ValueError('New shared personal path has conflicting modes')
            plan.actions.append(PlanAction('attach_personal_path', row['path'], {
                'platforms': row['platforms'], 'owners': row['owners'], 'owner_packages': selections,
                'packages': sorted({n for names in selections.values() for n in names}),
                'mode': modes.pop(), 'global_root': global_root}))
    else:
        for row in adapter_targets(source, home, sorted(requests), target_root=target):
            selections = {requests[client] for client in row['platforms']}
            if len(selections) != 1:
                raise ValueError('New shared repository path requires matching owner selections and modes')
            packages, mode = selections.pop()
            plan.actions.append(PlanAction('attach_repo_path', row['repo_path'], {
                'platform': row['platform'], 'platforms': row['platforms'], 'packages': list(packages),
                'mode': mode, 'global_root': global_root, 'verify_rules': row['verify_rules']}))
        metadata['repo_links'] = [str(a.path) for a in plan.actions if a.kind == 'attach_repo_path']
    new_kind = 'attach_personal_path' if scope == 'repo' else 'attach_repo_path'
    for action in plan.actions:
        if action.kind != new_kind:
            continue
        bindings = merge_binding_selections(*(
            (requests[client][0], bindings_by_client[client]) for client in action.details['platforms']))
        action.details['package_bindings'] = {
            name: {**binding, 'disposition': 'external_present'
                   if Path(binding['source_path']).absolute() == (action.path / name).absolute()
                   else 'managed_projection'}
            for name, binding in bindings.items()}
    added_clients = {c for a in plan.actions if a.kind == new_kind for c in a.details['platforms']}
    if set(requests) - added_clients:
        raise ValueError('Selected clients have no supported paths in the added scope')
    metadata['skill_scope'] = 'both'
    metadata['scope_migration'] = {'from': scope, 'to': 'both'}
    report = preflight_install_plan(source, plan, home, target_root=target)
    if not report['ok']:
        raise ValueError('Scope migration is unsafe: ' + '; '.join(b['reason'] for b in report['blockers']))
    return plan
