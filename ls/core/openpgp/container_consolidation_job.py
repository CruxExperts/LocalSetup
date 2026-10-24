"""Durable controller approval of a fixed, separately prepared snapshot.

Uncertain approval is reconciled from retained exact local ciphertext. This
owner never repeats approval after an uncertain checkpoint or acknowledgement.
"""
import hashlib
import json
from pathlib import Path

from ..agent.runtime_lock import runtime_use
from . import artifacts, container_transfer as transfer
from .artifact_records import canonical_bytes, validate_digest, validate_opaque_id
from .container_catalog import CatalogContext
from .container_consolidation import PreparedSnapshot, approve_snapshot, prepare_snapshot
from .container_preparation import ContainerPreparationLimits, _Budget
from .container_runtime import _canonical, _read, _write
from .container_snapshot_recovery import (
    observe_snapshot, published_snapshot_paths, receipt_paths, snapshot_receipt,
)
from .container_storage import ensure_private_directory

_SCHEMA = 'localsetup.secure-container.snapshot-approval-job'


def _matches_context(context, policy, role):
    fingerprint = policy.controller_fingerprint if role == 'owner' else policy.origin_fingerprint
    return (isinstance(context, CatalogContext) and context.role == role
            and context.signer_fingerprint == fingerprint
            and (context.trust_store_id, context.trust_revision,
                 context.governance_epoch, context.expected_scope)
                == (policy.trust_store_id, policy.trust_revision, policy.governance_epoch, policy.scope))


