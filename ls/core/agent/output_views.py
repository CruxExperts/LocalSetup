"""Signed, transaction-pinned output views on the shared configuration cadence."""
import hashlib
import json
import os
from pathlib import Path

from ..live_configuration import ConfigurationError, _NAME
from ..live_configuration_cli import bound_runtime
from ..live_configuration_files import editable_json
from ..openpgp.container_storage import read_private_file
from ..output_filters import FilterError, parse_policy
from .session_owner import _separate

ENVIRONMENT = 'LOCALSETUP_RUN_FILTER_CONTEXT'


def render(view):
    from .run_io import safe
    text = 'Output withheld by filter.' if view['text'] is None else safe(view['text'])
    metadata = view['filter']
    if metadata['action'] != 'allow':
        text += '\n[Filter: ' + metadata['action'] + '; rules: ' + ', '.join(map(str, metadata['orders'])) + ']'
    return text + '\n'


def selection(value, workspace):
    if value is None:
        return None
    if (type(value) is not dict or set(value) != {'authority_file', 'owner'}
            or type(value['authority_file']) is not str or len(value['authority_file']) > 4096
            or '\x00' in value['authority_file'] or type(value['owner']) is not str
            or _NAME.fullmatch(value['owner']) is None):
        raise ConfigurationError('configuration_binding_invalid')
    path = Path(value['authority_file'])
    if not path.is_absolute() or '..' in path.parts:
        raise ConfigurationError('configuration_binding_invalid')
    _separate(path, workspace)
    raw = read_private_file(path, maximum=65536)
    authority = editable_json(raw)
    if (type(authority) is not dict or type(authority.get('owners')) is not dict
            or authority['owners'].get(value['owner']) != {'kind': 'output-filters'}):
        raise ConfigurationError('configuration_owner_unsupported')
    try:
        for name in (authority['root'], authority['gnupg_home'], authority['trust']['path'],
                     authority['secret']['protected_file_root']):
            if name is not None:
                _separate(Path(name), workspace)
    except (KeyError, TypeError):
        raise ConfigurationError('configuration_binding_invalid') from None
    return path, authority, hashlib.sha256(raw).hexdigest()


def _policy(value, *, retain):
    policy = parse_policy(value)
    available = {'observation', 'agent-context'} | ({'retained-log'} if retain else set())
    if any(set(rule.sinks) - available for rule in policy.rules):
        raise FilterError('filter_sink_unavailable')
    return policy


def _load(value, workspace, environment, *, retain=False):
    path, authority, digest = selection(value, workspace)
    runtime, _defaults, actual, _writable = bound_runtime(path,
        source_root=Path(__file__).resolve().parents[3], home=Path.home(), env_values=environment)
    if actual != digest:
        raise ConfigurationError('configuration_authority_changed')
    policy = _policy(runtime.value(value['owner']), retain=retain)
    # Validate run capabilities before accepting any later signed revision.
    # Other owners keep their existing validators and atomic set validation.
    def validate(candidate):
        _policy(candidate, retain=retain)
    runtime.validators[value['owner']] = validate
    return runtime, policy, authority, digest


def launch_environment(value, workspace, *, retain=False):
    if value is None:
        if retain:
            raise FilterError('filter_required_for_retention')
        return {}
    runtime, _policy, authority, digest = _load(value, workspace, os.environ, retain=retain)
    secret = authority['secret']
    environment = {}
    if secret['provider'] == 'env' and secret['name'] in os.environ:
        environment[secret['name']] = os.environ[secret['name']]
    # Source-selected names remain JSON data, never interpreter environment keys.
    capsule = dict(selection=value, retain=retain, authority=digest, revision=runtime.accepted.revision,
                   snapshot=runtime.accepted.digest, environment=environment)
    raw = json.dumps(capsule, separators=(',', ':'), ensure_ascii=True)
    if len(raw.encode()) > 65536:
        raise ConfigurationError('configuration_binding_invalid')
    return {ENVIRONMENT: raw}


