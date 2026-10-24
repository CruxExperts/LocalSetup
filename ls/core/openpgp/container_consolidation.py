"""Prepare immutable complete snapshots through existing read and seal owners.

This operation does not grant upload authority, choose branch winners or remove
history. Callers explicitly supply both reading and publishing authority.
"""
from contextlib import ExitStack
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import tempfile

from . import artifacts
from .artifact_records import parse_provenance
from .container_delta_reader import delta_read_context
from .container_delta_reconstruction import _DeltaContext
from .container_preparation import ContainerInput, PreparedContainerSet, ContainerPreparationLimits, _Budget
from .container_reader import ContainerReader, ContainerReadError
from .container_reconstruction import extract_container_file
from .container_snapshot_records import CoveredFile, SnapshotClaim, MAX_COVERED_FILES
from .container_sources import LocalFileRangeSource
from .envelope_io import AnchoredFile, ExclusiveOutput


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    prepared: PreparedContainerSet
    claim: SnapshotClaim


def prepare_snapshot(path: Path, *, reader: ContainerReader, authorized: bool,
                     output_dir, namespace, publication_policy,
                     trust_state_path, gnupg_home, passphrase_reference, secret_resolver,
                     expected_commitment=None, expected_artifact_id=None, publication_check=None, _budget=None):
    """Reconstruct all files of one exact version, then sign a new full version.

    The reader's configured inventory is frozen once. New arrivals do not expand
    the covered set. The optional publication check binds a caller's separate
    job or worker authority and runs at preparation's existing publish gates.
    """
    if authorized is not True or not isinstance(reader, ContainerReader):
        raise artifacts.ArtifactError(artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED)
    if publication_check is not None and not callable(publication_check):
        raise ValueError('Invalid snapshot publication check')
    budget = _budget if _budget is not None else _Budget(reader.limits)
    if not isinstance(budget, _Budget) or budget.limits != reader.limits:
        raise ValueError('Invalid snapshot budget')
    initial = budget.temporary
    try:
        budget.checkpoint()
        binding = reader.check()
        if publication_check is not None:
            publication_check()
        boundary = artifacts._require_boundary(reader.policy.boundary)
        with ExitStack() as stack:
            workspace = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.lssc-snapshot-', dir=boundary)))
            admission = replace(reader.policy, boundary=workspace)
            source = stack.enter_context(LocalFileRangeSource(path))
            metadata = reader._inspect(source, admission, budget, workspace)
            if expected_commitment is not None and metadata.logical_commitment_sha256 != expected_commitment:
                raise ContainerReadError('changed')
            if expected_artifact_id is not None and metadata.logical['artifact_id'] != expected_artifact_id:
                raise ContainerReadError('unauthorized')
            frozen = replace(reader, source_paths=reader._paths(budget), source_resolver=None,
                             metadata_resolver=None, reference_resolver=None)
            sources = {metadata.key: source}
            frozen._sources(sources, stack, metadata, admission, budget, workspace, path, None)
            dependencies = [(item, item.generation) for item in sources.values()]
            delta_policy = (delta_read_context(frozen, stack, admission, budget, workspace, dependencies).policy
                            if frozen.delta_codec is not None else None)
            covered, verified_metadata, inputs = {}, {}, []

            def verify():
                budget.checkpoint()
                if frozen.check(metadata) != binding:
                    raise ContainerReadError('changed')
                for item in verified_metadata.values():
                    budget.checkpoint()
                    artifacts._policy_logical(item.logical, admission, historical=False,
                                              limits=budget.limits.container_limits)
                if any(item.generation != generation for item, generation in dependencies):
                    raise ContainerReadError('changed')
                if publication_check is not None:
                    publication_check()

            for file in sorted(metadata.logical['files'], key=lambda item: item['file_id']):
                verify()
                context = _DeltaContext(delta_policy)
                name = file['file_id']+'.plain'
                extract_container_file(sources, file['file_id'], name, authorized=True,
                    policy=admission, trust_state_path=frozen.trust_state_path, gnupg_home=frozen.gnupg_home,
                    passphrase_reference=frozen.passphrase_reference, secret_resolver=frozen.secret_resolver,
                    limits=budget.limits, _budget=budget, _delta_context=context)
                for item, selected in context.selected:
                    claim = CoveredFile(item.logical['version_id'], item.logical_commitment_sha256,
                        selected['file_id'], selected['length'], selected['sha256'],
                        selected.get('delta', {}).get('change_id'))
                    previous = covered.setdefault(claim.key, claim)
                    if previous != claim:
                        raise ContainerReadError('conflict')
                    if len(covered) > MAX_COVERED_FILES:
                        raise ContainerReadError('limit_exceeded')
                    verified_metadata[item.logical_commitment_sha256] = item
                inputs.append(ContainerInput(workspace/name, file['file_id'], parse_provenance(file['provenance'])))
            claim = SnapshotClaim(metadata.logical['version_id'], metadata.logical_commitment_sha256,
                                  tuple(covered[key] for key in sorted(covered)))
            verify()
            prepared = artifacts.prepare_container(tuple(inputs), output_dir=output_dir, namespace=namespace,
                policy=publication_policy, trust_state_path=trust_state_path, gnupg_home=gnupg_home,
                passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                limits=budget.limits, artifact_id=metadata.logical['artifact_id'],
                parent_versions=(claim.source_version,), snapshot=claim, _budget=budget,
                publication_check=verify)
            return PreparedSnapshot(prepared, claim)
    finally:
        budget.release_temp(budget.temporary - initial)


