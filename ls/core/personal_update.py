"""Plan package refreshes from recorded personal ownership, without reselection."""
import hashlib
import json
from pathlib import Path

from .aliases import collect_package_aliases
from .installation_ownership import InstallationOwner
from .manifests import load_pack_config
from .models import PlanAction
from .paths import expand_user_path
from .personal_inventory import personal_inventory
from .personal_registry import owner_key
from .plan import build_install_plan
from .skills import load_skill_catalog
from .workflows import load_workflow_catalog


_RETIRED_WORKFLOWS = {
    'ls-workflow-audit-framework': 'ls-localsetup-framework-audit',
    'ls-workflow-audit-markdown-references': 'ls-markdown-reference-validator',
    'ls-workflow-codex-heartbeat': 'ls-codex-heartbeat-checks',
    'ls-workflow-context-index-query': 'ls-context-index-search',
    'ls-workflow-context-index-refresh': 'ls-context-index-search',
    'ls-workflow-documentation-alignment': 'ls-docs-source-drift',
    'ls-workflow-skills-index-refresh': 'ls-find-public-skills',
    'ls-workflow-transport-handoff': 'ls-agentq-ship-ingest',
}


def _unavailable_packages_message(missing: set[str], available: set[str]) -> str:
    details = []
    for name in sorted(missing):
        replacement = _RETIRED_WORKFLOWS.get(name)
        if replacement in available:
            status = f'retired workflow; supported owning skill: {replacement}'
        elif replacement:
            status = f'retired workflow; owning skill {replacement} is also unavailable in this source'
        else:
            status = 'unavailable; no supported replacement is recorded'
        details.append(f'{name} ({status})')
    return ('Recorded packages are unavailable in the update source: ' + '; '.join(details)
            + '. No selections were changed. Review the owning skills and the recorded-update '
              'guidance in ls/docs/ADAPTER_OWNERSHIP.md before selecting replacements.')


def _workflow_package_aliases(source: Path) -> dict[str, str]:
    canonical = {workflow.package for workflow in load_workflow_catalog(source)}
    all_aliases = collect_package_aliases(source)
    return {alias: target for alias, target in all_aliases.items() if target in canonical}


def _canonical_packages(names, aliases: dict[str, str]) -> list[str]:
    return sorted({aliases.get(name, name) for name in names})


def _canonical_selectors(value, skill_aliases: dict[str, str], workflow_aliases: dict[str, str]):
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key, aliases in (('skills', skill_aliases), ('workflows', workflow_aliases)):
        if isinstance(value.get(key), list):
            result[key] = _canonical_packages(value[key], aliases)
    return result


