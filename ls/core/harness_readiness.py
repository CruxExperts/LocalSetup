"""Read-only selected-agent checks; execution requires an explicit probe."""
from copy import deepcopy
import os
from pathlib import Path
import time

from . import harness
from .harness_configuration import read
from .agent import profiles, runtime_diagnostics
from .agent.completion_context import validate_paths
from .agent.file_grants import FileGrant


def _path(profile, name):
    value = profile.get(name)
    if (not isinstance(value, str) or not value or '\0' in value
            or not Path(value).is_absolute() or '..' in Path(value).parts
            or str(Path(value)) != value):
        raise ValueError('Expected a canonical absolute path')
    return Path(value)


def _selection(config):
    agent, heartbeat = config.get('agent'), config.get('heartbeat', {})
    if (not isinstance(agent, dict) or not isinstance(heartbeat, dict)
            or type(agent.get('enabled', False)) is not bool
            or any(type(heartbeat[key]) is not bool for key in ('enabled', 'scheduled_agent') if key in heartbeat)):
        raise ValueError('Invalid heartbeat activation configuration')
    name, inventory = agent.get('profile'), config.get('agent_profiles')
    if not isinstance(name, str) or not name or not isinstance(inventory, dict):
        raise ValueError('Select an explicit agent profile')
    profile = inventory.get(name)
    if not isinstance(profile, dict) or profile.get('launcher') not in ('lscli', 'lscli-review'):
        raise ValueError('Readiness requires a typed LSCli profile')
    return name, profile, agent, heartbeat


def _grant(profile, target):
    from .agent.run_cli import _grant as load_grant
    value, _ = load_grant(_path(profile, 'grant'), target)
    review = profile['launcher'] == 'lscli-review'
    grant = FileGrant('readiness', 'readiness', target, tuple(value['read']),
                      () if review else tuple(value['write']), tuple(value['disclose']), time.monotonic() + 30)
    if review:
        validate_paths(profile.get('context'))
        for path in profile['context']:
            grant.check('readiness', 'readiness', 'read', path, provider=True)


def _coding_paths(profile, target):
    from .agent.coding_run import RunPaths
    from .agent.profile_setup import _parent
    from .agent.resource_group import _parent as delegation
    from .agent.session_owner import _separate
    state = _path(profile, 'state_root')
    _separate(state, target)
    _separate(state, _path(profile, 'runtime_root'))
    RunPaths(_path(profile, 'runtime_root'), state / 'sessions', state / 'leases',
             state / 'snapshots', state / 'scratch', _path(profile, 'resource_parent')).validate(target)
    fd = _parent(state / '.readiness', create=False)
    if fd is not None:
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise ValueError('Coding state must be owner-private')
        finally:
            os.close(fd)
    fd = delegation(_path(profile, 'resource_parent'))
    os.close(fd)


def inspect(repo_root, target_root=None, *, probe=False):
    if type(probe) is not bool:
        raise ValueError('Probe selection must be boolean')
    target = harness._target(target_root, repo_root)
    checks = []
    result = {'ok': False, 'scope': 'selected-agent', 'status': 'blocked', 'checks': checks,
              'probe': {'status': 'not_started' if probe else 'not_run'}, 'schedule_changed': False,
              'unverified': ['collection_hooks', 'live_cron', 'future_runs']}

    def check(name, callback, next_step):
        try:
            value = callback()
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
            checks.append({'name': name, 'status': 'blocked', 'next_step': next_step})
            return None
        checks.append({'name': name, 'status': 'verified'})
        return value

    config = check('configuration', lambda: read(target / harness.HEARTBEAT_CONFIG)[1],
                   'Run harness codex-heartbeat init if missing; inspect configuration ownership, permissions and YAML.')
    if config is None:
        return result
    selection = check('selection', lambda: _selection(config),
                      'Use configure-lscli with an explicit review or coding mode and an existing provider profile.')
    if selection is None:
        return result
    name, profile, agent, heartbeat = selection
    review = profile['launcher'] == 'lscli-review'
    result.update(mode='review' if review else 'coding', profile=name,
                  activation={'agent_enabled': agent.get('enabled', False),
                              'heartbeat_enabled': heartbeat.get('enabled', False),
                              'scheduled_agent': heartbeat.get('scheduled_agent', False)})

    def installed():
        value = runtime_diagnostics.runtime(_path(profile, 'runtime_root'))
        if value['status'] != 'verified' or value.get('dependencies', {}).get('status') != 'verified':
            raise ValueError('Installed runtime or dependency metadata unavailable')
        return value

    runtime = check('runtime', installed,
                    'Inspect lscli doctor; finish busy upgrades or install verified runtime artifacts and their locked dependencies.')

    def provider():
        value = profiles.load(_path(profile, 'profiles'), profile.get('profile'))
        if not review and not {'tools', 'streaming'} <= value.capabilities:
            raise ValueError('Coding requires tool and streaming capabilities')
        return value

    provider_profile = check('provider_profile', provider,
                             'Select an existing private provider profile with the capabilities required by this mode.')
    if provider_profile is not None:
        source = provider_profile.credential_source
        result['credential_source'] = source.provider if source else 'env'
    check('grant', lambda: _grant(profile, target),
          'Supply an owned private grant outside the workspace; review context needs both read and disclose scopes.')
    if not review:
        if runtime is not None and runtime.get('native_sandbox', {}).get('status') == 'present_unprobed':
            checks.append({'name': 'native_bundle', 'status': 'present_unprobed'})
        else:
            checks.append({'name': 'native_bundle', 'status': 'blocked', 'next_step':
                           'Install a qualified native bundle on a supported platform, or explicitly select tool-free review mode.'})
        check('coding_paths', lambda: _coding_paths(profile, target),
              'Select private separate state paths and an owned cgroup delegation with cpu, memory and pids enabled.')

    execution = harness._load_runtime(repo_root)
    selected = {'agent': {**agent, 'enabled': True}, 'agent_profiles': {name: deepcopy(profile)}}
    # The runtime uses its own error type. Return only a controlled diagnosis,
    # never exception text containing configuration, context or provider data.
    try:
        execution.planned_commands(selected, target_root=target)
        checks.append({'name': 'launcher', 'status': 'verified'})
    except (execution.HeartbeatError, OSError, ValueError, TypeError, RuntimeError, RecursionError):
        checks.append({'name': 'launcher', 'status': 'blocked', 'next_step':
                       'Inspect typed profile fields and re-register lscli for the selected installed runtime if stale.'})
    if any(item['status'] == 'blocked' for item in checks):
        if probe:
            result['probe'] = {'status': 'not_started', 'next_step': 'Resolve the blocked static checks before probing.'}
        return result
    result.update(ok=True, status='static_verified')
    result['unverified'] += ['credentials', 'provider_execution', 'context_contents'] if review else [
        'credentials', 'provider_execution', 'native_execution', 'tool_actions']
    if probe:
        from .harness_probe import run
        outcome = run(execution, selected, target)
        result['probe'] = outcome
        result['ok'] = outcome['status'] == 'passed'
        result['status'] = 'probe_passed' if result['ok'] else 'probe_failed'
        if result['ok']:
            verified = {'credentials', 'provider_execution', 'context_contents', 'native_execution'}
            result['unverified'] = [item for item in result['unverified'] if item not in verified]
    return result
