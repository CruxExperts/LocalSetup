"""Bounded worker proof and controller admission inside existing metadata layers."""
import base64
from dataclasses import dataclass
import hashlib

WORKER_FEATURE = 'worker-snapshot-admission-v1'
MAX_WORKER_PROOF_BYTES = 32*1024


@dataclass(frozen=True, slots=True)
class WorkerSnapshotProof:
    request: bytes

    def __post_init__(self):
        if type(self.request) is not bytes or not 0 < len(self.request) <= MAX_WORKER_PROOF_BYTES:
            raise ValueError('Invalid worker snapshot proof')

    def record(self):
        return {'request': base64.b64encode(self.request).decode('ascii')}


def worker_snapshot_proof(value):
    if (type(value) is not dict or set(value) != {'request'}
            or type(value['request']) is not str
            or len(value['request']) > 4*((MAX_WORKER_PROOF_BYTES+2)//3)):
        raise ValueError('Invalid worker snapshot proof')
    proof = WorkerSnapshotProof(base64.b64decode(value['request'], validate=True))
    if proof.record() != value:
        raise ValueError('Noncanonical worker snapshot proof')
    return proof


def bare_result_digest(logical):
    """Bind the complete proposed result, excluding only its recursive signature."""
    from .artifact_records import canonical_bytes
    claim = dict(logical['snapshot'])
    claim.pop('worker', None)
    return hashlib.sha256(canonical_bytes(dict(logical, snapshot=claim))).hexdigest()


def validate_worker_admission(value, logical, controller):
    from .artifact_records import validate_digest, validate_opaque_id
    from .contracts import normalize_fingerprint
    fields = {'version', 'worker', 'assignment', 'job', 'destination', 'request', 'result'}
    if (type(value) is not dict or set(value) != fields
            or type(value['version']) is not int or value['version'] != 1
            or controller is None or normalize_fingerprint(value['worker']) != value['worker']
            or value['worker'] in (controller, logical['origin']['fingerprint'])):
        raise ValueError('Invalid worker admission')
    validate_opaque_id(value['job'])
    for key in ('assignment', 'destination', 'request', 'result'):
        validate_digest(value[key])
    proof = worker_snapshot_proof(logical['snapshot']['worker'])
    if (value['request'] != hashlib.sha256(proof.request).hexdigest()
            or value['result'] != bare_result_digest(logical)):
        raise ValueError('Worker admission does not match snapshot')


def validate_worker_layers(record, outer, controller, *, allow_origin_unapproved):
    logical = record['logical']
    worker = 'worker' in logical.get('snapshot', {})
    if (worker != (WORKER_FEATURE in record['schema']['required_features'])
            or worker and record['schema']['minor'] < 3):
        raise ValueError('Worker feature declaration mismatch')
    if outer is None:
        if worker and not allow_origin_unapproved:
            raise ValueError('Worker snapshot requires controller admission')
        return
    if (worker != ('worker_admission' in outer)
            or worker != (WORKER_FEATURE in outer['schema']['required_features'])
            or worker and outer['schema']['minor'] < 3):
        raise ValueError('Worker admission declaration mismatch')
    if worker:
        validate_worker_admission(outer['worker_admission'], logical, controller)
