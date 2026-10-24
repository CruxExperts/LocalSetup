"""Configure one existing LSCli heartbeat connection without activating cron."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import os
from pathlib import Path
import stat
import uuid

import yaml

from . import harness
from .agent.diagnostics import locations
from .agent.profile_setup import _parent
from .agent.profiles import load


def read(path):
    directory = _parent(path, create=False)
    if directory is None:
        raise FileNotFoundError('Initialize the target with harness codex-heartbeat init first')
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o7022):
                raise ValueError('Heartbeat configuration must be an owned regular file without shared writes')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                raw = stream.read(1048577)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    if len(raw) > 1048576:
        raise ValueError('Heartbeat configuration exceeds 1 MiB')
    try:
        config = yaml.safe_load(raw)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError('Heartbeat configuration must contain valid UTF-8 YAML') from exc
    if not isinstance(config, dict):
        raise ValueError('Heartbeat configuration must be a mapping')
    return raw, config


def _replace(path, raw, data, backup_root):
    """Back up exact prior bytes and refuse observed conflicts before replacement."""
    from .terminal_ops.command_scheduler import _file_lock
    backup = backup_root / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex + '.yaml.gz')
    parent = _parent(backup, create=True)
    try:
        if os.fstat(parent).st_mode & 0o077:
            raise ValueError('Heartbeat backup directory must be owner-private')
        with _file_lock(backup_root, 'configuration.lock', blocking=False):
            if read(path)[0] != raw:
                raise ValueError('Heartbeat configuration changed; inspect and plan again')
            fd = os.open(backup.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(gzip.compress(raw, mtime=0))
                stream.flush()
                os.fsync(stream.fileno())
            fd = os.open(backup.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                if gzip.decompress(stream.read()) != raw:
                    raise ValueError('Heartbeat backup verification failed')
            os.fsync(parent)
            directory = _parent(path, create=False)
            if directory is None:
                raise ValueError('Heartbeat configuration parent disappeared; backup retained')
            temporary = '.heartbeat-' + uuid.uuid4().hex
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if read(path)[0] != raw:
                    raise ValueError('Heartbeat configuration changed; backup retained, no replacement')
                os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                os.close(directory)
    finally:
        os.close(parent)
    return str(backup)


def configure(repo_root, target_root, *, mode, provider_profile, grant, context=None,
              executable=None, profiles=None, runtime_root=None, state_root=None,
              resource_parent=None, apply=False, expected_config_sha256=None):
    target = harness._target(target_root, repo_root)
    path = target / harness.HEARTBEAT_CONFIG
    try:
        raw, old = read(path)
    except FileNotFoundError as exc:
        raise ValueError('Initialize the target with harness codex-heartbeat init first') from exc
    digest = hashlib.sha256(raw).hexdigest()
    if expected_config_sha256 is not None and digest != expected_config_sha256:
        raise ValueError('Heartbeat configuration changed from the reviewed digest')
    if mode not in {'review', 'coding'}:
        raise ValueError('Choose review or coding explicitly')
    defaults = locations(Path.home())
    name = 'lscli-review' if mode == 'review' else 'lscli-heartbeat'
    templates = harness._read_yaml(harness._template(repo_root, 'codex_heartbeat.yaml'))['agent_profiles']
    config = deepcopy(old)
    existing = config.setdefault('agent_profiles', {})
    if not isinstance(existing, dict):
        raise ValueError('Heartbeat agent_profiles must be a mapping')
    selected = deepcopy(existing.get(name, templates[name]))
    if not isinstance(selected, dict) or selected.get('launcher') != templates[name]['launcher']:
        raise ValueError('Selected template name has custom content; preserve and inspect it')
    def absolute(value):
        return str(Path(value).expanduser().absolute())
    selected.update(executable=absolute(executable or Path.home() / '.local/bin/lscli'),
                    profiles=absolute(profiles or defaults['profiles']),
                    runtime_root=absolute(runtime_root or defaults['runtimes']),
                    profile=provider_profile, grant=absolute(grant))
    if mode == 'review':
        if resource_parent is not None or state_root is not None:
            raise ValueError('Review mode does not use tool resource or session-state paths')
        if not context:
            raise ValueError('Review mode requires explicit --context files')
        selected['context'] = context
    else:
        if context:
            raise ValueError('Coding uses its prompt and grant; --context belongs to review configuration')
        if resource_parent is None:
            raise ValueError('Coding mode requires an explicit delegated --resource-parent')
        selected.update(state_root=absolute(state_root or defaults['state']), resource_parent=absolute(resource_parent))
    existing[name] = selected
    prior_agent = old.get('agent', {})
    agent = {'enabled': True, 'profile': name}
    if isinstance(prior_agent, dict) and prior_agent.get('profile') == name and 'timeout_seconds' in prior_agent:
        agent['timeout_seconds'] = prior_agent['timeout_seconds']
    config['agent'] = agent
    heartbeat = config.get('heartbeat', {})
    if not isinstance(heartbeat, dict):
        raise ValueError('Heartbeat settings must be a mapping')
    if any(type(heartbeat[key]) is not bool for key in ('enabled', 'scheduled_agent') if key in heartbeat):
        raise ValueError('Heartbeat enabled and scheduled_agent settings must be booleans')
    if heartbeat.get('enabled') and heartbeat.get('scheduled_agent') and config != old:
        raise ValueError('Select --collection-only or disable the heartbeat before changing its recurring agent authority')
    runtime = harness._load_runtime(repo_root)
    runtime.planned_commands({'agent': agent, 'agent_profiles': {name: selected}}, target_root=target)
    # Coding planning validates the launcher and limits; provider metadata is
    # checked here too, without resolving its credential source.
    load(Path(selected['profiles']), provider_profile)
    result = {'ok': True, 'operation': 'configure_lscli', 'status': 'planned', 'mode': mode,
              'config_sha256': digest, 'changed': config != old, 'agent': agent,
              'selected_profile': selected, 'schedule_changed': False, 'backup': None}
    if apply:
        if config != old:
            data = yaml.safe_dump(config, sort_keys=False).encode('utf-8')
            if len(data) > 1048576:
                raise ValueError('Heartbeat configuration exceeds 1 MiB')
            backup_root = runtime.state_root_from_config(target, old) / 'configuration-backups'
            result['backup'] = _replace(path, raw, data, backup_root)
        result['status'] = 'configured' if config != old else 'unchanged'
    return result
