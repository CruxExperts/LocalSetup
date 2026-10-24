"""Signed resource tuning at container operation boundaries, without enrollment."""
from dataclasses import replace
import hashlib
from pathlib import Path

from ..live_configuration import ConfigurationError
from ..live_configuration_cli import bound_runtime
from ..live_configuration_files import editable_json
from ..agent.runtime_lock import runtime_use
from . import container_runtime as runtime
from .container_storage import read_private_file

_FIELDS = ('scan_interval_seconds', 'max_scan_objects', 'max_metadata_bytes', 'max_delta_depth')
_SCHEMA = 'localsetup.container-configuration-binding'


def _digest(value):
    return hashlib.sha256(runtime._canonical(value)).hexdigest()


def _fixed(config):
    value = runtime._config_dict(config)
    for name in _FIELDS:
        value.pop(name)
    return value


def binding(root):
    value = runtime._read(root/'configuration-binding.json')
    if value is not None and (set(value) != {'schema', 'version', 'authority_file', 'authority_digest',
            'config_owner', 'home', 'base_digest'} or value['schema'] != _SCHEMA
            or type(value['version']) is not int or value['version'] != 1
            or any(type(value[key]) is not str or not runtime._HEX64.fullmatch(value[key])
                   for key in ('authority_digest', 'base_digest'))
            or type(value['config_owner']) is not str):
        raise ConfigurationError('configuration_binding_invalid')
    if value is not None:
        for key in ('authority_file', 'home'):
            runtime._absolute(Path(value[key]))
    return value


def binding_digest(root):
    selected = binding(root)
    if selected is not None:
        _authority(selected)
    return None if selected is None else _digest(selected)


def _authority(selected):
    raw = read_private_file(Path(selected['authority_file']), maximum=65536)
    if hashlib.sha256(raw).hexdigest() != selected['authority_digest']:
        raise ConfigurationError('configuration_authority_changed')
    return editable_json(raw)


def environment_names(root):
    selected = binding(root)
    if selected is None:
        return ()
    secret = _authority(selected)['secret']
    return (secret['name'],) if secret['provider'] == 'env' else ()


class ContainerConfiguration:
    def __init__(self, root, base, *, workspace=None, env_values=None, selected=None):
        self.root, self.base = root, base
        self.selected = binding(root) if selected is None else selected
        self.shared = None
        self.config = base
        self.applied = None
        self.error = None
        if self.selected is None:
            return
        if self.selected['base_digest'] != _digest(runtime._config_dict(base)):
            raise ConfigurationError('configuration_conflict')
        authority = _authority(self.selected)
        if authority['owners'].get(self.selected['config_owner']) != {
                'kind': 'container-runtime', 'runtime_id': base.runtime_id}:
            raise ConfigurationError('configuration_owner_unsupported')
        if workspace is not None:
            from ..agent.session_owner import _separate
            for value in (self.selected['authority_file'], authority['root'], authority['gnupg_home'],
                          authority['trust']['path'], authority['secret']['protected_file_root']):
                if value is not None:
                    _separate(Path(value), workspace)
        self.shared, _defaults, digest, _write = bound_runtime(Path(self.selected['authority_file']),
            source_root=Path(__file__).resolve().parents[3], home=Path(self.selected['home']), env_values=env_values)
        if digest != self.selected['authority_digest']:
            raise ConfigurationError('configuration_authority_changed')
        self._accept()

    def _accept(self):
        candidate = runtime._parse_config(self.shared.value(self.selected['config_owner']), self.base.runtime_id)
        if _fixed(candidate) != _fixed(self.base):
            raise ConfigurationError('configuration_runtime_lifecycle_required')
        self.config, self.error = candidate, None
        self.applied = {'revision': self.shared.accepted.revision, 'digest': self.shared.accepted.digest}

    def check(self):
        if self.selected is not None:
            if binding(self.root) != self.selected:
                raise ConfigurationError('configuration_binding_changed')
            self.shared.store.authority.check()

    def poll(self):
        if self.shared is None:
            return self.config
        self.check()
        try:
            if self.shared.poll(authorized=False):
                self._accept()
        except ConfigurationError as error:
            # Retain the last applied tuning. Current authority is still
            # required; an authority failure cannot be hidden by old values.
            self.error = error.code
            self.check()
        return self.config

    def tune_reader(self, reader):
        config = self.poll()
        limits = replace(reader.limits, container_limits=replace(reader.limits.container_limits,
            max_metadata_bytes=config.max_metadata_bytes))
        return config, replace(reader, limits=limits, max_delta_depth=config.max_delta_depth)


def bind_configuration(root, *, authority_file, config_owner, home, authorized, env_values=None):
    if authorized is not True:
        raise ConfigurationError('configuration_unauthorized')
    root = runtime._root(root)
    runtime._absolute(authority_file)
    runtime._absolute(home)
    base = runtime.load_runtime_config(root)
    if base.revision == 0:
        raise ConfigurationError('configuration_runtime_required')
    raw = read_private_file(authority_file, maximum=65536)
    selected = dict(schema=_SCHEMA, version=1, authority_file=str(authority_file),
        authority_digest=hashlib.sha256(raw).hexdigest(), config_owner=config_owner, home=str(home),
        base_digest=_digest(runtime._config_dict(base)))
    consumer = ContainerConfiguration(root, base, env_values=env_values, selected=selected)
    with runtime_use(root, exclusive=True, timeout=0):
        if runtime._load_config_locked(root) != base:
            raise ConfigurationError('configuration_conflict')
        existing = binding(root)
        # Explicit rebinding may acknowledge an already completed legacy
        # lifecycle revision, but cannot silently replace configuration authority.
        if (existing is not None and
                {k: v for k, v in existing.items() if k != 'base_digest'} !=
                {k: v for k, v in selected.items() if k != 'base_digest'}):
            raise ConfigurationError('configuration_binding_changed')
        consumer.shared.store.authority.check()
        runtime._write(root/'configuration-binding.json', selected)
    return {'ok': True, 'state': 'bound', 'revision': consumer.shared.accepted.revision,
            'digest': consumer.shared.accepted.digest}