def _build_recorded_plan(source: Path, home: Path, target: Path, scope: str):
    lock_path = target / '.localsetup/lock.json'
    if not lock_path.exists():
        lock_path = target / 'localsetup.lock.json'
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    if not isinstance(lock, dict) or lock.get('skill_scope', 'repo') != scope:
        raise ValueError(f'A recorded {scope} installation is required')
    clients = lock.get('platforms')
    if not isinstance(clients, list) or any(not isinstance(c, str) or not c for c in clients):
        raise ValueError('Invalid recorded personal client selection')
    registry_path = expand_user_path(load_pack_config(source).global_registry, home)
    snapshots = {str(lock_path): hashlib.sha256(lock_bytes).hexdigest(),
                 str(registry_path): hashlib.sha256(registry_path.read_bytes()).hexdigest()}
    personal_clients = clients if scope == 'personal' else sorted({o['client'] for row in lock.get('personal_adapter_targets', []) for o in row.get('owners', [])})
    if scope == 'both':
        from .combined_repair import _plan as combined_plan
        repair, _ = combined_plan(source, home, target, None, allow_source_drift=True)
        if not repair['ok'] or repair['actions']:
            raise ValueError('Repair combined adapters before updating')
    if scope == 'repo':
        from .verify import verify_install
        report = verify_install(source, home, target_root=target)
        if not report['ok']:
            from .adapter_alias_reconciliation import plan_alias_retirements

            global_root = expand_user_path(load_pack_config(source).global_root, home)
            repo_actions = [
                PlanAction('attach_repo_path', Path(row['path']), {
                    **row,
                    'path': row['path'],
                    'packages': row.get('packages', lock.get('repo_packages', [])),
                    'global_root': str(global_root),
                })
                for row in lock.get('adapter_targets', [])
                if isinstance(row, dict) and isinstance(row.get('path'), str)
            ]
            retirements, conflicts = plan_alias_retirements(source, home, target, repo_actions)
            if conflicts:
                rows = '; '.join(f"{row['path']}: {row['reason']}" for row in conflicts)
                raise ValueError('Repair repository adapters before migrating: ' + rows)
            aliases_by_path: dict[str, set[str]] = {}
            for row in retirements:
                aliases_by_path.setdefault(str(Path(row['path']).parent.resolve(strict=False)), set()).add(row['alias'])
            allowed_issues = {
                f"adapter visible packages do not match selection: {path}"
                for path, aliases in aliases_by_path.items() if aliases
            }
            if any(issue not in allowed_issues and not (
                issue.startswith('external adapter skill invalid:')
                and issue.endswith('External skill source changed; rebuild the selection and plan')
            ) and not (
                issue.startswith(('external skill invalid:', 'external adapter skill invalid:'))
                and issue.endswith('External integration contract requires alignment')
            ) for issue in report.get('issues', [])):
                raise ValueError('Repair repository adapters before migrating')
    from .adapter_alias_reconciliation import plan_alias_retirements

    global_root = expand_user_path(load_pack_config(source).global_root, home)
    reconciliation_actions = [
        PlanAction('attach_personal_path', Path(row['path']), {
            **row,
            'global_root': row.get('global_root') or str(global_root),
        })
        for row in lock.get('personal_adapter_targets', [])
        if isinstance(row, dict) and isinstance(row.get('path'), str)
    ]
    proven_retirements, alias_conflicts = plan_alias_retirements(
        source, home, target, reconciliation_actions
    )
    if alias_conflicts:
        rows = '; '.join(f"{row['path']}: {row['reason']}" for row in alias_conflicts)
        raise ValueError('Repair personal adapters before updating: ' + rows)
    allowed_aliases: dict[str, set[str]] = {}
    for row in proven_retirements:
        adapter_path = str(Path(row['path']).parent.resolve(strict=False))
        allowed_aliases.setdefault(adapter_path, set()).add(row['alias'])
    inventory = personal_inventory(
        source, home, personal_clients,
        expected=lock.get('personal_adapter_targets', []),
        allow_legacy_alias_retirements=allowed_aliases,
        allow_source_drift=True,
    )
    if not inventory['ok']:
        raise ValueError('Repair personal adapters before updating: ' + '; '.join(inventory['issues']))
    owners = inventory['owners']
    if {row['owner']['client'] for row in owners} != set(personal_clients):
        raise ValueError('Recorded personal clients are missing ownership records')
    package_aliases = collect_package_aliases(source)
    from .package_bindings import merge_bindings
    from .skills_external import binding_artifact_path, inspect_external_skill
    from .provenance_source import framework_version
    recorded_bindings = merge_bindings(lock.get('package_bindings', {}),
                                      *(row.get('package_bindings', {}) for row in owners))
    refreshed_bindings = {}
    for name, binding in recorded_bindings.items():
        current = inspect_external_skill(Path(binding['source_path']), binding['scope'])
        if current.origin_id != binding['origin_id'] or current.compatibility == 'alignment-required':
            raise ValueError(f'Custom skill origin or contract requires alignment: {name}')
        refreshed = current.receipt()
        refreshed_bindings[name] = {**refreshed,
            'integrated_framework_version': framework_version(source),
            'artifact_path': str(binding_artifact_path(global_root, refreshed)),
            'disposition': 'managed_projection'}
    package_aliases = {alias: name for alias, name in package_aliases.items() if alias not in recorded_bindings}
    workflow_names = {workflow.package for workflow in load_workflow_catalog(source)}
    workflow_aliases = {alias: target for alias, target in package_aliases.items()
                        if target in workflow_names}
    skill_aliases = {alias: target for alias, target in package_aliases.items()
                     if target not in workflow_names}
    package_aliases = {**skill_aliases, **workflow_aliases}
    requested = {name for row in owners for name in _canonical_packages(row['packages'], package_aliases)}
    baseline = lock.get('global_baseline_packages', [])
    if not isinstance(baseline, list) or any(not isinstance(n, str) for n in baseline):
        raise ValueError('Invalid recorded global package baseline')
    repository = lock.get('adapter_targets', []) if scope != 'personal' else []
    repo_requested = {name for row in repository for name in _canonical_packages(
        row.get('packages', lock.get('repo_packages', [])), package_aliases)} if scope != 'personal' else requested
    packages = requested | repo_requested | set(_canonical_packages(baseline, package_aliases))
    skills = {skill.name for skill in load_skill_catalog(source)}
    workflows = {workflow.package for workflow in load_workflow_catalog(source)}
    if packages - skills - workflows - refreshed_bindings.keys():
        raise ValueError(_unavailable_packages_message(packages - skills - workflows - refreshed_bindings.keys(), skills | workflows))
    selected_skills = sorted((packages & skills) | (packages & refreshed_bindings.keys()))
    selected_workflows = sorted(packages & workflows)
    plan = build_install_plan(source, home, global_preset='custom', global_skills=[n for n in selected_skills if n not in refreshed_bindings],
                              global_workflows=selected_workflows, platform_ids=[], target_root=target,
                              skill_scope=scope)
    for action in plan.actions:
        if action.kind == 'install_skills':
            action.details['skills'] = selected_skills
            action.details['package_bindings'] = refreshed_bindings
        if action.kind == 'install_workflows':action.details['workflows'] = selected_workflows
    metadata = plan.rollback_metadata
    metadata['package_bindings'] = refreshed_bindings
    for key, value in lock.items():
        if key == 'global_baseline_skills':
            metadata[key] = _canonical_packages(value, skill_aliases)
        elif key == 'global_baseline_workflows':
            metadata[key] = _canonical_packages(value, workflow_aliases)
        elif key == 'global_baseline_packages':
            metadata[key] = _canonical_packages(value, package_aliases)
        elif key == 'global_baseline_selectors':
            metadata[key] = _canonical_selectors(value, skill_aliases, workflow_aliases)
        elif key.startswith('global_baseline_'):
            metadata[key] = value
    metadata["aliases"] = {k: v for k, v in metadata["aliases"].items() if v in selected_skills}
    metadata.update(attach_mode=lock.get("attach_mode", "symlink"), platforms=clients, global_only=not clients, skills=selected_skills,
                    workflows=selected_workflows, packages=sorted(packages),
                    repo_packages=sorted(repo_requested), repo_skills=sorted(repo_requested & set(selected_skills)),
                    repo_workflows=sorted(repo_requested & workflows), adapter_packages=sorted(repo_requested),
                    recorded_state_hashes=snapshots)
    metadata['recorded_adapter_transitions'] = lock.get('adapter_transitions', [])
    if scope != 'personal':
        from .detach_records import recorded_detach_rows
        recorded_detach_rows(lock, target)
        metadata['repo_links'] = []
        for key in ('repo_selectors', 'repo_packs'):
            if key in lock:
                metadata[key] = (_canonical_selectors(lock[key], skill_aliases, workflow_aliases)
                                 if key == 'repo_selectors' else lock[key])
        for row in repository:
            path = Path(row['path'])
            if not path.is_absolute():path = target / path
            client_ids = sorted({o['client'] for o in row['owners']}) if 'owners' in row else list(row.get('platforms') or ([row['platform']] if row.get('platform') else []))
            details = dict(row, platforms=client_ids, packages=_canonical_packages(
                               row.get('packages', lock.get('repo_packages', [])), package_aliases),
                           global_root=str(expand_user_path(load_pack_config(source).global_root, home)),
                           mode=row.get('mode', lock.get('attach_mode', 'symlink')))
            details['package_bindings'] = {
                name: {**refreshed_bindings[name], 'disposition': binding.get('disposition', 'managed_projection')}
                for name, binding in row.get('package_bindings', {}).items()
            }
            plan.actions.append(PlanAction('attach_repo_path', path, details))
            metadata['repo_links'].append(str(path))
    actions = {}
    for row in owners:
        for path in row['paths']:
            action = actions.setdefault(path, PlanAction('attach_personal_path', Path(path), {
                'platforms': [], 'owners': [], 'owner_packages': {}, 'packages': [],
                'mode': row['mode'], 'global_root': str(expand_user_path(load_pack_config(source).global_root, home))}))
            if action.details['mode'] != row['mode']:
                raise ValueError('Recorded owners disagree on the shared adapter mode')
            action.details['platforms'].append(row['owner']['client'])
            action.details['owners'].append(row['owner'])
            selected = _canonical_packages(row['packages'], package_aliases)
            action.details['owner_packages'][owner_key(InstallationOwner(**row['owner']))] = selected
            action.details['packages'] = sorted(set(action.details['packages']) | set(selected))
            action.details['package_bindings'] = merge_bindings(action.details.get('package_bindings', {}), {
                name: {**refreshed_bindings[name], 'disposition': binding.get('disposition', 'managed_projection')}
                for name, binding in row.get('package_bindings', {}).items()
            })
    plan.actions.extend(actions[path] for path in sorted(actions))
    agents = lock.get('codex_agents', [])
    if agents:
        if agents != ['guardian_subagent']:raise ValueError('Unsupported recorded Codex agent selection')
        plan.actions.append(PlanAction('install_codex_agents', home / '.codex/agents', {'agents': agents}))
        metadata['codex_agents'] = agents
    return plan


def build_recorded_personal_plan(source: Path, home: Path, target: Path):
    return _build_recorded_plan(source, home, target, 'personal')


def build_recorded_both_plan(source: Path, home: Path, target: Path):
    return _build_recorded_plan(source, home, target, 'both')
