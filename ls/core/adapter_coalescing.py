"""Pair physical adapter writes without merging logical ownership receipts."""
from .adapter_markers import is_safe_adapter_package_name


def paired_repository_actions(plan) -> dict:
    groups = {}
    for action in plan.actions:
        if action.kind not in {'attach_repo_path', 'attach_personal_path'}:continue
        group = groups.setdefault(action.path, {})
        if action.kind in group:raise ValueError('Duplicate adapter action for the same path and scope')
        group[action.kind] = action
    pairs = {}
    for path, group in groups.items():
        if len(group) != 2:continue
        repository = group['attach_repo_path'];personal = group['attach_personal_path']
        if (repository.details.get('mode', 'symlink') != personal.details.get('mode', 'symlink')
                or repository.details.get('global_root') != personal.details.get('global_root')):
            raise ValueError('Paired adapter actions must use the same mode and package library')
        packages = repository.details.get('packages', [])
        if not isinstance(packages, list) or any(not isinstance(n, str) or not is_safe_adapter_package_name(n) for n in packages):
            raise ValueError('Invalid paired repository package selection')
        from .package_bindings import merge_binding_selections
        personal.details['package_bindings'] = merge_binding_selections(
            (packages, repository.details.get('package_bindings', {})),
            (personal.details.get('packages', []), personal.details.get('package_bindings', {})))
        pairs[path] = repository
    return pairs
