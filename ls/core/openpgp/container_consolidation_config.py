"""Private, explicitly selected write authority separate from ordinary read grants."""
from dataclasses import dataclass
import hashlib
from pathlib import Path

from .artifact_records import validate_digest, validate_opaque_id
from .container_storage import read_private_file


@dataclass(frozen=True, slots=True)
class ConsolidationBinding:
    path: Path
    sha256: str

    def __post_init__(self):
        if not isinstance(self.path, Path) or not self.path.is_absolute() or '..' in self.path.parts:
            raise ValueError('Invalid consolidation authority path')
        validate_digest(self.sha256)

    def record(self):
        return dict(path=str(self.path), sha256=self.sha256)


def parse_binding(value):
    if value is None:
        return None
    if type(value) is not dict or set(value) != {'path', 'sha256'}:
        raise ValueError('Invalid consolidation binding')
    return ConsolidationBinding(Path(value['path']), value['sha256'])


def read_binding(binding):
    from ..live_configuration_files import editable_json
    raw = read_private_file(binding.path, maximum=128*1024)
    if hashlib.sha256(raw).hexdigest() != binding.sha256:
        raise PermissionError('Consolidation authority changed')
    value = editable_json(raw)
    fields = {'schema', 'version', 'mode', 'artifacts', 'events', 'namespace', 'publisher',
              'controller', 'destination', 'worker', 'max_jobs'}
    if (type(value) is not dict or set(value) != fields
            or value['schema'] != 'localsetup.snapshot-consolidation' or type(value['version']) is not int
            or value['version'] != 1 or value['mode'] not in ('ad-hoc', 'client', 'worker')
            or type(value['artifacts']) is not list or not 1 <= len(value['artifacts']) <= 64
            or value['artifacts'] != sorted(set(value['artifacts']))
            or type(value['events']) is not list or value['events'] != sorted(set(value['events']))
            or any(event not in ('manual', 'change', 'scheduled') for event in value['events'])
            or type(value['max_jobs']) is not int or not 1 <= value['max_jobs'] <= 1024
            or (value['mode'] == 'worker') != (value['worker'] is not None)):
        raise ValueError('Invalid consolidation authority')
    for artifact in value['artifacts']:
        validate_opaque_id(artifact)
    destination = value['destination']
    if (type(destination) is not dict or set(destination) != {'kind', 'path', 'mode'}
            or destination['kind'] != 'local-directory'
            or destination['mode'] not in ('conditional', 'content_addressed')):
        raise ValueError('Invalid consolidation destination')
    return value


def _worker_authority(worker):
    from ..live_configuration_files import editable_json
    raw = read_private_file(Path(worker['authority_file']), maximum=65536)
    if hashlib.sha256(raw).hexdigest() != worker['authority_sha256']:
        raise PermissionError('Worker authority changed')
    return editable_json(raw)


def check_workspace(binding, workspace):
    """Check private selections without unlocking or using a publication key."""
    from ..agent.session_owner import _separate
    value = read_binding(binding)
    paths = [binding.path, Path(value['destination']['path'])]
    keys = [value[role]['key'] for role in ('publisher', 'controller')]
    for role in ('publisher', 'controller'):
        paths.extend((Path(value[role]['trust_state_path']), Path(value[role]['policy']['boundary'])))
    worker = value['worker']
    if worker is not None:
        paths.extend((Path(worker['authority_file']), Path(worker['home'])))
        authority = _worker_authority(worker)
        paths.extend(Path(authority[name]) for name in ('root', 'gnupg_home'))
        paths.append(Path(authority['trust']['path']))
        if authority['secret']['protected_file_root'] is not None:
            paths.append(Path(authority['secret']['protected_file_root']))
        keys.append(worker['key'])
    for key in keys:
        paths.append(Path(key['home']))
        if key['protected_file_root'] is not None:
            paths.append(Path(key['protected_file_root']))
    for path in paths:
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('Invalid consolidation authority path')
        _separate(path, workspace)


def selected_environment(binding, environment):
    """Return names explicitly selected by private authority, never ambient extras."""
    value = read_binding(binding)
    references = [value[role]['key']['reference'] for role in ('publisher', 'controller')]
    worker = value['worker']
    if worker is not None:
        references.append(worker['key']['reference'])
        authority = _worker_authority(worker)
        references.append(authority['secret'])
    selected = {}
    for reference in references:
        if reference['provider'] == 'env' and reference['name'] in environment:
            name, secret = reference['name'], environment[reference['name']]
            if type(name) is not str or len(name) > 128 or type(secret) is not str or len(secret.encode()) > 4096:
                raise ValueError('Invalid configured environment')
            selected[name] = secret
    return selected
