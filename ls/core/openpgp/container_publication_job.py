"""Encrypted publication checkpoints; uncertain dispatch is observation-only.

The operator supplies a stable job and destination binding plus current write
authorization. A local lease serializes this journal, not distributed workers.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Callable, Mapping

from ..agent.runtime_lock import runtime_use
from .artifact_records import canonical_bytes, parse_container_key, validate_opaque_id
from .artifacts import container_admission_policy_sha256
from .container_catalog import CatalogContext
from .container_preparation import PreparedContainerSet, PreparedVolume, ContainerPreparationLimits, _Budget
from .container_runtime import _canonical, _read, _write
from .container_storage import ensure_private_directory
from . import container_transfer as transfer

_SCHEMA = 'localsetup.secure-container.publication-job'
_MAX_VOLUMES = 1024


@dataclass(frozen=True, slots=True)
class PublicationJobResult:
    job_id: str
    completed_volumes: int
    total_volumes: int


def _binding(prepared, job_id, destination_binding, publication_mode, context, policy):
    try:
        if (not isinstance(prepared, PreparedContainerSet)
                or type(prepared.volumes) is not tuple or not 1 <= len(prepared.volumes) <= _MAX_VOLUMES
                or not isinstance(context, CatalogContext)
                or publication_mode not in ('conditional', 'content_addressed')):
            raise ValueError
        for value in (prepared.artifact_id, prepared.version_id, job_id):
            validate_opaque_id(value)
        for value in (prepared.logical_commitment_sha256, destination_binding):
            if not isinstance(value, str) or transfer._HEX.fullmatch(value) is None:
                raise ValueError
        volumes = []
        for volume in prepared.volumes:
            if (not isinstance(volume, PreparedVolume) or not isinstance(volume.path, Path)
                    or not volume.path.is_absolute() or '..' in volume.path.parts
                    or type(volume.serialized_length) is not int or volume.serialized_length <= 0
                    or type(volume.message_count) is not int or volume.message_count <= 0
                    or not isinstance(volume.transport_sha256, str)
                    or transfer._HEX.fullmatch(volume.transport_sha256) is None):
                raise ValueError
            validate_opaque_id(volume.volume_id)
            parse_container_key(volume.key, prepared.artifact_id, prepared.version_id, volume.volume_id)
            volumes.append([volume.volume_id, volume.key, str(volume.path), volume.serialized_length,
                            volume.transport_sha256, volume.message_count])
        if len({v[0] for v in volumes}) != len(volumes) or len({v[2] for v in volumes}) != len(volumes):
            raise ValueError
        return hashlib.sha256(canonical_bytes({
            'job': job_id, 'destination': destination_binding, 'mode': publication_mode,
            'artifact': prepared.artifact_id, 'version': prepared.version_id,
            'commitment': prepared.logical_commitment_sha256, 'volumes': volumes,
            'policy': container_admission_policy_sha256(policy),
            'local': [context.catalog_id, context.trust_store_id, context.trust_revision,
                      context.governance_epoch, context.signer_fingerprint],
        })).hexdigest()
    except Exception:
        raise transfer.TransferError('publication_job_invalid') from None


def publish_prepared_job(
    prepared: PreparedContainerSet, *, job_root: Path, job_id: str,
    destination_binding: str, destinations: Mapping[str, object],
    authorization_check: Callable[[], None], local_context: CatalogContext,
    policy, trust_state_path, gnupg_home, passphrase_reference=None, secret_resolver=None,
    publication_mode: str = 'conditional', limits=ContainerPreparationLimits(), _budget=None,
    dependencies: tuple[PreparedContainerSet, ...] = (),
) -> PublicationJobResult:
    """Publish a fixed volume set, resuming unknown operations by observation.

    destination_binding identifies the operator-approved target configuration;
    destination adapters must resolve that exact target. It is not authority.
    Keep the same prepared bytes, ordered volume set and job_id across restarts.
    Completed entries are historical delivery evidence, not availability claims.
    Optional dependency sets precede the selected version in the same journal.
    Completed dependencies are observed again on resume, never silently replayed.
    """
    if (not isinstance(job_root, Path) or not callable(authorization_check)
            or not isinstance(limits, ContainerPreparationLimits) or limits.timeout_seconds > 3600):
        raise transfer.TransferError('publication_job_invalid')
    binding = _binding(prepared, job_id, destination_binding, publication_mode, local_context, policy)
    if (type(dependencies) is not tuple or len(dependencies) > _MAX_VOLUMES
            or any(not isinstance(item, PreparedContainerSet) or type(item.volumes) is not tuple
                   for item in dependencies)
            or len(prepared.volumes) + sum(len(item.volumes) for item in dependencies) > _MAX_VOLUMES):
        raise transfer.TransferError('publication_job_invalid')
    if dependencies:
        dependency_bindings = [_binding(item, job_id, destination_binding, publication_mode, local_context, policy)
                               for item in dependencies]
        binding = hashlib.sha256(canonical_bytes({'selected': binding, 'dependencies': dependency_bindings})).hexdigest()
    entries = [(volume, item.logical_commitment_sha256, True)
               for item in dependencies for volume in item.volumes]
    entries.extend((volume, prepared.logical_commitment_sha256, False) for volume in prepared.volumes)
    if (len(entries) > _MAX_VOLUMES or len({v.volume_id for v, _, _ in entries}) != len(entries)
            or len({v.key for v, _, _ in entries}) != len(entries)
            or len({v.path for v, _, _ in entries}) != len(entries)
            or not isinstance(destinations, Mapping) or set(destinations) != {v.volume_id for v, _, _ in entries}):
        raise transfer.TransferError('publication_job_invalid')
    destinations = dict(destinations)
    budget = _budget if _budget is not None else _Budget(limits)
    if not isinstance(budget, _Budget) or budget.limits != limits:
        raise transfer.TransferError('publication_job_invalid')

    def check():
        budget.checkpoint()
        transfer._local_authority(local_context)
        authorization_check()
        budget.checkpoint()

    def save(record):
        check()
        sealed = transfer._seal_progress(record, local_context, budget=budget)
        size = len(_canonical(sealed))
        budget.reserve_temp(size)
        try:
            check()
            _write(path, sealed)
            check()
        finally:
            budget.release_temp(size)

    check()
    ensure_private_directory(job_root, create=True)
    path = job_root / 'publication.json'
    try:
        with runtime_use(job_root, exclusive=True, timeout=0):
            check()
            sealed = _read(path)
            if sealed is None:
                record = {'schema': _SCHEMA, 'version': 1, 'binding': binding,
                          'states': 'p' * len(entries)}
                save(record)
            else:
                record = transfer._open_progress(sealed, local_context, budget=budget)
                if (set(record) != {'schema', 'version', 'binding', 'states'}
                        or record['schema'] != _SCHEMA or type(record['version']) is not int
                        or record['version'] != 1 or record['binding'] != binding
                        or type(record['states']) is not str
                        or len(record['states']) != len(entries)
                        or not set(record['states']) <= {'p', '?', 'v'}):
                    raise transfer.TransferError('publication_job_mismatch')
            for index, (volume, commitment, dependency) in enumerate(entries):
                check()
                previous = record['states'][index]
                if previous == 'v' and not dependency:
                    continue
                if previous == 'p':
                    # Persist uncertainty before handing control to any write
                    # adapter. A failed checkpoint cannot dispatch publication.
                    record['states'] = record['states'][:index] + '?' + record['states'][index+1:]
                    save(record)
                check()
                transfer.publish_prepared_volume(volume, destination=destinations[volume.volume_id],
                    local_context=local_context, policy=policy, trust_state_path=trust_state_path,
                    gnupg_home=gnupg_home, passphrase_reference=passphrase_reference,
                    secret_resolver=secret_resolver, limits=limits.container_limits,
                    cancel_check=lambda: _cancelled(check),
                    timeout_seconds=min(3600, budget.deadline - transfer.time.monotonic()),
                    publication_mode=publication_mode, reconcile_only=previous in ('?', 'v'),
                    expected_logical_commitment=commitment, _budget=budget)
                check()
                if previous == 'v':
                    continue
                record['states'] = record['states'][:index] + 'v' + record['states'][index+1:]
                save(record)
            check()
            return PublicationJobResult(job_id, record['states'].count('v'), len(entries))
    except TimeoutError:
        raise transfer.TransferError('busy') from None


def _cancelled(check):
    try:
        check()
        return False
    except Exception:
        return True
