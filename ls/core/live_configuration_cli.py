"""Controller CLI for one explicitly bound signed configuration owner set."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets

from .live_configuration import ConfigurationAuthority, ConfigurationError, ConfigurationStore
from .live_configuration_files import editable_json, export_edit, flush_edit
from .live_configuration_runtime import ConfigurationRuntime
from .openpgp.container_identity import machine_binding
from .openpgp.container_storage import read_private_file
from .openpgp.secrets import SecretProvider, SecretReference, SecretResolver
from .openpgp.trust_state import load_trust_state


def _owners(specs, source_root, home):
    if type(specs) is not dict or not 1 <= len(specs) <= 64:
        raise ConfigurationError('configuration_binding_invalid')
    defaults, validators = {}, {}
    for name, spec in specs.items():
        if type(spec) is not dict:
            raise ConfigurationError('configuration_binding_invalid')
        if spec.get('kind') == 'application-profile' and set(spec) == {'kind', 'application', 'name'}:
            from .application_profiles.service import resolve_profile, validate_profile
            profile = resolve_profile(spec['application'], spec['name'], source_root=source_root, home=home)
            defaults[name] = profile['settings']
            def validate(value, profile=profile):
                validate_profile(profile['application'], profile['name'], value,
                    source_root=source_root, base_profile=profile['base_profile'])
        elif spec == {'kind': 'terminal-runtime'}:
            from .application_profiles.service import load_tmux_runtime_defaults
            from .terminal_ops.command_theme import PRESENTATION_KEYS, validate_theme
            from .schema import validate_json_schema
            document = load_tmux_runtime_defaults(source_root=source_root)
            defaults[name] = document['settings']
            def validate(value, document=document):
                def complete(default, incoming):
                    if isinstance(default, dict):
                        # UI additions are optional for previously signed settings.
                        required = default.keys() - (PRESENTATION_KEYS - {'theme'}) - {'native_shortcuts'}
                        return (isinstance(incoming, dict) and required <= incoming.keys()
                                and all(complete(default[key], incoming[key]) for key in required
                                        if key != 'theme'))
                    return True
                if not complete(document['settings'], value):
                    raise ConfigurationError('configuration_incomplete')
                try:
                    validate_theme(value['theme'])
                except (ValueError, TypeError, AttributeError):
                    raise ConfigurationError('configuration_invalid') from None
                if validate_json_schema(document | {'settings': value},
                        source_root/'ls/config/application-profile-tmux-runtime.schema.json',
                        label='signed terminal runtime'):
                    raise ConfigurationError('configuration_invalid')
        elif spec.get('kind') == 'container-runtime' and set(spec) == {'kind', 'runtime_id'}:
            from .openpgp.container_runtime import RuntimeConfig, _config_dict, _parse_config
            defaults[name] = _config_dict(RuntimeConfig(spec['runtime_id']))
            def validate(value, runtime_id=spec['runtime_id']):
                _parse_config(value, runtime_id)
        elif spec == {'kind': 'output-filters'}:
            from .output_filters import defaults as filter_defaults, validate_policy
            defaults[name] = filter_defaults()
            validate = validate_policy
        elif spec.get('kind') == 'snapshot-worker' and set(spec) == {'kind', 'assignment_id', 'artifact_id', 'worker'}:
            from .openpgp.container_worker_authority import WorkerAssignment
            defaults[name] = WorkerAssignment(**{key: value for key, value in spec.items() if key != 'kind'},
                                               enabled=False).record()
            def validate(value):
                if type(value) is not dict or set(value) != {'assignment_id', 'artifact_id', 'worker', 'enabled'}:
                    raise ConfigurationError('configuration_invalid')
                WorkerAssignment(**value)
        else:
            raise ConfigurationError('configuration_owner_unsupported')
        validators[name] = validate
    return defaults, validators


def bound_runtime(path: Path, *, source_root: Path, home: Path, env_values=None):
    """The private binding is bootstrap authority, never an editable owner value."""
    raw = read_private_file(path, maximum=65536)
    binding = editable_json(raw)
    fields = {'schema', 'version', 'root', 'store_id', 'signer', 'recipients', 'gnupg_home',
              'secret', 'trust', 'owners', 'write'}
    if (type(binding) is not dict or set(binding) != fields
            or binding['schema'] != 'localsetup.configuration-authority'
            or type(binding['version']) is not int or binding['version'] != 1
            or type(binding['write']) is not bool or type(binding['recipients']) is not list):
        raise ConfigurationError('configuration_binding_invalid')
    trust, secret = binding['trust'], binding['secret']
    if (type(trust) is not dict or set(trust) != {'path', 'store_id', 'revision', 'epoch', 'scope'}
            or type(trust['revision']) is not int or type(trust['epoch']) is not int
            or trust['revision'] < 1 or trust['epoch'] < 1
            or type(trust['scope']) is not list or not 1 <= len(trust['scope']) <= 32
            or any(type(v) is not str for v in trust['scope'])
            or trust['scope'] != sorted(set(trust['scope']))
            or type(secret) is not dict or set(secret) != {'provider', 'name', 'protected_file_root'}):
        raise ConfigurationError('configuration_binding_invalid')
    for value in (path, Path(binding['root']), Path(binding['gnupg_home']), Path(trust['path'])):
        if not value.is_absolute() or '..' in value.parts:
            raise ConfigurationError('configuration_binding_invalid')
    reference = SecretReference(SecretProvider(secret['provider']), secret['name'])
    if (reference.provider is SecretProvider.PROTECTED_FILE) != (secret['protected_file_root'] is not None):
        raise ConfigurationError('configuration_binding_invalid')
    def current():
        if read_private_file(path, maximum=65536) != raw:
            raise ConfigurationError('configuration_authority_changed')
        state = load_trust_state(Path(trust['path']), expected_scope=tuple(trust['scope']))
        if ((state.store_id, state.revision, state.epoch) != (trust['store_id'], trust['revision'], trust['epoch'])
                or state.owner is None or state.owner.fingerprint != authority.signer):
            raise ConfigurationError('configuration_unauthorized')
    authority = ConfigurationAuthority(binding['signer'], tuple(binding['recipients']),
        Path(binding['gnupg_home']), reference,
        SecretResolver(protected_file_root=secret['protected_file_root'], env_values=env_values), current)
    authority.check()
    defaults, validators = _owners(binding['owners'], source_root, home)
    runtime = ConfigurationRuntime(ConfigurationStore(Path(binding['root']), binding['store_id'], authority),
        validators, actor=hashlib.sha256(authority.signer.encode()).hexdigest(),
        machine=machine_binding().digest, operation=secrets.token_hex(32))
    return runtime, defaults, hashlib.sha256(raw).hexdigest(), binding['write']


def main(argv=None, *, source_root: Path, home: Path | None = None):
    parser = argparse.ArgumentParser(prog='localsetup config',
        epilog='Common preferences: config show | explain | schema | validate | presets. These read-only verbs require no authority binding.')
    parser.add_argument('--authority-file', required=True, type=Path,
                        help='Private controller-selected configuration authority binding')
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('status', help='Inspect the verified revision without disclosing values')
    edit = actions.add_parser('edit', help='Export a new private editable owner set')
    edit.add_argument('--directory', required=True, type=Path)
    flush = actions.add_parser('flush', help='Validate, sign and reload every owner in the editable set')
    flush.add_argument('--directory', required=True, type=Path)
    group = actions.add_parser('flush-all', help='Capture, save and reload the explicitly connected process cohort')
    group.add_argument('--participant-fd', required=True, action='append', type=int)
    group.add_argument('--cohort', required=True, help='Controller-selected identity of the inherited channels')
    group.add_argument('--timeout', type=int, default=300)
    recover = actions.add_parser('recover', help='Reconcile an exact retained revision without signing again')
    recover.add_argument('--candidate', help='Opaque candidate digest from an uncertain result')
    recover.add_argument('--participant-fd', action='append', type=int)
    recover.add_argument('--cohort')
    recover.add_argument('--timeout', type=int, default=300)
    cancel = actions.add_parser('cancel-flush', help='Release an uncommitted capture after reconciling its controller')
    cancel.add_argument('--transaction', required=True)
    cancel.add_argument('--participant-fd', required=True, action='append', type=int)
    cancel.add_argument('--cohort', required=True)
    cancel.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args(argv)
    try:
        runtime, defaults, binding, writable = bound_runtime(args.authority_file,
            source_root=source_root, home=home or Path.home())
        snapshot = runtime.accepted
        result = {'ok': True, 'action': args.action}
        if args.action == 'edit':
            directory = export_edit(runtime, args.directory, binding_digest=binding, defaults=defaults)
            result['edit_directory'] = str(directory)
        elif args.action == 'flush':
            snapshot = flush_edit(runtime, args.directory, binding_digest=binding, authorized=writable)
        elif args.action == 'flush-all':
            from .live_configuration_channels import flush_connected
            snapshot = flush_connected(runtime, args.participant_fd, cohort=args.cohort,
                                       timeout_seconds=args.timeout, authorized=writable)
        elif args.action == 'cancel-flush':
            from .live_configuration_channels import reconcile_connected
            reconcile_connected(runtime, args.participant_fd, transaction=args.transaction,
                                cohort=args.cohort, timeout_seconds=args.timeout, authorized=writable)
        elif args.action == 'recover':
            from contextlib import nullcontext
            from .live_configuration_channels import connected, reconcile_channels
            peers = (nullcontext(((), None)) if args.participant_fd is None else
                     connected(runtime, args.participant_fd, cohort=args.cohort,
                               timeout_seconds=args.timeout, authorized=writable))
            with peers as (channels, _deadline):
                pending = runtime.store._intent()
                candidate = args.candidate or (pending['candidate'] if pending is not None else None)
                if candidate is None:
                    raise ConfigurationError('configuration_pending')
                runtime._validate_set(runtime.store._candidate(candidate))
                snapshot = runtime.store.recover(authorized=writable, candidate_digest=candidate)
                runtime.reload()
                if channels:
                    reconcile_channels(runtime, channels, snapshot=snapshot)
        result.update(configured=snapshot is not None, revision=0 if snapshot is None else snapshot.revision,
            digest=None if snapshot is None else snapshot.digest, cadence_seconds=runtime.cadence,
            owner_count=len(runtime.validators))
    except ConfigurationError as error:
        result = {'ok': False, 'code': error.code}
        if error.candidate is not None:
            result['candidate'] = error.candidate
        if getattr(error, 'transaction', None) is not None:
            result['transaction'] = error.transaction
    except Exception:
        result = {'ok': False, 'code': 'configuration_unavailable'}
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['ok'] else 2
