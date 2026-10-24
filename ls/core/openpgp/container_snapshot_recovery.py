"""Read-only observation of complete snapshot ciphertext after interruption."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import tempfile

from . import artifacts
from .artifact_records import parse_descriptor
from .container_consolidation import PreparedSnapshot
from .container_format import scan_message_descriptors
from .container_preparation import PreparedContainerSet, PreparedVolume
from .container_snapshot_records import snapshot_claim
from .container_sources import LocalFileRangeSource
from .container_transfer import TransferError


def snapshot_receipt(snapshot):
    prepared = snapshot.prepared
    return {'artifact': prepared.artifact_id, 'version': prepared.version_id,
            'commitment': prepared.logical_commitment_sha256,
            'checkpoint': snapshot.claim.checkpoint_sha256,
            'volumes': [[v.key, str(v.path), v.serialized_length, v.transport_sha256,
                         v.volume_id, v.message_count] for v in prepared.volumes]}


def receipt_paths(receipt):
    try:
        if (type(receipt) is not dict
                or set(receipt) != {'artifact', 'version', 'commitment', 'checkpoint', 'volumes'}
                or type(receipt['volumes']) is not list or not 1 <= len(receipt['volumes']) <= 1024
                or any(type(row) is not list or len(row) != 6 or type(row[1]) is not str
                       for row in receipt['volumes'])):
            raise ValueError
        paths = tuple(Path(row[1]) for row in receipt['volumes'])
        if len(set(paths)) != len(paths) or any(not p.is_absolute() or '..' in p.parts for p in paths):
            raise ValueError
        return paths
    except Exception:
        raise TransferError('snapshot_job_invalid') from None


@dataclass(frozen=True, slots=True)
class SnapshotObservation:
    snapshot: PreparedSnapshot
    # Exact origin metadata and payload ciphertext digests per origin volume.
    origin_bytes: dict


def observe_snapshot(paths, *, source_commitment, approved, policy, context, budget, check):
    """Authenticate one complete, unambiguous set without signing or uploading.

    An approved set must later be compared with the retained original set;
    metadata authentication alone does not prove identical payload ciphertext.
    """
    if (type(paths) is not tuple or not 1 <= len(paths) <= min(1024, budget.limits.container_limits.max_volumes)
            or len(set(paths)) != len(paths) or type(approved) is not bool):
        raise TransferError('snapshot_job_pending')
    inventory = logical = claim = None
    volumes, evidence = {}, {}
    artifacts._require_boundary(policy.boundary)
    with tempfile.TemporaryDirectory(prefix='.lssc-recover-', dir=policy.boundary) as temporary:
        for path in paths:
            check()
            with LocalFileRangeSource(path) as raw:
                source = artifacts._BudgetedRangeSource(raw, budget)
                metadata = artifacts.inspect_container_metadata(source, policy=policy,
                    trust_state_path=context.trust_state_path, gnupg_home=context.gnupg_home,
                    passphrase_reference=context.passphrase_reference, secret_resolver=context.secret_resolver,
                    allow_origin_unapproved=not approved, limits=budget.limits.container_limits,
                    _budget=budget, _workspace=Path(temporary))
                observed_claim = snapshot_claim(metadata.logical.get('snapshot'))
                identity = (metadata.logical['artifact_id'], metadata.logical['version_id'],
                            metadata.logical_commitment_sha256)
                if (len(metadata.metadata_layers) != (2 if approved else 1)
                        or observed_claim.source_commitment != source_commitment
                        or logical is not None and identity != logical
                        or inventory is not None and metadata.inventory != inventory
                        or claim is not None and observed_claim != claim
                        or metadata.origin_volume_id in volumes):
                    raise TransferError('snapshot_job_conflict')
                logical, inventory, claim = identity, metadata.inventory, observed_claim
                descriptors = tuple(parse_descriptor(row['descriptor']) for row in metadata.volume_record['messages'])
                if scan_message_descriptors(source, header=metadata.header,
                        limits=budget.limits.container_limits) != descriptors:
                    raise TransferError('ciphertext_invalid')
                transport = hashlib.sha256()
                length = source.size
                for offset in range(0, length, 1024*1024):
                    check()
                    transport.update(source.read_range(offset, min(1024*1024, length-offset)))
                check()
                volumes[metadata.origin_volume_id] = PreparedVolume(metadata.key, Path(path), length,
                    transport.hexdigest(), metadata.volume_id, len(descriptors))
                evidence[metadata.origin_volume_id] = (
                    hashlib.sha256(metadata.metadata_layers[-1][0]).hexdigest(),
                    tuple(item.sha256 for item in descriptors))
    expected = [row['volume_id'] for row in inventory['volumes']]
    if set(volumes) != set(expected):
        raise TransferError('snapshot_job_pending')
    check()
    return SnapshotObservation(PreparedSnapshot(PreparedContainerSet(*logical,
        tuple(volumes[key] for key in expected)), claim), evidence)