def approve_snapshot(snapshot: PreparedSnapshot, *, authorized: bool, output_dir,
                     policy, trust_state_path, gnupg_home, passphrase_reference, secret_resolver,
                     limits=ContainerPreparationLimits(), publication_check=None, _budget=None):
    """Controller-approve the complete exact snapshot, then publish its local set.

    No carrier upload or history removal occurs. Existing controller trust and
    the caller's current job authority are rechecked at every publication gate.
    """
    from .container_transfer import _copy_ciphertext
    if (authorized is not True or not isinstance(snapshot, PreparedSnapshot)
            or not isinstance(snapshot.prepared, PreparedContainerSet)
            or not isinstance(snapshot.claim, SnapshotClaim)):
        raise artifacts.ArtifactError(artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED)
    if publication_check is not None and not callable(publication_check):
        raise ValueError('Invalid snapshot publication check')
    budget = _budget if _budget is not None else _Budget(limits)
    if not isinstance(budget, _Budget) or budget.limits != limits:
        raise ValueError('Invalid snapshot budget')
    volumes = snapshot.prepared.volumes
    if (not volumes or len(volumes) > limits.container_limits.max_volumes
            or len({v.volume_id for v in volumes}) != len(volumes)):
        raise ValueError('Invalid snapshot volume set')
    destination = artifacts._require_boundary(output_dir)
    initial = budget.temporary
    inventory = None

    def current():
        budget.checkpoint()
        artifacts._current_authorities(trust_state_path, policy, include_controller=True)
        artifacts._require_fresh_policy(policy)
        if publication_check is not None:
            publication_check()
        budget.checkpoint()

    def matching(metadata, volume):
        nonlocal inventory
        if (metadata.logical_commitment_sha256 != snapshot.prepared.logical_commitment_sha256
                or metadata.logical['artifact_id'] != snapshot.prepared.artifact_id
                or metadata.logical['version_id'] != snapshot.prepared.version_id
                or metadata.key != volume.key or metadata.volume_id != volume.volume_id
                or metadata.logical.get('snapshot') != snapshot.claim.record()
                or metadata.volume_record['volume_id'] != volume.volume_id
                or len(metadata.volume_record['messages']) != volume.message_count
                or {row['volume_id'] for row in metadata.inventory['volumes']}
                    != {item.volume_id for item in volumes}
                or inventory is not None and metadata.inventory != inventory):
            raise ContainerReadError('changed')
        inventory = metadata.inventory

    try:
        current()
        with ExitStack() as stack:
            workspace = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.lssc-approve-set-', dir=destination)))
            staged, approved = [], []
            for volume in volumes:
                current()
                with LocalFileRangeSource(volume.path) as source:
                    if source.size != volume.serialized_length:
                        raise ContainerReadError('changed')
                    digest = hashlib.sha256()
                    for offset in range(0, source.size, 1024*1024):
                        current()
                        digest.update(source.read_range(offset, min(1024*1024, source.size-offset)))
                    if digest.hexdigest() != volume.transport_sha256:
                        raise ContainerReadError('changed')
                    result = artifacts.approve_container_distribution(source, output_dir=workspace,
                        policy=policy, trust_state_path=trust_state_path, gnupg_home=gnupg_home,
                        passphrase_reference=passphrase_reference, secret_resolver=secret_resolver,
                        limits=limits, publication_check=current,
                        metadata_check=lambda metadata: matching(metadata, volume), _budget=budget)
                # Keep both the private approved ciphertext and its staged
                # destination charged until the enclosing workspace closes.
                budget.reserve_temp(result.serialized_length * 2)
                output = stack.enter_context(ExclusiveOutput(destination / result.path.name))
                name, descriptor = output.stage()
                with os.fdopen(descriptor, 'wb') as stream, AnchoredFile(result.path, private=True) as source:
                    _copy_ciphertext(source, stream, length=result.serialized_length,
                                     sha256=result.transport_sha256, check=current)
                    stream.flush()
                    os.fsync(stream.fileno())
                staged.append((output, name))
                approved.append(replace(result, path=output.path))
            current()
            for output, name in staged:
                output.publish(name)
                current()
            for output, _name in staged:
                output.published_identity = None
            return PreparedSnapshot(replace(snapshot.prepared, volumes=tuple(approved)), snapshot.claim)
    finally:
        budget.release_temp(budget.temporary - initial)