def _require_receipt_capacity(record, output, limits):
    """Reserve room for both receipts before approval can produce ciphertext."""
    prepared = record['prepared']
    approved = dict(prepared, volumes=[
        [row[0], str(output / ('f'*32 + '.lssc')),
         limits.container_limits.max_container_bytes, 'f'*64, 'f'*32, row[5]]
        for row in prepared['volumes']
    ])
    completed = dict(record, phase='approved', approved=approved)
    if len(json.dumps(completed, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')) > 32 * 1024:
        raise transfer.TransferError('progress_limit')


def approve_snapshot_job(snapshot: PreparedSnapshot | None, *, job_root: Path, job_id: str,
                         source_commitment: str, assignment_binding: str,
                         local_context: CatalogContext, controller_context: CatalogContext,
                         policy, publication_check, limits=ContainerPreparationLimits(),
                         _budget=None, _worker_admission=None) -> PreparedSnapshot:
    """Record prepared receipts, approve once, or recover the same exact result.

    A restart may pass snapshot=None: the encrypted record owns the retained
    original paths. The caller must still supply the same trusted assignment
    binding and a current authorization check that raises on rejection.
    """
    try:
        validate_opaque_id(job_id)
        validate_digest(source_commitment)
        validate_digest(assignment_binding)
        if (not isinstance(job_root, Path) or not job_root.is_absolute() or '..' in job_root.parts
                or not isinstance(local_context, CatalogContext) or local_context.role != 'publisher'
                or not _matches_context(controller_context, policy, 'owner')
                or not callable(publication_check) or not isinstance(limits, ContainerPreparationLimits)
                or snapshot is not None and not isinstance(snapshot, PreparedSnapshot)):
            raise ValueError
        binding = hashlib.sha256(canonical_bytes({
            'job': job_id, 'root': str(job_root), 'source': source_commitment,
            'assignment': assignment_binding, 'policy': artifacts.container_admission_policy_sha256(policy),
            'contexts': [[c.catalog_id, c.trust_store_id, c.trust_revision, c.governance_epoch,
                          c.signer_fingerprint, str(c.trust_state_path)]
                         for c in (local_context, controller_context)],
        })).hexdigest()
    except Exception:
        raise transfer.TransferError('snapshot_job_invalid') from None
    budget = _budget if _budget is not None else _Budget(limits)
    if not isinstance(budget, _Budget) or budget.limits != limits:
        raise transfer.TransferError('snapshot_job_invalid')

    def check():
        budget.checkpoint()
        if _worker_admission is not None:
            from .container_worker_admission import WorkerAdmission
            if not isinstance(_worker_admission, WorkerAdmission):
                raise transfer.TransferError('snapshot_job_unauthorized')
            _worker_admission.check()
        transfer._local_authority(local_context)
        artifacts._current_authorities(controller_context.trust_state_path, policy, include_controller=True)
        artifacts._require_fresh_policy(policy)
        if publication_check() is not None:
            raise transfer.TransferError('snapshot_job_unauthorized')
        budget.checkpoint()

    def observe(paths, approved):
        return observe_snapshot(paths, source_commitment=source_commitment, approved=approved,
            policy=policy, context=controller_context, budget=budget, check=check)

    def save(record):
        check()
        sealed = transfer._seal_progress(record, local_context, budget=budget)
        size = len(_canonical(sealed))
        budget.reserve_temp(size)
        try:
            check()
            _write(journal, sealed)
            check()
        finally:
            budget.release_temp(size)

    check()
    ensure_private_directory(job_root, create=True)
    journal, output = job_root/'consolidation.json', job_root/'approved'
    try:
        with runtime_use(job_root, exclusive=True, timeout=0):
            check()
            sealed = _read(journal)
            if sealed is None:
                if snapshot is None:
                    raise transfer.TransferError('snapshot_job_missing')
                requested = snapshot_receipt(snapshot)
                original = observe(receipt_paths(requested), False)
                if snapshot_receipt(original.snapshot) != requested:
                    raise transfer.TransferError('snapshot_job_mismatch')
                record = {'schema': _SCHEMA, 'version': 1, 'binding': binding,
                          'phase': 'prepared', 'prepared': requested, 'approved': None}
                save(record)
            else:
                record = transfer._open_progress(sealed, local_context, budget=budget)
                if (set(record) != {'schema', 'version', 'binding', 'phase', 'prepared', 'approved'}
                        or record['schema'] != _SCHEMA or type(record['version']) is not int
                        or record['version'] != 1 or record['binding'] != binding
                        or record['phase'] not in ('prepared', 'approving', 'approved')
                        or (record['phase'] == 'approved') != (record['approved'] is not None)):
                    raise transfer.TransferError('snapshot_job_mismatch')
                if snapshot is not None and snapshot_receipt(snapshot) != record['prepared']:
                    raise transfer.TransferError('snapshot_job_mismatch')
                original = observe(receipt_paths(record['prepared']), False)
                if snapshot_receipt(original.snapshot) != record['prepared']:
                    raise transfer.TransferError('snapshot_job_mismatch')
            if record['phase'] == 'prepared':
                _require_receipt_capacity(record, output, limits)
                ensure_private_directory(output, create=True)
                record['phase'] = 'approving'
                save(record)  # A lost acknowledgement must not call approval again.
                result = approve_snapshot(original.snapshot, authorized=True, output_dir=output,
                    policy=policy, trust_state_path=controller_context.trust_state_path,
                    gnupg_home=controller_context.gnupg_home,
                    passphrase_reference=controller_context.passphrase_reference,
                    secret_resolver=controller_context.secret_resolver,
                    publication_check=check, limits=limits, _budget=budget,
                    _worker_admission=_worker_admission)
                paths = receipt_paths(snapshot_receipt(result))
            elif record['phase'] == 'approved':
                paths = receipt_paths(record['approved'])
                if any(p.parent != output for p in paths):
                    raise transfer.TransferError('snapshot_job_mismatch')
            else:
                # Enumerate only bounded published leaves in this private job.
                # Hidden in-progress staging stays intact and never counts as a
                # completed approval. Missing/partial sets remain pending.
                ensure_private_directory(output, create=False)
                paths = published_snapshot_paths(output, check)
            approved = observe(paths, True)
            if (approved.origin_bytes != original.origin_bytes
                    or approved.snapshot.claim != original.snapshot.claim
                    or approved.snapshot.prepared.logical_commitment_sha256
                        != original.snapshot.prepared.logical_commitment_sha256):
                raise transfer.TransferError('snapshot_job_conflict')
            receipt = snapshot_receipt(approved.snapshot)
            if record['phase'] == 'approved' and receipt != record['approved']:
                raise transfer.TransferError('snapshot_job_mismatch')
            if record['phase'] != 'approved':
                record.update(phase='approved', approved=receipt)
                save(record)
            check()
            return approved.snapshot
    except TimeoutError:
        raise transfer.TransferError('busy') from None


def prepare_snapshot_job(path: Path, *, reader, authorized: bool, job_root: Path,
                         job_id: str, source_commitment: str, assignment_binding: str,
                         namespace, local_context: CatalogContext, publisher_context: CatalogContext,
                         policy, publication_check, _budget=None, expected_artifact_id=None,
                         worker_authority=None, destination_binding=None) -> PreparedSnapshot:
    """Prepare once or recover one complete, authenticated retained snapshot.

    A missing result after dispatch remains pending. Changing jobs is a separate
    explicit request; this operation never overwrites or deletes partial work.
    """
    from .container_preparation import ContainerNamespace
    from .container_reader import ContainerReader
    from .container_worker_authority import AssignedWorker
    try:
        validate_opaque_id(job_id)
        for value in (source_commitment, assignment_binding):
            validate_digest(value)
        if expected_artifact_id is not None:
            validate_opaque_id(expected_artifact_id)
        if worker_authority is not None:
            if not isinstance(worker_authority, AssignedWorker):
                raise ValueError
            validate_digest(destination_binding)
        if (authorized is not True or not isinstance(reader, ContainerReader)
                or not isinstance(namespace, ContainerNamespace)
                or any(not isinstance(p, Path) or not p.is_absolute() or '..' in p.parts
                       for p in (path, job_root))
                or not isinstance(local_context, CatalogContext) or local_context.role != 'publisher'
                or not _matches_context(publisher_context, policy, 'publisher')
                or not callable(publication_check)):
            raise ValueError
        reader_binding = reader.check()
        binding = hashlib.sha256(canonical_bytes({
            'job': job_id, 'root': str(job_root), 'path': str(path), 'source': source_commitment,
            **({'worker': worker_authority.binding, 'destination': destination_binding}
               if worker_authority is not None else {}),
            **({'artifact': expected_artifact_id} if expected_artifact_id is not None else {}),
            'assignment': assignment_binding, 'reader': reader_binding,
            'read_policy': artifacts.container_admission_policy_sha256(reader.policy),
            'policy': artifacts.container_admission_policy_sha256(policy),
            'namespace': [namespace.tenant_id, namespace.project_id, namespace.publisher_opaque_id],
            'contexts': [[c.catalog_id, c.trust_store_id, c.trust_revision, c.governance_epoch,
                          c.signer_fingerprint, str(c.trust_state_path)]
                         for c in (local_context, publisher_context)],
        })).hexdigest()
    except Exception:
        raise transfer.TransferError('snapshot_job_invalid') from None
    budget = _budget if _budget is not None else _Budget(reader.limits)
    if not isinstance(budget, _Budget) or budget.limits != reader.limits:
        raise transfer.TransferError('snapshot_job_invalid')

    def check():
        budget.checkpoint()
        transfer._local_authority(local_context)
        artifacts._current_authorities(publisher_context.trust_state_path, policy, include_controller=False)
        artifacts._require_fresh_policy(policy)
        if reader.check() != reader_binding or publication_check() is not None:
            raise transfer.TransferError('snapshot_job_unauthorized')
        budget.checkpoint()

    def save(record):
        check()
        sealed = transfer._seal_progress(record, local_context, budget=budget)
        size = len(_canonical(sealed))
        budget.reserve_temp(size)
        try:
            check()
            _write(journal, sealed)
            check()
        finally:
            budget.release_temp(size)

    check()
    ensure_private_directory(job_root, create=True)
    journal, output = job_root/'preparation.json', job_root/'prepared'
    try:
        with runtime_use(job_root, exclusive=True, timeout=0):
            check()
            sealed = _read(journal)
            if sealed is None:
                ensure_private_directory(output, create=True)
                if next(output.iterdir(), None) is not None:
                    raise transfer.TransferError('snapshot_job_conflict')
                record = dict(schema='localsetup.secure-container.snapshot-preparation-job',
                              version=1, binding=binding, phase='preparing', snapshot=None)
                save(record)
                result = prepare_snapshot(path, reader=reader, authorized=True, output_dir=output,
                    namespace=namespace, publication_policy=policy,
                    trust_state_path=publisher_context.trust_state_path, gnupg_home=publisher_context.gnupg_home,
                    passphrase_reference=publisher_context.passphrase_reference,
                    secret_resolver=publisher_context.secret_resolver, expected_commitment=source_commitment,
                    publication_check=check, _budget=budget, expected_artifact_id=expected_artifact_id,
                    worker_authority=worker_authority, job_id=job_id, destination_binding=destination_binding)
                paths = receipt_paths(snapshot_receipt(result))
            else:
                record = transfer._open_progress(sealed, local_context, budget=budget)
                if (set(record) != {'schema', 'version', 'binding', 'phase', 'snapshot'}
                        or record['schema'] != 'localsetup.secure-container.snapshot-preparation-job'
                        or type(record['version']) is not int or record['version'] != 1
                        or record['binding'] != binding or record['phase'] not in ('preparing', 'prepared')
                        or (record['phase'] == 'prepared') != (record['snapshot'] is not None)):
                    raise transfer.TransferError('snapshot_job_mismatch')
                ensure_private_directory(output, create=False)
                paths = published_snapshot_paths(output, check)
            observed = observe_snapshot(paths, source_commitment=source_commitment, approved=False,
                policy=reader.policy, context=reader, budget=budget, check=check).snapshot
            if expected_artifact_id is not None and observed.prepared.artifact_id != expected_artifact_id:
                raise transfer.TransferError('snapshot_job_unauthorized')
            if sealed is None and snapshot_receipt(observed) != snapshot_receipt(result):
                raise transfer.TransferError('snapshot_job_mismatch')
            digest = hashlib.sha256(canonical_bytes(snapshot_receipt(observed))).hexdigest()
            if record['phase'] == 'prepared' and digest != record['snapshot']:
                raise transfer.TransferError('snapshot_job_mismatch')
            if record['phase'] != 'prepared':
                record.update(phase='prepared', snapshot=digest)
                save(record)
            check()
            return observed
    except TimeoutError:
        raise transfer.TransferError('busy') from None


def run_snapshot_job(path: Path, *, reader, authorized: bool, job_root: Path,
                     job_id: str, source_commitment: str, assignment_binding: str,
                     namespace, local_context: CatalogContext, publisher_context: CatalogContext,
                     controller_context: CatalogContext, publication_policy, approval_policy,
                     publication_check, destination_binding: str, destination,
                     publication_mode: str = 'conditional', worker_authority=None,
                     assignment_controller=None):
    """One bounded lifecycle from reconstruction through verified delivery.

    The reader supplies receiver acceptance policy, not publishing authority.
    Caller authorization is independent and rechecked throughout every stage.
    Worker approval uses the authoritative controller's assignment lease. The
    optional explicit controller supports a separate transport for phase checks.
    """
    from .container_publication_job import publish_prepared_job
    from .container_reader import ContainerReader
    from contextlib import nullcontext
    from .container_worker_authority import AssignedWorker, AssignmentController
    if (authorized is not True or not isinstance(reader, ContainerReader)
            or not callable(publication_check) or not isinstance(job_root, Path)
            or not job_root.is_absolute() or '..' in job_root.parts
            or publication_mode not in ('conditional', 'content_addressed')
            or reader.limits.timeout_seconds > 3600):
        raise transfer.TransferError('snapshot_job_invalid')
    try:
        validate_opaque_id(job_id)
        for value in (source_commitment, assignment_binding, destination_binding):
            validate_digest(value)
        if worker_authority is not None and not isinstance(worker_authority, AssignedWorker):
            raise ValueError
        controller = assignment_controller
        if worker_authority is not None and controller is None:
            controller = worker_authority.endpoint
        if worker_authority is None and controller is not None:
            raise ValueError
        if worker_authority is not None and (
                worker_authority.worker.signer == publication_policy.origin_fingerprint
                or worker_authority.view.authority.signer != approval_policy.controller_fingerprint
                or not isinstance(controller, AssignmentController)):
            raise ValueError
        if (not _matches_context(controller_context, approval_policy, 'owner')
                or not _matches_context(publisher_context, publication_policy, 'publisher')
                or not isinstance(local_context, CatalogContext) or local_context.role != 'publisher'):
            raise ValueError
        # Bind the entire lifecycle before its first signing operation. A changed
        # destination or later-stage identity cannot reuse an unfinished job.
        assignment_binding = hashlib.sha256(canonical_bytes({
            'assignment': assignment_binding, 'destination': destination_binding, 'mode': publication_mode,
            **({'worker': worker_authority.binding} if worker_authority is not None else {}),
            'policies': [artifacts.container_admission_policy_sha256(p)
                         for p in (reader.policy, publication_policy, approval_policy)],
            'contexts': [[c.catalog_id, c.trust_store_id, c.trust_revision, c.governance_epoch,
                          c.signer_fingerprint, str(c.trust_state_path)]
                         for c in (local_context, publisher_context, controller_context)],
        })).hexdigest()
    except Exception:
        raise transfer.TransferError('snapshot_job_invalid') from None
    budget = _Budget(reader.limits)

    def check():
        budget.checkpoint()
        transfer._local_authority(local_context)
        if publication_check() is not None:
            raise transfer.TransferError('snapshot_job_unauthorized')
        reader.check()
        artifacts._current_authorities(controller_context.trust_state_path, approval_policy,
                                       include_controller=True)
        artifacts._require_fresh_policy(approval_policy)
        budget.checkpoint()

    check()
    def worker_phase(phase):
        check()
        if worker_authority is not None:
            worker_authority.authorize(phase, job_id=job_id, source_commitment=source_commitment,
                                       destination_binding=destination_binding, budget=budget)
        check()

    worker_phase('prepare')
    ensure_private_directory(job_root, create=True)
    try:
        with runtime_use(job_root, exclusive=True, timeout=0):
            prepared = prepare_snapshot_job(path, reader=reader, authorized=authorized,
                job_root=job_root/'prepare', job_id=job_id, source_commitment=source_commitment,
                assignment_binding=assignment_binding, namespace=namespace, local_context=local_context,
                publisher_context=publisher_context, policy=publication_policy,
                publication_check=check, _budget=budget,
                worker_authority=worker_authority, destination_binding=destination_binding,
                expected_artifact_id=worker_authority.assignment.artifact_id if worker_authority is not None else None)
            worker_phase('approve')
            admission = (controller.approval_guard(
                assignment_binding=worker_authority.binding, job_id=job_id,
                source_commitment=source_commitment, destination_binding=destination_binding,
                policy=approval_policy, budget=budget) if worker_authority is not None else nullcontext(None))
            with admission as guard:
                approved = approve_snapshot_job(prepared, job_root=job_root/'approve', job_id=job_id,
                    source_commitment=source_commitment, assignment_binding=assignment_binding,
                    local_context=local_context, controller_context=controller_context, policy=approval_policy,
                    publication_check=check, limits=reader.limits, _budget=budget, _worker_admission=guard)
            worker_phase('deliver')
            result = publish_prepared_job(approved.prepared, job_root=job_root/'deliver', job_id=job_id,
                destination_binding=destination_binding,
                destinations={v.volume_id: destination for v in approved.prepared.volumes},
                authorization_check=check, local_context=local_context, policy=reader.policy,
                trust_state_path=reader.trust_state_path, gnupg_home=reader.gnupg_home,
                passphrase_reference=reader.passphrase_reference, secret_resolver=reader.secret_resolver,
                publication_mode=publication_mode, limits=reader.limits, _budget=budget)
            check()
            return approved, result
    except TimeoutError:
        raise transfer.TransferError('busy') from None
