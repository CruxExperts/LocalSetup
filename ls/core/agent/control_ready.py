"""Fresh controller challenge and exact peer context for control discovery."""
import base64
import re

from .control_envelope import ControlBinding, MAX_CIPHERTEXT
from .operation_journal import CONTROL_METHODS, IDENTIFIER
from .remote_control import PROTOCOL


def challenge(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError('Encrypted control requires a fresh 128-bit controller challenge')
    return value


def _methods(value):
    if (type(value) is not frozenset or not value
            or any(type(item) is not str for item in value)
            or not value <= CONTROL_METHODS | {'reconcile'}):
        raise ValueError('Invalid readiness method admission')


def seal_ready(peer, binding, methods, *, nonce):
    challenge(nonce)
    _methods(methods)
    binding.check()
    if binding.scope != peer.scope:
        raise PermissionError('Readiness scope differs from admitted peer')
    raw = peer.seal({'protocol': PROTOCOL, 'schema_version': 2, 'kind': 'ready',
                     'nonce': nonce, 'binding': binding.value(), 'methods': sorted(methods)})
    binding.check()
    return {'encoding': 'base64', 'envelope': base64.b64encode(raw).decode('ascii')}


def open_ready(peer, packet, *, nonce, expected, methods):
    """Verify a fresh response against controller-selected context.

    Generate a new unpredictable nonce per connection attempt; never derive
    expected context or allowed methods from the received packet. Discovery
    yields identity only, not permission to replay a prior operation.
    """
    challenge(nonce)
    _methods(methods)
    if (type(expected) is not dict or set(expected) != {'agent', 'machine', 'task', 'session'}
            or any(type(item) is not str or not IDENTIFIER.fullmatch(item) for item in expected.values())):
        raise ValueError('Readiness requires explicit expected agent, machine, task and session')
    if (type(packet) is not dict or set(packet) != {'encoding', 'envelope'}
            or packet['encoding'] != 'base64' or type(packet['envelope']) is not str
            or len(packet['envelope']) > 4*((MAX_CIPHERTEXT+2)//3)):
        raise ValueError('Invalid readiness envelope')
    raw = base64.b64decode(packet['envelope'], validate=True)
    if base64.b64encode(raw).decode('ascii') != packet['envelope']:
        raise ValueError('Noncanonical readiness encoding')
    value = peer.open(raw)
    if (type(value) is not dict
            or set(value) != {'protocol', 'schema_version', 'kind', 'nonce', 'binding', 'methods'}
            or value['protocol'] != PROTOCOL or type(value['schema_version']) is not int
            or value['schema_version'] != 2 or value['kind'] != 'ready'
            or value['nonce'] != nonce or value['methods'] != sorted(methods)):
        raise PermissionError('Readiness is stale, unsupported or differs from controller admission')
    selected = value['binding']
    if type(selected) is not dict or type(selected.get('scope')) is not list:
        raise ValueError('Invalid readiness binding')
    try:
        binding = ControlBinding(**(selected | {'scope': tuple(selected['scope'])}))
    except TypeError:
        raise ValueError('Invalid readiness binding') from None
    if binding.scope != peer.scope or any(getattr(binding, key) != item for key, item in expected.items()):
        raise PermissionError('Readiness targets a different agent, machine or session')
    binding.check()
    return binding
