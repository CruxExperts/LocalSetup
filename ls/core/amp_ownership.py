"""Shared recorded ownership evidence for client prerequisites and portable copies."""
from pathlib import Path

from .adapter_markers import ADAPTER_MARKER_JSON, is_safe_adapter_package_name
from .installation_ownership import InstallationOwner
from .lockfile import load_json
from .manifests import load_pack_config
from .models import PlanAction
from .paths import expand_user_path
from .personal_inventory import _links
from .provenance import is_managed_package, package_digest
from .registry import load_registry


def _mapping(value):
    if not isinstance(value, dict):raise ValueError('ownership container must be an object')
    return value


def _list(value):
    if not isinstance(value, list):raise ValueError('ownership entries must be an array')
    return value


def _packages(value):
    names = _list(value)
    if any(not isinstance(n, str) or not is_safe_adapter_package_name(n) for n in names):
        raise ValueError('invalid owned package names')
    return set(names)


def records(source, home):
    pack = load_pack_config(source)
    registry = load_registry(expand_user_path(pack.global_registry, home))
    result = []
    for row in _mapping(registry.get('personal_owners', {})).values():
        row = _mapping(row)
        owner = InstallationOwner(**_mapping(row['owner']))
        if owner.scope != 'personal' or owner.root != str(home.resolve()):raise ValueError('invalid personal ownership')
        for path in _list(row['paths']):
            if not isinstance(path, str) or not Path(path).is_absolute():raise ValueError('invalid owned path')
            Path(path).relative_to(home)
            result.append((Path(path), _packages(row['packages']), row.get('mode', 'symlink'), {owner.client}, home, row.get('package_bindings', {})))
    for target, record in _mapping(registry.get('targets', {})).items():
        if not isinstance(target, str) or not Path(target).is_absolute():raise ValueError('invalid target root')
        for row in _list(_mapping(record).get('adapters', [])):
            row = _mapping(row)
            owners = [InstallationOwner(**_mapping(o)) for o in _list(row.get('owners', []))]
            clients = {o.client for o in owners if o.scope == 'repo' and o.root == target}
            if not clients:continue
            path = Path(row['path'])
            if not path.is_absolute():path = Path(target) / path
            result.append((path, _packages(row.get('packages', [])), row.get('mode', 'symlink'), clients, Path(target), row.get('package_bindings', {})))
    return result, expand_user_path(pack.global_root, home)


def affected_actions(actions, recorded, library, *, client_id='amp-cli'):
    extra = [];targets = []
    changed = {a.details.get('package_bindings', {}).get(name, {}).get('package_id', name)
               for a in actions if a.kind in {'install_skills', 'install_workflows'} and a.path == library
               for name in a.details.get('skills', a.details.get('workflows', []))}
    for path, packages, mode, clients, target, bindings in recorded:
        if client_id not in clients:continue
        physical = [a for a in actions if a.path == path and a.kind in {'attach_repo_path', 'attach_personal_path', 'repair_repo_path'}]
        names = {n for n in packages if bindings.get(n, {}).get('package_id', n) in changed} | {n for a in physical for n in a.details.get('packages', [])}
        if not names:continue
        extra.append(PlanAction('repair_repo_path', path, {'platforms': [client_id], 'packages': sorted(names),
                     'global_root': str(library), 'mode': mode, '_amp_recorded_only': True,
                     'package_bindings': {n: b for n, b in bindings.items() if n in names}}))
        targets.append(target)
    return extra, targets


def portable_counterpart(path, library_package, expected, recorded):
    if path.is_symlink() or not any(root == path.parent and path.name in packages and mode == 'portable'
                                   for root, packages, mode, clients, target, bindings in recorded):return False
    marker = path.parent / ADAPTER_MARKER_JSON
    if marker.is_symlink() or not marker.is_file():return False
    data = _mapping(load_json(marker))
    if (data.get('managed_by') != 'localsetup' or data.get('mode') != 'portable'
            or path.name not in _packages(data.get('packages', []))
            ):return False
    from .package_bindings import package_source
    if package_source(Path(data.get('global_root', '')), path.name, adapter=path.parent).resolve() != library_package.resolve():
        return False
    return (is_managed_package(path) and package_digest(path) == package_digest(expected)
            and _links(path) == _links(expected))


def selected_package_paths(source, actions, action, name):
    """Use the same selected origin for native client precedence checks."""
    from .package_bindings import package_source
    from .skills_external import validate_source_binding
    library = Path(action.details['global_root'])
    bindings = action.details.get('package_bindings', {})
    installed = package_source(library, name, bindings=bindings)
    installing = next((a for a in actions if a.kind in {'install_skills', 'install_workflows'}
                       and a.path == library and name in a.details.get('skills', a.details.get('workflows', []))
                       and a.details.get('package_bindings', {}).get(name, {}).get('package_id')
                       == bindings.get(name, {}).get('package_id')), None)
    if installing and name in bindings:
        original = validate_source_binding(bindings[name]).path
    elif installing:
        original = source / 'ls' / ('workflows' if installing.kind == 'install_workflows' else 'skills') / name
    elif (installed / 'SKILL.md').is_file():
        original = installed
    else:
        original = source / 'ls/skills' / name
    if not (original / 'SKILL.md').is_file():
        raise ValueError('Selected skill metadata is unavailable')
    return installed, original
