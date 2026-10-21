"""Explicit one-attempt heartbeat dispatch with durable compound reservations."""
import hashlib
import importlib.util
import os
from pathlib import Path, PurePosixPath
import time

from . import heartbeat_action as action
from . import heartbeat_budget_store as store
from . import heartbeat_compaction as compaction
from .profile_setup import _parent
from .runtime_install import selected
from .runtime_lock import LOCK_NAME
from .session_owner import _private, _separate, lease


def _script(name):
    path = Path(__file__).resolve().parents[2]/'skills/ls-codex-heartbeat-checks/scripts'/(name+'.py')
    spec = importlib.util.spec_from_file_location('_owned_'+name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _history(value, workspace, profile, expires, checkpoint, *, receipt=None):
    state = Path(value['state_root'])/'sessions'
    root = state/hashlib.sha256(value['session'].encode()).hexdigest()
    if checkpoint is None:
        if root.exists() or root.is_symlink():
            raise FileExistsError('Fresh heartbeat session already exists')
        return None
    for directory in (root, root/'journal', root/'checkpoints'):
        fd = _private(directory)
        try:
            if store.files._read(fd, LOCK_NAME) is None:
                raise FileNotFoundError('Session coordination evidence is missing')
        finally:
            os.close(fd)
    with lease(state, task=value['task'], session=value['session'], workspace=workspace,
               expires=expires, create=False) as owner:
        if receipt is not None:
            return compaction.verify(owner, receipt, source=checkpoint, profile=profile,
                                     token_limit=value['compact']['tokens'])
        owner.resume_checkpoint(checkpoint, profile=profile)
        return checkpoint


def _freeze(value, binding, profiles, grant):
    root = Path(value['state_root'])/'heartbeat'
    fd = _parent(root/'marker', create=True)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise ValueError('Heartbeat execution storage must be private')
        os.mkdir(binding, 0o700, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = root/binding
    fd = _private(directory)
    try:
        store.files._publish(fd, 'profiles.json', profiles, 0o600)
        store.files._publish(fd, 'grant.json', grant, 0o600)
    finally:
        os.close(fd)
    return directory


def _command(launcher, value, workspace, directory, kind, checkpoint):
    argv = [*launcher, kind, '--profile='+value['profile'], '--profiles', str(directory/'profiles.json'),
            '--workspace', str(workspace), '--runtime-root', value['runtime_root'],
            '--state-root', value['state_root'], '--task', value['task'], '--session', value['session']]
    allocation = value['run'] if kind == 'run' else value['compact']
    argv += ['--timeout', str(allocation['seconds']-20), '--token-limit', str(allocation['tokens'])]
    if kind == 'compact':
        argv += ['--checkpoint', checkpoint, '--disclose-history', '--keep-messages', str(allocation['keep_messages'])]
    else:
        argv += ['--grant', str(directory/'grant.json'), '--resource-parent', value['resource_parent'],
                 '--prompt-stdin', '--format', 'jsonl', '--request-limit', str(allocation['requests']),
                 '--tool-limit', str(allocation['tools'])]
        if checkpoint is not None:
            argv += ['--resume', checkpoint]
        else:
            argv += ['--require-new-session']
    return argv


def _protect(grant, workspace, control_paths):
    scopes = action._decode(grant)['write']
    for path in control_paths:
        control = path.resolve().relative_to(workspace.resolve()).parts
        for scope in scopes:
            parts = () if scope == '.' else PurePosixPath(scope).parts
            if parts[:len(control)] == control or control[:len(parts)] == parts:
                raise PermissionError('Heartbeat grant can mutate control configuration or state')


def execute(source, workspace, accounting_root, *, expected_binding, expected_head, control_paths=()):
    """Only an explicit live controller may call this; no saved queue grants authority."""
    started = time.monotonic()
    workspace = action.path(str(workspace))
    _separate(Path(__file__).resolve().parents[2], workspace)
    plan, value, launcher, profiles, grant = action.prepare(source, workspace, accounting_root)
    _protect(grant, workspace, control_paths)
    if plan['binding'] != expected_binding:
        raise ValueError('Action changed since controller review')
    expires = started+plan['envelope']['seconds']
    state = store.inspect(accounting_root, workspace)
    if state['head'] != expected_head or state['policy']['task'] != value['task']:
        raise ValueError('Accounting head or task differs from the action')
    runner, protocol = _script('heartbeat_process'), _script('heartbeat_protocol')
    with selected(Path(value['runtime_root']), timeout=max(0, min(5, expires-time.monotonic())), create=False) as release:
        if release.name != launcher[-1]:
            raise ValueError('Selected runtime changed since action planning')
        checkpoint = _history(value, workspace, plan['profile_sha256'], expires, value['checkpoint'])
        if time.monotonic() >= expires:
            raise TimeoutError('Heartbeat preflight exhausted the allocation')
        reservation = store.append(accounting_root, workspace,
            dict(type='reserve', operation=value['operation'], run=plan['authorization']['run'],
                 compact=plan['authorization']['compact']), expected_head, binding=expected_binding)
        directory = _freeze(value, expected_binding, profiles, grant)
        phases = []
        outcome = 'failed'
        try:
            for kind in (['compact', 'run'] if value['compact'] is not None else ['run']):
                remaining = expires-time.monotonic()
                if remaining <= 1:
                    raise TimeoutError('Heartbeat allocation expired')
                allocation = value['compact'] if kind == 'compact' else value['run']
                receipt = compaction.Receipt(source=checkpoint, profile=plan['profile_sha256'],
                    token_limit=allocation['tokens']) if kind == 'compact' else protocol.Receipt()
                result = runner.execute(_command(launcher, value, workspace, directory, kind, checkpoint),
                    cwd=workspace, timeout=min(allocation['seconds']-1, remaining-1),
                    stdin_text=value['prompt'] if kind == 'run' else None,
                    output_limit=value['output_bytes'], receipt=receipt,
                    idle_timeout=value['idle_seconds'] if kind == 'run' else None)
                phases.append({'kind': kind, 'process': {k: v for k, v in result.items() if not k.endswith('_tail')}})
                if result['returncode'] != 0 or not result['protocol'] or not result['protocol']['completed']:
                    reason = result.get('termination_reason')
                    outcome = {'cancelled': 'cancelled', 'timeout': 'timed_out',
                               'output_limit': 'output_limit'}.get(reason, 'failed')
                    break
                if kind == 'compact':
                    checkpoint = _history(value, workspace, plan['profile_sha256'], expires, checkpoint,
                                          receipt=result['protocol']['receipt'])
                else:
                    identity = result['protocol']['identity']
                    if identity != {k: value[k] for k in ('task', 'session', 'profile')}:
                        raise ValueError('Coding result identity differs from authorization')
                    checkpoint = _history(value, workspace, plan['profile_sha256'], expires,
                                          result['protocol']['checkpoint'])
                    outcome = 'execution_completed'
        except KeyboardInterrupt:
            outcome = 'cancelled'
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
            outcome = 'failed'
        evidence = {'schema_version': 1, 'operation': value['operation'], 'binding': expected_binding,
                    'outcome': outcome, 'phases': phases, 'checkpoint': checkpoint,
                    'elapsed_seconds': round(time.monotonic()-started, 6)}
        raw = store.files.encode(evidence)
        fd = _private(directory)
        try:
            store.files._publish(fd, 'result.json', raw, 0o600)
        finally:
            os.close(fd)
        digest = hashlib.sha256(raw).hexdigest()
        state = store.append(accounting_root, workspace, dict(type='result', operation=value['operation'], result=digest),
                             reservation['head'])
        return {'schema_version': 1, 'outcome': outcome, 'result': digest, 'evidence': str(directory/'result.json'),
                'accounting': state}
