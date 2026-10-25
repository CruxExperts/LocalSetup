"""Preserve recorded exposure while refreshing the shared package library."""
import stat
import hashlib
import json
from .detach_records import recorded_detach_rows

from .personal_update import _build_recorded_plan
from .plan import build_install_plan


def recorded_refresh(source, home, target, config, packs, platforms, explicit_mode):
    receipt = target / '.localsetup/lock.json'
    if not receipt.exists() and not receipt.is_symlink():
        receipt = target / 'localsetup.lock.json'
    if not receipt.exists() and not receipt.is_symlink():
        from .manifests import load_platforms
        from .client_registry.historical import historical_adapter_paths
        paths = {path for client in load_platforms(source) for path in client.repo_paths}
        paths.update(path for values in historical_adapter_paths().values() for path in values)
        ambiguous = any((target / path).exists() or (target / path).is_symlink() for path in paths)
        if platforms is None and ambiguous:
            raise ValueError('self-refresh requires recorded ownership or explicit --platforms; a shared adapter path does not identify clients')
        return None
    if receipt.is_symlink() or not stat.S_ISREG(receipt.stat().st_mode):
        raise ValueError('self-refresh requires a regular recorded receipt')
    if not receipt.parent.resolve().is_relative_to(target.resolve()):
        raise ValueError('self-refresh receipt escapes the target')
    receipt_bytes = receipt.read_bytes()
    lock = json.loads(receipt_bytes)
    if not isinstance(lock, dict):
        raise ValueError('self-refresh requires a valid recorded installation')
    scope = lock.get('skill_scope', 'repo')
    if scope not in {'repo', 'personal', 'both'}:
        raise ValueError('self-refresh requires a valid recorded scope')
    version = lock.get('version', 1)
    if type(version) is not int or version not in {1, 2}:
        raise ValueError('Invalid recorded receipt version')
    clients = lock.get('platforms', lock.get('tools', []))
    if not isinstance(clients, list) or any(not isinstance(c, str) or not c for c in clients):
        raise ValueError('Invalid recorded client selection')
    recorded_detach_rows(lock, target)
    if platforms is not None and version == 1:
        if scope != 'repo':
            raise ValueError('Legacy non-repository ownership requires reconciliation')
        # Explicit legacy transitions retain the existing preservation preflight.
        return None
    if config.skill_scope is not None and config.skill_scope != scope:
        raise ValueError('self-refresh preserves recorded scope; use explicit scope migration')
    if 'adapter_targets' not in lock:
        raise ValueError('self-refresh requires validated recorded adapter paths; reconcile legacy ownership before refresh')
    if platforms is not None and set(platforms) != set(lock.get('platforms', [])):
        raise ValueError('self-refresh cannot change recorded clients; use an explicit install or scope migration after reviewing ownership')
    exposure_fields = ('skills', 'workflows', 'preset', 'skill_classes', 'skill_tags', 'exclude_skills',
                       'repo_packs', 'repo_preset', 'repo_skills', 'repo_workflows',
                       'repo_skill_classes', 'repo_skill_tags', 'repo_exclude_skills')
    if any(getattr(config, key, None) is not None for key in exposure_fields):
        raise ValueError('self-refresh preserves recorded exposure; use explicit install to change target package selectors')
    plan = _build_recorded_plan(source, home, target, scope)
    expected = hashlib.sha256(receipt_bytes).hexdigest()
    if plan.rollback_metadata['recorded_state_hashes'].get(str(receipt)) != expected:
        raise ValueError('Recorded receipt changed during self-refresh selection; retry after reconciliation')
    modes = {a.details['mode'] for a in plan.actions if a.kind in {'attach_repo_path', 'attach_personal_path'}}
    if explicit_mode and any(mode != config.attach_mode for mode in
                             (modes or {plan.rollback_metadata['attach_mode']})):
        raise ValueError('self-refresh preserves recorded adapter modes; use the explicit mode migration command')
    library = build_install_plan(source, home, packs=packs, platform_ids=[], target_root=target,
        global_packs=config.global_packs, global_preset=config.global_preset,
        global_skills=config.global_skills, global_workflows=config.global_workflows,
        global_skill_classes=config.global_skill_classes, global_skill_tags=config.global_skill_tags,
        global_exclude_skills=config.global_exclude_skills, skill_scope=scope)
    metadata = plan.rollback_metadata
    from .package_bindings import merge_binding_selections
    metadata['package_bindings'] = merge_binding_selections(
        (metadata['skills'], metadata.get('package_bindings', {})),
        (library.rollback_metadata['skills'], library.rollback_metadata.get('package_bindings', {})))
    for kind, key in [('install_skills', 'skills'), ('install_workflows', 'workflows')]:
        names = sorted(set(metadata[key]) | set(library.rollback_metadata[key]))
        metadata[key] = names
        action = next((a for a in plan.actions if a.kind == kind), None)
        if action is None and names:
            # Reuse only the canonical package installation action, never adapters.
            action = next(a for a in library.actions if a.kind == kind)
            position = max(i for i, a in enumerate(plan.actions)
                           if a.kind in {'install_skills', 'install_workflows'}) + 1
            plan.actions.insert(position, action)
        if action is not None:
            action.details[key] = names
            if kind == 'install_skills':
                action.details['package_bindings'] = metadata['package_bindings']
    metadata['packages'] = sorted(set(metadata['skills']) | set(metadata['workflows']))
    metadata['aliases'].update(library.rollback_metadata['aliases'])
    for key, value in library.rollback_metadata.items():
        if key.startswith('global_baseline_') or key in {'packs', 'preset', 'selectors'}:
            metadata[key] = value
    return plan