def protected(value, workspace, check, cancelled, *, retain=False):
    raw = os.environ.get(ENVIRONMENT)
    if value is None:
        if retain:
            raise FilterError('filter_required_for_retention')
        if raw is not None:
            raise ConfigurationError('configuration_binding_changed')
        return None
    if raw is None or len(raw.encode()) > 65536:
        raise ConfigurationError('configuration_binding_invalid')
    capsule = editable_json(raw.encode())
    if (type(capsule) is not dict or set(capsule) != {'selection', 'retain', 'authority', 'revision', 'snapshot', 'environment'}
            or type(capsule['retain']) is not bool or capsule['retain'] != retain
            or capsule['selection'] != value or type(capsule['environment']) is not dict
            or len(capsule['environment']) > 1
            or any(type(k) is not str or type(v) is not str for k, v in capsule['environment'].items())):
        raise ConfigurationError('configuration_binding_changed')
    check()
    runtime, policy, _authority, digest = _load(value, workspace, capsule['environment'], retain=retain)
    if (digest != capsule['authority'] or type(capsule['revision']) is not int
            or runtime.accepted.revision != capsule['revision'] or runtime.accepted.digest != capsule['snapshot']):
        raise ConfigurationError('configuration_binding_changed')
    # The resolver has its own private copy. Do not leave the launch capsule
    # available to later provider subprocesses through ambient inheritance.
    os.environ.pop(ENVIRONMENT, None)
    return Observations(policy, check, cancelled, authority_check=runtime.store.authority.check,
                        runtime=runtime, owner=value['owner'])


class Observations:
    """Only complete SDK text parts and final output enter the display view."""
    def __init__(self, policy, check, cancelled, *, authority_check=lambda: None, runtime=None, owner=None):
        self.policy, self.check, self.cancelled = policy, check, cancelled
        self.authority_check = authority_check
        self.retention = None
        self.runtime, self.owner, self.error = runtime, owner, None
        self.applied = None if runtime is None else runtime.accepted.digest
        from .context_filters import ContextFilter
        self.context_views = ContextFilter(policy, check, cancelled, authority_check, refresh=self.poll)
        # Keep the provider boundary installed even if context rules start empty.
        self.context = self.context_views

    def poll(self):
        """Called by the owner loop and before delivery, never within a projection."""
        self.check()
        self.authority_check()
        if self.runtime is not None:
            try:
                if self.runtime.poll(authorized=False):
                    policy = parse_policy(self.runtime.value(self.owner))
                    self.check()
                    self.policy = self.context_views.policy = policy
                    self.applied, self.error = self.runtime.accepted.digest, None
            except ConfigurationError as error:
                self.error = error.code
                raise
            # A failed reload cannot be bypassed by another call before cadence.
            if self.error is not None:
                raise ConfigurationError(self.error)
        self.check()

    def text(self, value, *, kind='text', status=None, checkpoint=None):
        self.poll()
        policy = self.policy
        result = policy.apply(value, sink='observation', check=self.check)
        retained = (None if self.retention is None else
                    policy.apply(value, sink='retained-log', check=self.check))
        self.authority_check()
        self.check()
        if result.action == 'halt' or retained is not None and retained.action == 'halt':
            self.cancelled.set()
            raise InterruptedError('Output filter requested cancellation')
        if retained is not None:
            self.retention.append({'text': retained.text, 'filter': retained.metadata()},
                                  kind=kind, status=status, checkpoint=checkpoint)
        return {'text': result.text, 'filter': result.metadata()}

    def progress(self, event):
        self.check()
        self.authority_check()
        # Partial deltas, tool arguments/results, reasoning and opaque extension
        # fields cannot be forwarded as unfiltered side channels. The SDK's
        # part_end carries a complete part; no fragment concatenation is needed.
        if event.get('event_kind') != 'part_end':
            return None
        part = event.get('part')
        if type(part) is not dict or part.get('part_kind') != 'text':
            return None
        return self.text(part.get('content'))
