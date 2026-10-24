"""Fresh worker possession checks against one authoritative signed assignment.

Phase checks alone do not fence admission. The separate approval guard holds
the assignment writer lease through controller admission, not carrier writes.
The controller endpoint must use the authoritative configuration
store, never a disconnected replica. No new transport, key enrollment or daemon
is created here.
"""
from dataclasses import dataclass, replace
import hashlib
import secrets
import time

from ..agent.runtime_lock import runtime_use
from ..live_configuration import ConfigurationAuthority, ConfigurationStore, _decode, _encode
from .artifact_records import validate_digest, validate_opaque_id
from .container_preparation import _Budget
from .contracts import normalize_fingerprint

_SCHEMA = 'localsetup.snapshot-worker-check'
_MAX_BYTES = 32*1024
_PHASES = ('prepare', 'approve', 'deliver')


class WorkerAuthorityError(PermissionError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkerAssignment:
    assignment_id: str
    artifact_id: str
    worker: str
    enabled: bool = True

    def __post_init__(self):
        validate_opaque_id(self.assignment_id)
        validate_opaque_id(self.artifact_id)
        if normalize_fingerprint(self.worker) != self.worker or type(self.enabled) is not bool:
            raise ValueError('Invalid worker assignment')

    def record(self):
        return dict(assignment_id=self.assignment_id, artifact_id=self.artifact_id,
                    worker=self.worker, enabled=self.enabled)


def _assignment(snapshot, owner):
    try:
        value = snapshot.record['values'][owner]
        if set(value) != {'assignment_id', 'artifact_id', 'worker', 'enabled'}:
            raise ValueError
        result = WorkerAssignment(**value)
        if not result.enabled:
            raise WorkerAuthorityError('worker_disabled')
        return result
    except WorkerAuthorityError:
        raise
    except Exception:
        raise WorkerAuthorityError('assignment_invalid') from None


def _binding(store, owner, snapshot):
    assignment = _assignment(snapshot, owner)
    return hashlib.sha256(_encode(dict(store=store.store_id, owner=owner,
        controller=store.authority.signer, recipients=list(store.authority.recipients),
        revision=snapshot.revision, digest=snapshot.digest, assignment=assignment.record()))).hexdigest()


def _request(value):
    try:
        fields = {'schema', 'version', 'kind', 'assignment',
                  'nonce', 'phase', 'job', 'source', 'destination', 'artifact'}
        if type(value) is dict and value.get('phase') == 'snapshot':
            fields.add('result')
            validate_digest(value.get('result'))
        if (type(value) is not dict or set(value) != fields
                or value['schema'] != _SCHEMA or type(value['version']) is not int
                or value['version'] != 1 or value['kind'] != 'request'
                or value['phase'] not in (*_PHASES, 'snapshot')):
            raise ValueError
        for key in ('assignment', 'nonce', 'source', 'destination'):
            validate_digest(value[key])
        for key in ('artifact', 'job'):
            validate_opaque_id(value[key])
        return value
    except Exception:
        raise WorkerAuthorityError('worker_request_invalid') from None


def _limit(serialized):
    if type(serialized) is not bytes or not 0 < len(serialized) <= _MAX_BYTES:
        raise WorkerAuthorityError('worker_proof_invalid')
    return serialized


class AssignmentController:
    """Run in the controller's trusted endpoint with exclusive signing access.

    Configuration commit and response issuance share the existing store lease.
    Every committed configuration revision is a new assignment generation, even
    if a worker is reassigned to the same key. Protect the authoritative HEAD;
    a signature alone cannot detect a malicious controller rollback or split brain.
    """
    def __init__(self, store: ConfigurationStore, owner: str, *, authorized: bool):
        if not isinstance(store, ConfigurationStore) or authorized is not True or type(owner) is not str:
            raise WorkerAuthorityError('worker_unauthorized')
        self.store, self.owner = store, owner

    def approval_guard(self, *, assignment_binding, job_id, source_commitment,
                       destination_binding, policy, budget):
        from .container_worker_admission import approval_guard
        return approval_guard(self, assignment_binding=assignment_binding, job_id=job_id,
            source_commitment=source_commitment, destination_binding=destination_binding,
            policy=policy, budget=budget)

    def respond(self, serialized, *, timeout_seconds, cancel_check):
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60 or not callable(cancel_check):
            raise WorkerAuthorityError('worker_request_invalid')
        deadline = time.monotonic()+timeout_seconds
        def check():
            if cancel_check() or time.monotonic() >= deadline:
                raise WorkerAuthorityError('worker_check_expired')
            self.store.authority.check()
        check()
        _limit(serialized)
        try:
            with runtime_use(self.store.root, exclusive=True, timeout=0):
                snapshot = self.store.load()
                check()
                intent = self.store._intent()
                if intent is not None and (snapshot is None or intent['candidate'] != snapshot.digest):
                    raise WorkerAuthorityError('assignment_pending')
                assignment = _assignment(snapshot, self.owner)
                if assignment.worker == self.store.authority.signer or assignment.worker not in self.store.authority.recipients:
                    raise WorkerAuthorityError('worker_unauthorized')
                # The signature pin comes from the controller's current signed
                # assignment, never a signer named by an incoming request.
                worker_view = replace(self.store.authority, signer=assignment.worker)
                request = _request(_decode(worker_view.open(serialized)))
                if request['phase'] == 'snapshot':
                    raise WorkerAuthorityError('worker_request_invalid')
                check()
                if (request['assignment'] != _binding(self.store, self.owner, snapshot)
                        or request['artifact'] != assignment.artifact_id):
                    raise WorkerAuthorityError('assignment_changed')
                check()
                response = _limit(self.store.authority.seal(_encode(dict(request, kind='response'))))
                check()
                # This also catches an owner violating the shared writer lease.
                current = self.store.load(minimum_revision=snapshot.revision)
                check()
                if current is None or current.digest != snapshot.digest:
                    raise WorkerAuthorityError('assignment_changed')
                return response
        except WorkerAuthorityError:
            raise
        except TimeoutError:
            raise WorkerAuthorityError('worker_authority_busy') from None
        except Exception:
            raise WorkerAuthorityError('worker_authority_unavailable') from None


class AssignedWorker:
    """Bind a worker key and exact signed assignment before starting a job.

    ``endpoint.respond`` is an operator-bound transport which must honor the
    deadline and cancellation. It must contact the authoritative controller;
    cached responses cannot satisfy a fresh random challenge. A timeout is a
    denial, never permission to use the last successful result.
    """
    def __init__(self, view: ConfigurationStore, owner: str, worker: ConfigurationAuthority,
                 endpoint, *, timeout_seconds=30):
        if (not isinstance(view, ConfigurationStore) or not isinstance(worker, ConfigurationAuthority)
                or type(owner) is not str or not callable(getattr(endpoint, 'respond', None))
                or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60):
            raise WorkerAuthorityError('worker_unauthorized')
        snapshot = view.load()
        assignment = _assignment(snapshot, owner)
        if (worker.signer != assignment.worker or worker.signer == view.authority.signer
                or worker.recipients != view.authority.recipients):
            raise WorkerAuthorityError('worker_unauthorized')
        self.view, self.worker, self.endpoint = view, worker, endpoint
        self.assignment, self.timeout_seconds = assignment, timeout_seconds
        self.binding = _binding(view, owner, snapshot)

    def snapshot_proof(self, logical, *, job_id, destination_binding, budget):
        from .artifact_records import validate_logical
        from .container_worker_records import WorkerSnapshotProof, bare_result_digest
        if not isinstance(budget, _Budget):
            raise WorkerAuthorityError('worker_request_invalid')
        validate_logical(logical, limits=budget.limits.container_limits)
        if (logical['artifact_id'] != self.assignment.artifact_id
                or self.worker.signer == logical['origin']['fingerprint']
                or 'worker' in logical['snapshot']):
            raise WorkerAuthorityError('worker_request_invalid')
        request = _request(dict(schema=_SCHEMA, version=1, kind='request', assignment=self.binding,
            nonce=secrets.token_hex(32), phase='snapshot', job=job_id,
            source=logical['snapshot']['source_commitment'], destination=destination_binding,
            artifact=self.assignment.artifact_id, result=bare_result_digest(logical)))
        budget.checkpoint()
        self.view.authority.check()
        budget.use_crypto()
        proof = WorkerSnapshotProof(self.worker.seal(_encode(request)))
        budget.checkpoint()
        self.view.authority.check()
        return proof

    def authorize(self, phase, *, job_id, source_commitment, destination_binding, budget):
        if not isinstance(budget, _Budget):
            raise WorkerAuthorityError('worker_request_invalid')
        request = _request(dict(schema=_SCHEMA, version=1, kind='request', assignment=self.binding,
            nonce=secrets.token_hex(32), phase=phase, job=job_id, source=source_commitment,
            destination=destination_binding, artifact=self.assignment.artifact_id))
        deadline = min(budget.deadline, time.monotonic()+self.timeout_seconds)
        def check():
            budget.checkpoint()
            self.view.authority.check()
            self.worker.check()
            if time.monotonic() >= deadline:
                raise WorkerAuthorityError('worker_check_expired')
        def cancelled():
            try:
                check()
                return False
            except Exception:
                return True
        try:
            check()
            # Request/response sealing and opening, plus the controller's two
            # signed configuration reads, share the enclosing job's allowance.
            for _ in range(6):
                budget.use_crypto()
            proof = _limit(self.worker.seal(_encode(request)))
            check()
            response = self.endpoint.respond(proof, timeout_seconds=deadline-time.monotonic(),
                                              cancel_check=cancelled)
            check()
            accepted = _decode(self.view.authority.open(_limit(response)))
            if accepted != dict(request, kind='response'):
                raise WorkerAuthorityError('worker_proof_mismatch')
            check()
        except WorkerAuthorityError:
            raise
        except Exception:
            raise WorkerAuthorityError('worker_authority_unavailable') from None
