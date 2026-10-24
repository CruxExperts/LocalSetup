"""Private remote-control admission and protected credential forwarding."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from .control_envelope import RemotePeer, encode
from .operation_journal import CONTROL_METHODS, IDENTIFIER
from .session_owner import _separate
from ..openpgp.secrets import SecretProvider, SecretReference, SecretResolver

SECRET_ENV = 'LOCALSETUP_RUN_CONTROL_SECRETS'


@dataclass(frozen=True)
class Admission:
    agent: str
    methods: frozenset[str]
    peer: RemotePeer


def selection(value, workspace, *, protected=False):
    if value is None:
        return None
    fields = {'agent', 'methods', 'home', 'trust_state', 'store_id', 'revision',
              'epoch', 'scope', 'local_role', 'local_fingerprint', 'remote_role',
              'remote_fingerprint', 'passphrase'}
    if type(value) is not dict or set(value) != fields:
        raise ValueError('Invalid encrypted control admission')
    if (not isinstance(value['agent'], str) or not IDENTIFIER.fullmatch(value['agent'])
            or type(value['methods']) is not list or not value['methods']
            or any(type(method) is not str for method in value['methods'])
            or len(set(value['methods'])) != len(value['methods'])
            or not set(value['methods']) <= CONTROL_METHODS | {'reconcile'}
            or type(value['scope']) is not list or not 1 <= len(value['scope']) <= 16
            or any(not isinstance(part, str) or not IDENTIFIER.fullmatch(part) for part in value['scope'])
            or not isinstance(value['store_id'], str) or not IDENTIFIER.fullmatch(value['store_id'])):
        raise ValueError('Invalid encrypted control identity or methods')
    def path(raw):
        if type(raw) is not str or len(raw) > 4096 or '\x00' in raw:
            raise ValueError('Invalid encrypted control path')
        result = Path(raw)
        if not result.is_absolute() or '..' in result.parts:
            raise ValueError('Encrypted control paths must be absolute')
        _separate(result, workspace)
        return result
    home, trust = path(value['home']), path(value['trust_state'])
    secret = value['passphrase']
    if (type(secret) is not dict
            or set(secret) not in ({'provider', 'name'}, {'provider', 'name', 'executable'})):
        raise ValueError('Invalid encrypted control secret reference')
    reference = SecretReference(SecretProvider(secret['provider']), secret['name'])
    if reference.provider not in {SecretProvider.ENV, SecretProvider.ENVMAN}:
        raise ValueError('Unsupported encrypted control secret provider')
    binary = secret.get('executable', 'envman')
    if 'executable' in secret:
        if reference.provider is not SecretProvider.ENVMAN:
            raise ValueError('Only Envman references select an executable')
        binary = str(path(binary))
    values = None
    if protected:
        raw = os.environ.get(SECRET_ENV, '')
        if len(raw.encode()) > 32_768:
            raise ValueError('Encrypted control launch data exceeds bounds')
        from .broker_rpc import _decode
        packet = _decode(raw.encode())
        if (type(packet) is not dict or set(packet) != {'selection_sha256', 'values'}
                or packet['selection_sha256'] != hashlib.sha256(encode(value)).hexdigest()
                or type(packet['values']) is not dict
                or set(packet['values']) - {reference.name}
                or any(type(item) is not str or len(item.encode()) > 4096 for item in packet['values'].values())):
            raise PermissionError('Encrypted control admission changed across protected startup')
        values = packet['values']
    peer = RemotePeer(home, trust, value['store_id'], value['revision'], value['epoch'],
                      tuple(value['scope']), value['local_role'], value['local_fingerprint'],
                      value['remote_role'], value['remote_fingerprint'], reference,
                      SecretResolver(envman_binary=binary, env_values=values))
    return Admission(value['agent'], frozenset(value['methods']), peer)


def launch_environment(value, workspace):
    admission = selection(value, workspace)
    if admission is None:
        return {}
    reference = admission.peer.passphrase_reference
    values = {}
    if reference.provider is SecretProvider.ENV and reference.name in os.environ:
        secret = os.environ[reference.name]
        if len(secret.encode()) > 4096:
            raise ValueError('Encrypted control credential exceeds bounds')
        values[reference.name] = secret
    return {SECRET_ENV: json.dumps({'selection_sha256': hashlib.sha256(encode(value)).hexdigest(),
                                   'values': values}, sort_keys=True, separators=(',', ':'))}
