"""Durable controller approval of a fixed, separately prepared snapshot.

Uncertain approval is reconciled from retained exact local ciphertext. This
owner never repeats approval after an uncertain checkpoint or acknowledgement.
"""
import hashlib
import json
from pathlib import Path
import re

from ..agent.runtime_lock import runtime_use
from . import artifacts, container_transfer as transfer
from .artifact_records import canonical_bytes, validate_digest, validate_opaque_id
from .container_catalog import CatalogContext
from .container_consolidation import PreparedSnapshot, approve_snapshot
from .container_preparation import ContainerPreparationLimits, _Budget
from .container_runtime import _canonical, _read, _write
from .container_snapshot_recovery import observe_snapshot, receipt_paths, snapshot_receipt
from .container_storage import ensure_private_directory

_SCHEMA = 'localsetup.secure-container.snapshot-approval-job'


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
                         policy, publication_check, limits=ContainerPreparationLimits()) -> PreparedSnapshot:
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
                or not isinstance(controller_context, CatalogContext) or controller_context.role != 'owner'
                or controller_context.signer_fingerprint != policy.controller_fingerprint
                or (controller_context.trust_store_id, controller_context.trust_revision,
                    controller_context.governance_epoch, controller_context.expected_scope)
                    != (policy.trust_store_id, policy.trust_revision, policy.governance_epoch, policy.scope)
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
    budget = _Budget(limits)

    def check():
        budget.checkpoint()
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
                    publication_check=check, limits=limits, _budget=budget)
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
                paths = []
                for index, path in enumerate(output.iterdir()):
                    check()
                    if index >= 2048:
                        raise transfer.TransferError('snapshot_job_limit')
                    if re.fullmatch(r'[0-9a-f]{32}\.lssc', path.name):
                        paths.append(path)
                paths = tuple(sorted(paths))
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
