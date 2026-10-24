"""One encrypted request/receipt protocol, before transport-specific framing.

Callers must supply an admitted peer, binding and live authority callback.
Network authentication alone cannot construct execution authority here.
"""
import base64
import hashlib
import re
import time

from .broker_rpc import _decode
from .control_envelope import ControlBinding, RemotePeer, encode
from .control_requests import Requests
from .operation_journal import CONTROL_METHODS, Journal

PROTOCOL = 'localsetup.agent-control'


def _identity(peer, binding, request_id):
    if (not isinstance(peer, RemotePeer) or not isinstance(binding, ControlBinding)
            or not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id)
            or peer.scope != binding.scope):
        raise ValueError('Invalid remote control identity')
    binding.check()
    peer.check()


def _frame(binding, request_id, kind, payload):
    return {'protocol': PROTOCOL, 'schema_version': 1, 'kind': kind,
            'binding': binding.value(), 'request_id': request_id, 'payload': payload}


def _open(peer, binding, request_id, kind, ciphertext):
    _identity(peer, binding, request_id)
    value = peer.open(ciphertext)
    if (not isinstance(value, dict) or set(value) != {
            'protocol', 'schema_version', 'kind', 'binding', 'request_id', 'payload'}
            or value['protocol'] != PROTOCOL or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['kind'] != kind
            or encode(value['binding']) != encode(binding.value()) or value['request_id'] != request_id):
        raise PermissionError('Remote control frame does not match admitted operation')
    binding.check()
    return value['payload']


def _request(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 16384:
        raise ValueError('Control request exceeds limit')
    value = _decode(raw)
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or type(value.get('id')) is not int
            or not 1 <= value['id'] <= 1024 or not isinstance(value.get('method'), str)
            or value['method'] not in CONTROL_METHODS):
        raise ValueError('Invalid control request')
    return value


def seal_request(peer, binding, request_id, raw):
    _identity(peer, binding, request_id)
    _request(raw)
    result = peer.seal(_frame(binding, request_id, 'request', base64.b64encode(raw).decode('ascii')))
    binding.check()
    return result


def serve_request(peer, binding, request_id, ciphertext, *, requests, journal, authorize):
    """Verify, admit, journal and dispatch once; failures never retry effects.

    authorize(binding, method) must check the current live run and method grant.
    It runs before dispatch and after the result, and must return exactly True.
    """
    if (not isinstance(binding, ControlBinding)
            or not isinstance(requests, Requests) or not isinstance(journal, Journal)
            or (journal.task, journal.session) != (binding.task, binding.session)
            or not callable(authorize)):
        raise ValueError('Remote control requires its bound owner and journal')
    payload = _open(peer, binding, request_id, 'request', ciphertext)
    if not isinstance(payload, str) or len(payload) > 4*((16384+2)//3):
        raise ValueError('Invalid encrypted control request')
    raw = base64.b64decode(payload, validate=True)
    if base64.b64encode(raw).decode('ascii') != payload:
        raise ValueError('Noncanonical encrypted control request')
    value = _request(raw)
    if authorize(binding, value['method']) is not True:
        raise PermissionError('Remote control lacks live execution authority')
    _identity(peer, binding, request_id)
    receipt = requests.dispatch_recorded(raw, journal, binding_sha256=binding.digest(), request_id=request_id)
    if authorize(binding, value['method']) is not True:
        raise PermissionError('Remote control authority ended after dispatch')
    _identity(peer, binding, request_id)
    encrypted = peer.seal(_frame(binding, request_id, 'receipt', receipt))
    if (authorize(binding, value['method']) is not True or requests.closed
            or time.monotonic() >= requests.expires):
        raise PermissionError('Remote control authority ended before acknowledgement')
    binding.check()
    return encrypted


def open_receipt(peer, binding, request_id, ciphertext, *, request):
    original = _request(request)
    receipt = _open(peer, binding, request_id, 'receipt', ciphertext)
    if (not isinstance(receipt, dict) or set(receipt) != {
            'operation', 'request_id', 'binding_sha256', 'request_sha256', 'result_sha256', 'result'}
            or receipt['request_id'] != request_id or receipt['binding_sha256'] != binding.digest()
            or receipt['request_sha256'] != hashlib.sha256(request).hexdigest()
            or not isinstance(receipt['operation'], str) or not re.fullmatch(r'[0-9a-f]{32}', receipt['operation'])
            or receipt['result_sha256'] != hashlib.sha256(encode(receipt['result'])).hexdigest()
            or not isinstance(receipt['result'], dict)
            or type(receipt['result'].get('schema_version')) is not int
            or receipt['result']['schema_version'] != 1
            or type(receipt['result'].get('id')) is not int
            or receipt['result']['id'] != original['id']):
        raise PermissionError('Remote receipt does not match the exact request')
    result = receipt['result']
    fields = {'schema_version', 'id', 'status'}
    if original['method'].startswith('events.'):
        fields.add('events')
    statuses = {'steer': {'queued'}, 'approve': {'decision_recorded'},
                'cancel': {'cancellation_requested'}}.get(original['method'], {'active', 'cancellation_requested'})
    if set(result) != fields or not isinstance(result.get('status'), str) or result['status'] not in statuses:
        raise PermissionError('Remote receipt has invalid result semantics')
    return receipt
