import base64
import json
import time

import pytest

from ls.core.agent.control_envelope import ControlBinding
from ls.core.agent.control_ready import open_ready, seal_ready


class Peer:
    """Schema fixture only; real signatures are covered by test_control_run."""
    scope = ('control',)
    def seal(self, value): return json.dumps(value).encode()
    def open(self, raw): return json.loads(raw)


@pytest.fixture
def readiness():
    now = int(time.time())
    binding = ControlBinding('agent', 'machine', ('control',), 'task', 'session',
                             'run', 'operation', 'generation', now, now+60)
    expected = {key: getattr(binding, key) for key in ('agent', 'machine', 'task', 'session')}
    methods = frozenset({'status', 'cancel'})
    peer = Peer()
    packet = seal_ready(peer, binding, methods, nonce='a'*32)
    return peer, binding, expected, methods, packet


@pytest.mark.parametrize('field', ['agent', 'machine', 'task', 'session'])
def test_readiness_requires_controller_expected_context(readiness, field):
    peer, binding, expected, methods, packet = readiness
    with pytest.raises(PermissionError):
        open_ready(peer, packet, nonce='a'*32, expected=expected | {field: 'other'}, methods=methods)


@pytest.mark.parametrize('change', [
    {'nonce': 'b'*32}, {'schema_version': 1}, {'schema_version': True},
    {'kind': 'receipt'}, {'protocol': 'other'}, {'methods': ['cancel', 'status', 'steer']},
    {'methods': ['status', 'cancel']}, {'extra': True},
])
def test_readiness_rejects_stale_or_changed_payload(readiness, change):
    peer, _, expected, methods, packet = readiness
    payload = peer.open(base64.b64decode(packet['envelope'])) | change
    packet['envelope'] = base64.b64encode(peer.seal(payload)).decode()
    with pytest.raises(PermissionError):
        open_ready(peer, packet, nonce='a'*32, expected=expected, methods=methods)


@pytest.mark.parametrize('change', [{'scope': ['other']}, {'extra': True},
                                 {'issued_at': 1, 'expires_at': 2}])
def test_readiness_binding_is_strict_current_and_scope_bound(readiness, change):
    peer, _, expected, methods, packet = readiness
    payload = peer.open(base64.b64decode(packet['envelope']))
    payload['binding'].update(change)
    packet['envelope'] = base64.b64encode(peer.seal(payload)).decode()
    with pytest.raises((PermissionError, ValueError)):
        open_ready(peer, packet, nonce='a'*32, expected=expected, methods=methods)


def test_valid_ready_and_oversized_or_noncanonical_packet(readiness):
    peer, binding, expected, methods, packet = readiness
    assert open_ready(peer, packet, nonce='a'*32, expected=expected, methods=methods) == binding
    for envelope in ('a'*1_400_000, packet['envelope']+'\n'):
        with pytest.raises(ValueError):
            open_ready(peer, packet | {'envelope': envelope}, nonce='a'*32, expected=expected, methods=methods)
    with pytest.raises(ValueError):
        open_ready(peer, packet, nonce=None, expected=expected, methods=methods)
