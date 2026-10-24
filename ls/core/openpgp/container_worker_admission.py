"""Controller approval serialized with the authoritative assignment writer.

The existing signed approval wrapper is the admission receipt. This is not a
distributed lock: the controller must have exclusive signing custody and one
protected authoritative configuration store.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib

from ..agent.runtime_lock import runtime_use
from ..live_configuration import _decode
from .artifact_records import logical_commitment, validate_digest, validate_opaque_id
from .container_preparation import _Budget
from .container_worker_authority import WorkerAuthorityError, _assignment, _binding, _request
from .container_worker_records import (
    bare_result_digest, validate_worker_admission, worker_snapshot_proof,
)

_ISSUER = object()


class WorkerAdmission:
    """A controller-local capability, valid only while its assignment lease lives."""
    def __init__(self, issuer, controller, snapshot, expected, policy, budget):
        if issuer is not _ISSUER:
            raise WorkerAuthorityError('worker_unauthorized')
        self.controller, self.snapshot, self.expected = controller, snapshot, expected
        self.policy, self.budget = policy, budget
        self.active = True
        self._verified = None

    def check(self):
        if not self.active:
            raise WorkerAuthorityError('worker_admission_closed')
        self.budget.checkpoint()
        store = self.controller.store
        store.authority.check()
        if store._head() != {'revision': self.snapshot.revision, 'digest': self.snapshot.digest}:
            raise WorkerAuthorityError('assignment_changed')

    def receipt(self, metadata, policy, budget):
        self.check()
        if policy != self.policy or budget is not self.budget:
            raise WorkerAuthorityError('worker_unauthorized')
        logical = metadata.logical
        proof = worker_snapshot_proof(logical['snapshot']['worker'])
        identity = metadata.logical_commitment_sha256
        if logical_commitment(logical, limits=budget.limits.container_limits) != identity:
            raise WorkerAuthorityError('worker_proof_mismatch')
        if self._verified is not None:
            if self._verified[0] != identity:
                raise WorkerAuthorityError('worker_proof_mismatch')
            return dict(self._verified[1])
        store = self.controller.store
        assignment = _assignment(self.snapshot, self.controller.owner)
        worker_view = replace(store.authority, signer=assignment.worker)
        self.budget.use_crypto()
        request = _request(_decode(worker_view.open(proof.request)))
        self.check()
        if (any(request.get(key) != value for key, value in self.expected.items())
                or request['phase'] != 'snapshot'
                or request['artifact'] != assignment.artifact_id
                or logical['artifact_id'] != assignment.artifact_id
                or request['source'] != logical['snapshot']['source_commitment']
                or request['result'] != bare_result_digest(logical)):
            raise WorkerAuthorityError('worker_proof_mismatch')
        result = dict(version=1, worker=assignment.worker, assignment=request['assignment'],
            job=request['job'], destination=request['destination'],
            request=hashlib.sha256(proof.request).hexdigest(), result=request['result'])
        validate_worker_admission(result, logical, policy.controller_fingerprint)
        self._verified = identity, result
        return dict(result)


@contextmanager
def approval_guard(controller, *, assignment_binding, job_id, source_commitment,
                   destination_binding, policy, budget):
    validate_opaque_id(job_id)
    for value in (assignment_binding, source_commitment, destination_binding):
        validate_digest(value)
    if (not isinstance(budget, _Budget)
            or controller.store.authority.signer != policy.controller_fingerprint):
        raise WorkerAuthorityError('worker_unauthorized')
    guard = None
    try:
        with runtime_use(controller.store.root, exclusive=True, timeout=0):
            budget.use_crypto()
            snapshot = controller.store.load()
            intent = controller.store._intent()
            if intent is not None and (snapshot is None or intent['candidate'] != snapshot.digest):
                raise WorkerAuthorityError('assignment_pending')
            assignment = _assignment(snapshot, controller.owner)
            if (assignment.worker in (policy.origin_fingerprint, policy.controller_fingerprint)
                    or assignment.worker not in controller.store.authority.recipients):
                raise WorkerAuthorityError('worker_unauthorized')
            if _binding(controller.store, controller.owner, snapshot) != assignment_binding:
                raise WorkerAuthorityError('assignment_changed')
            expected = dict(assignment=assignment_binding, job=job_id,
                            source=source_commitment, destination=destination_binding)
            guard = WorkerAdmission(_ISSUER, controller, snapshot, expected, policy, budget)
            guard.check()
            try:
                yield guard
                guard.check()
            finally:
                # Close the capability before releasing the assignment lease.
                guard.active = False
    except TimeoutError:
        raise WorkerAuthorityError('worker_authority_busy') from None
    finally:
        if guard is not None:
            guard.active = False
